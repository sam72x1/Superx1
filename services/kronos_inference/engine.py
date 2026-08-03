"""تحميل Kronos من checkout خارجي وتنفيذ الاستدلال المتسلسل."""

from __future__ import annotations

import importlib
import hashlib
import math
import subprocess
import sys
import threading
from importlib.metadata import PackageNotFoundError, version as package_version
from importlib.util import find_spec
from pathlib import Path
from time import monotonic, perf_counter
from typing import Any

from .config import ServiceConfig
from .validation import ForecastRequest


_READINESS_CACHE_SECONDS = 30.0
_RUNTIME_DISTRIBUTIONS = {
    "numpy": "numpy",
    "pandas": "pandas",
    "torch": "torch",
    "einops": "einops",
    "huggingface_hub": "huggingface_hub",
    "safetensors": "safetensors",
    "tqdm": "tqdm",
}


class EngineUnavailable(RuntimeError):
    """تعذّر تجهيز النموذج أو اعتماداته."""


class InferenceFailed(RuntimeError):
    """فشل النموذج بعد اكتمال تحميله."""


def _service_source_revision() -> str:
    """بصمة كود الغلاف وmanifest الاعتمادات المثبتة للتجربة."""
    package_root = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    paths = [*package_root.glob("*.py"), package_root / "requirements.txt"]
    for path in sorted(paths):
        relative = path.relative_to(package_root).as_posix().encode("utf-8")
        body = path.read_bytes()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(body).to_bytes(8, "big"))
        digest.update(body)
    return digest.hexdigest()


def _runtime_requirement_pins() -> dict[str, str]:
    """يقرأ pins الاستدلال نفسها؛ readiness لا يقبل بيئة مختلفة صامتًا."""
    requirements = Path(__file__).resolve().parent / "requirements.txt"
    pins: dict[str, str] = {}
    for raw_line in requirements.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        name, separator, pinned_version = line.partition("==")
        name = name.strip()
        pinned_version = pinned_version.strip()
        if not separator or not name or not pinned_version:
            raise EngineUnavailable("اعتمادات Kronos يجب أن تكون مثبتة بإصدارات دقيقة")
        pins[name.casefold().replace("-", "_")] = pinned_version
    expected = set(_RUNTIME_DISTRIBUTIONS.values())
    if set(pins) != expected:
        raise EngineUnavailable("ملف اعتمادات Kronos لا يطابق بيئة التشغيل المتوقعة")
    return pins


def _runtime_dependencies_ready() -> bool:
    """يتحقق من وجود modules ومن نسخة كل distribution بلا استيرادها."""
    pins = _runtime_requirement_pins()
    for module, distribution in _RUNTIME_DISTRIBUTIONS.items():
        if find_spec(module) is None:
            return False
        try:
            installed = package_version(distribution)
        except PackageNotFoundError:
            return False
        if installed != pins[distribution]:
            return False
    return True


class KronosEngine:
    """محرّك lazy؛ لا يحمل PyTorch أو الأوزان أثناء الاستيراد أو health."""

    def __init__(self, config: ServiceConfig) -> None:
        self._config = config
        self._predictor: Any | None = None
        self._pandas: Any | None = None
        self._torch: Any | None = None
        self._inference_lock = threading.Lock()
        self._readiness_lock = threading.Lock()
        self._readiness_cached: bool | None = None
        self._readiness_checked_at = 0.0
        self._service_revision = _service_source_revision()

    @property
    def loaded(self) -> bool:
        return self._predictor is not None

    def _identity(self) -> dict[str, Any]:
        predictor_device = getattr(self._predictor, "device", None)
        device = str(predictor_device or self._config.device or "auto")
        return {
            "model": self._config.model_id,
            "model_revision": self._config.model_revision,
            "tokenizer": self._config.tokenizer_id,
            "tokenizer_revision": self._config.tokenizer_revision,
            "kronos_revision": self._config.kronos_source_revision,
            "service_revision": self._service_revision,
            "market_timezone": self._config.market_timezone,
            "device": device,
            "max_context": self._config.max_context,
        }

    def health(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "model_loaded": self.loaded,
            **self._identity(),
        }

    def _resolve_repo_path(self) -> Path:
        if not self._config.kronos_repo_path:
            raise EngineUnavailable("KRONOS_REPO_PATH غير مضبوط")
        try:
            repo_path = Path(self._config.kronos_repo_path).expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise EngineUnavailable("KRONOS_REPO_PATH غير موجود") from exc
        if not repo_path.is_dir() or not (repo_path / "model" / "kronos.py").is_file():
            raise EngineUnavailable("KRONOS_REPO_PATH لا يشير إلى checkout صالح")
        self._verify_source_revision(repo_path)
        return repo_path

    def _verify_source_revision(self, repo_path: Path) -> None:
        """يثبت أن كود model نظيف وأن HEAD يطابق هوية التجربة المعلنة."""
        prefix = ["git", "--no-optional-locks", "-C", str(repo_path)]
        try:
            head = subprocess.run(
                [*prefix, "rev-parse", "HEAD"],
                check=True, capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            dirty = subprocess.run(
                [*prefix, "status", "--porcelain", "--untracked-files=all"],
                check=True, capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            tracked = set(subprocess.run(
                [*prefix, "ls-files"],
                check=True, capture_output=True, text=True, timeout=5,
            ).stdout.splitlines())
        except (OSError, subprocess.SubprocessError) as exc:
            raise EngineUnavailable("تعذّر التحقق من نسخة مصدر Kronos") from exc
        if head.casefold() != self._config.kronos_source_revision.casefold():
            raise EngineUnavailable("نسخة مصدر Kronos لا تطابق الإعداد المثبت")
        if dirty:
            raise EngineUnavailable("checkout Kronos يحتوي ملفات معدّلة أو غير متتبعة")
        unexpected: list[str] = []
        for path in repo_path.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(repo_path).as_posix()
            relative_parts = path.relative_to(repo_path).parts
            if (
                ".git" in relative_parts
                or "__pycache__" in relative_parts
                or path.suffix in {".pyc", ".pyo"}
            ):
                continue
            if relative not in tracked:
                unexpected.append(relative)
                if len(unexpected) >= 2:
                    break
        if unexpected:
            raise EngineUnavailable("ملفات غير مثبتة موجودة داخل checkout Kronos")

    def is_ready(self) -> bool:
        """يفحص المسار والاعتمادات بلا استيراد PyTorch أو تحميل الأوزان."""
        if self.loaded:
            return True
        now = monotonic()
        with self._readiness_lock:
            if (
                self._readiness_cached is not None
                and now - self._readiness_checked_at < _READINESS_CACHE_SECONDS
            ):
                return self._readiness_cached
            try:
                self._resolve_repo_path()
                ready = _runtime_dependencies_ready()
            except Exception:
                ready = False
            self._readiness_cached = ready
            self._readiness_checked_at = monotonic()
            return ready

    def _load(self) -> None:
        if self._predictor is not None:
            return
        repo_path = self._resolve_repo_path()
        if not _runtime_dependencies_ready():
            raise EngineUnavailable(
                "إصدارات اعتمادات Kronos لا تطابق manifest المثبت"
            )

        repo_text = str(repo_path)
        if repo_text not in sys.path:
            sys.path.insert(0, repo_text)
        try:
            model_module = importlib.import_module("model")
            module_path = Path(model_module.__file__ or "").resolve(strict=True)
            module_path.relative_to(repo_path)
            pandas = importlib.import_module("pandas")
            torch = importlib.import_module("torch")
            tokenizer = model_module.KronosTokenizer.from_pretrained(
                self._config.tokenizer_id,
                revision=self._config.tokenizer_revision,
            )
            model = model_module.Kronos.from_pretrained(
                self._config.model_id,
                revision=self._config.model_revision,
            )
            tokenizer.eval()
            model.eval()
            predictor = model_module.KronosPredictor(
                model,
                tokenizer,
                device=self._config.device,
                max_context=self._config.max_context,
            )
        except Exception as exc:
            raise EngineUnavailable("تعذّر تحميل Kronos أو أوزانه المثبتة") from exc

        self._pandas = pandas
        self._torch = torch
        self._predictor = predictor
        with self._readiness_lock:
            self._readiness_cached = True
            self._readiness_checked_at = monotonic()

    def forecast(self, request: ForecastRequest) -> dict[str, Any]:
        started = perf_counter()
        with self._inference_lock:
            self._load()
            assert self._pandas is not None
            assert self._torch is not None
            assert self._predictor is not None

            rows: list[dict[str, float]] = []
            for bar in request.bars:
                amount = bar.amount
                if amount is None:
                    typical_price = (bar.open + bar.high + bar.low + bar.close) / 4.0
                    amount = bar.volume * typical_price
                rows.append(
                    {
                        "open": bar.open,
                        "high": bar.high,
                        "low": bar.low,
                        "close": bar.close,
                        "volume": bar.volume,
                        "amount": amount,
                    }
                )

            frame = self._pandas.DataFrame.from_records(rows)
            history_timestamps = self._pandas.Series(
                [bar.model_timestamp for bar in request.bars]
            )
            future_timestamps = self._pandas.Series(
                [point.model_timestamp for point in request.future_timestamps]
            )

            try:
                with self._torch.inference_mode():
                    predicted = self._predictor.predict(
                        df=frame,
                        x_timestamp=history_timestamps,
                        y_timestamp=future_timestamps,
                        pred_len=request.pred_len,
                        T=1.0,
                        top_k=1,
                        top_p=1.0,
                        sample_count=1,
                        verbose=False,
                    )
            except Exception as exc:
                raise InferenceFailed("فشل استدلال Kronos") from exc

            if len(predicted.index) != request.pred_len or "close" not in predicted.columns:
                raise InferenceFailed("أعاد Kronos شكل توقع غير متوقع")
            base_close = request.bars[-1].close
            returns_pct: dict[str, float] = {}
            for horizon in request.horizons:
                predicted_close = float(predicted.iloc[horizon - 1]["close"])
                if not math.isfinite(predicted_close) or predicted_close <= 0:
                    raise InferenceFailed("أعاد Kronos سعر إغلاق غير صالح")
                value = ((predicted_close / base_close) - 1.0) * 100.0
                if not math.isfinite(value):
                    raise InferenceFailed("تعذّر حساب العائد المتوقع")
                returns_pct[str(horizon)] = round(value, 6)

        return {
            "status": "ok",
            "returns_pct": returns_pct,
            **self._identity(),
            "lookback": request.lookback,
            "pred_len": request.pred_len,
            "latency_ms": round((perf_counter() - started) * 1000.0, 2),
        }
