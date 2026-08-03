"""Run the scanner and loopback-only Kronos process in one Render worker."""

from __future__ import annotations

import json
import logging
import math
import os
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from time import monotonic
from types import FrameType
from typing import Callable, Mapping, Protocol, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from services.kronos_inference.config import (
    DEFAULT_KRONOS_SOURCE_REVISION,
    DEFAULT_MODEL_ID,
    DEFAULT_MODEL_REVISION,
    DEFAULT_TOKENIZER_ID,
    DEFAULT_TOKENIZER_REVISION,
)
from services.kronos_inference.runtime_layout import (
    RuntimeLayout,
    runtime_layout,
    snapshot_inventory,
)


logger = logging.getLogger(__name__)

_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})
_SAFE_SERVICE_ENV_KEYS = frozenset(
    {
        "HOME",
        "LANG",
        "LC_ALL",
        "PATH",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TMP",
        "TEMP",
        "TMPDIR",
        "TZ",
        "VIRTUAL_ENV",
        "PYTHONDONTWRITEBYTECODE",
        "PYTHONHASHSEED",
        "PYTHONUNBUFFERED",
        "KRONOS_DEVICE",
        "KRONOS_MARKET_TIMEZONE",
        "KRONOS_MAX_BODY_BYTES",
        "KRONOS_MAX_CONTEXT",
        "KRONOS_MAX_HORIZONS",
        "KRONOS_MAX_PRED_LEN",
        "KRONOS_MAX_REQUEST_THREADS",
        "KRONOS_MIN_LOOKBACK",
        "KRONOS_REQUEST_TIMEOUT_SECONDS",
    }
)


class ChildProcess(Protocol):
    pid: int
    returncode: int | None

    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...


ProcessFactory = Callable[[Sequence[str], Path, Mapping[str, str]], ChildProcess]
ReadinessProbe = Callable[[str, float], bool]


@dataclass(frozen=True)
class KronosHealth:
    forecast_in_progress: bool
    forecast_age_seconds: float | None


HealthProbe = Callable[[str, float], KronosHealth | None]


def _bool(values: Mapping[str, str], name: str, default: bool) -> bool:
    raw = values.get(name)
    if raw is None:
        return default
    normalized = raw.strip().casefold()
    if normalized in _TRUE:
        return True
    if normalized in _FALSE:
        return False
    raise ValueError(f"{name} يجب أن يكون true أو false")


def _bounded_float(
    values: Mapping[str, str],
    name: str,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    raw = values.get(name)
    try:
        value = default if raw is None else float(raw)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} يجب أن يكون رقمًا") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} يجب أن يكون بين {minimum:g} و{maximum:g}")
    return value


def _bounded_int(
    values: Mapping[str, str],
    name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    raw = values.get(name)
    try:
        value = default if raw is None else int(raw, 10)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} يجب أن يكون عددًا صحيحًا") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} يجب أن يكون بين {minimum} و{maximum}")
    return value


def _port(values: Mapping[str, str]) -> int:
    raw = values.get("KRONOS_LOCAL_PORT", "18080")
    try:
        port = int(raw, 10)
    except (TypeError, ValueError) as exc:
        raise ValueError("KRONOS_LOCAL_PORT يجب أن يكون عددًا صحيحًا") from exc
    if not 1024 <= port <= 65_535:
        raise ValueError("KRONOS_LOCAL_PORT يجب أن يكون بين 1024 و65535")
    keepalive = values.get("KEEPALIVE_PORT")
    if keepalive is not None:
        try:
            if port == int(keepalive, 10):
                raise ValueError("KRONOS_LOCAL_PORT يتعارض مع KEEPALIVE_PORT")
        except ValueError as exc:
            if "يتعارض" in str(exc):
                raise
    return port


def _loopback_no_proxy(environment: dict[str, str]) -> None:
    existing = environment.get("NO_PROXY") or environment.get("no_proxy") or ""
    entries = [part.strip() for part in existing.split(",") if part.strip()]
    for host in ("127.0.0.1", "localhost"):
        if host not in entries:
            entries.append(host)
    value = ",".join(entries)
    environment["NO_PROXY"] = value
    environment["no_proxy"] = value


def child_environments(
    values: Mapping[str, str],
    layout: RuntimeLayout,
) -> tuple[dict[str, str], dict[str, str], str]:
    """Build scanner and least-privilege inference environments."""

    port = _port(values)
    endpoint = f"http://127.0.0.1:{port}"

    scanner_environment = dict(values)
    scanner_environment["KRONOS_SERVICE_URL"] = endpoint
    scanner_environment.pop("KRONOS_SERVICE_TOKEN", None)
    _loopback_no_proxy(scanner_environment)

    service_environment = {
        key: value for key, value in values.items() if key in _SAFE_SERVICE_ENV_KEYS
    }
    service_environment.update(
        {
            "HF_HOME": str(layout.huggingface_home),
            "HF_HUB_DISABLE_TELEMETRY": "1",
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "KRONOS_HOST": "127.0.0.1",
            "KRONOS_PORT": str(port),
            "KRONOS_REPO_PATH": str(layout.kronos_repo_path),
            "KRONOS_SOURCE_REVISION": DEFAULT_KRONOS_SOURCE_REVISION,
            "KRONOS_MODEL_ID": DEFAULT_MODEL_ID,
            "KRONOS_MODEL_REVISION": DEFAULT_MODEL_REVISION,
            "KRONOS_TOKENIZER_ID": DEFAULT_TOKENIZER_ID,
            "KRONOS_TOKENIZER_REVISION": DEFAULT_TOKENIZER_REVISION,
            "OMP_NUM_THREADS": "1",
            "MKL_NUM_THREADS": "1",
            "OPENBLAS_NUM_THREADS": "1",
            "NUMEXPR_NUM_THREADS": "1",
        }
    )
    service_environment.pop("KRONOS_API_TOKEN", None)
    _loopback_no_proxy(service_environment)
    return scanner_environment, service_environment, endpoint


def validate_artifact(layout: RuntimeLayout) -> None:
    if not layout.manifest_path.is_file():
        raise RuntimeError("manifest بناء Kronos غير موجود")
    try:
        manifest = json.loads(layout.manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("manifest بناء Kronos غير صالح") from exc
    expected = {
        "schema": 2,
        "kronos_revision": DEFAULT_KRONOS_SOURCE_REVISION,
        "model": DEFAULT_MODEL_ID,
        "model_revision": DEFAULT_MODEL_REVISION,
        "tokenizer": DEFAULT_TOKENIZER_ID,
        "tokenizer_revision": DEFAULT_TOKENIZER_REVISION,
    }
    if any(manifest.get(key) != value for key, value in expected.items()):
        raise RuntimeError("manifest بناء Kronos لا يطابق المراجعات المثبتة")
    if not (layout.kronos_repo_path / "model" / "kronos.py").is_file():
        raise RuntimeError("مصدر Kronos غير موجود داخل build artifact")
    if not layout.huggingface_home.is_dir():
        raise RuntimeError("أوزان Kronos غير موجودة داخل build artifact")
    try:
        from huggingface_hub import snapshot_download

        hub_cache = layout.huggingface_home / "hub"
        snapshot_manifest = manifest.get("snapshots")
        if not isinstance(snapshot_manifest, dict):
            raise RuntimeError("manifest snapshots غير صالح")
        for label, repo_id, revision in (
            ("tokenizer", DEFAULT_TOKENIZER_ID, DEFAULT_TOKENIZER_REVISION),
            ("model", DEFAULT_MODEL_ID, DEFAULT_MODEL_REVISION),
        ):
            snapshot = snapshot_download(
                repo_id,
                revision=revision,
                cache_dir=str(hub_cache),
                local_files_only=True,
            )
            if not Path(snapshot).is_dir():
                raise RuntimeError("snapshot Kronos المثبت غير موجود")
            recorded = snapshot_manifest.get(label)
            if not isinstance(recorded, dict):
                raise RuntimeError("manifest snapshot ناقص")
            expected_files = recorded.get("files")
            actual_files = snapshot_inventory(Path(snapshot), layout.huggingface_home)
            if expected_files != actual_files:
                raise RuntimeError("بصمات ملفات snapshot لا تطابق build artifact")
    except Exception as exc:
        raise RuntimeError("أوزان Kronos المثبتة غير متاحة في وضع offline") from exc


def _spawn(command: Sequence[str], cwd: Path, environment: Mapping[str, str]) -> ChildProcess:
    return subprocess.Popen(
        list(command),
        cwd=str(cwd),
        env=dict(environment),
        close_fds=True,
    )


def _get_json(endpoint: str, path: str, timeout: float) -> dict[str, object] | None:
    request = Request(f"{endpoint}{path}", headers={"Accept": "application/json"})
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read(65_537)
            if len(raw) > 65_536 or response.status != 200:
                return None
    except (HTTPError, URLError, TimeoutError, OSError):
        return None
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return None
    return body if isinstance(body, dict) else None


def _ready(endpoint: str, timeout: float) -> bool:
    body = _get_json(endpoint, "/ready", timeout)
    if body is None:
        return False
    return (
        body.get("status") == "ready"
        and body.get("kronos_revision") == DEFAULT_KRONOS_SOURCE_REVISION
    )


def _health(endpoint: str, timeout: float) -> KronosHealth | None:
    body = _get_json(endpoint, "/health", timeout)
    if body is None:
        return None
    if (
        body.get("status") != "ok"
        or body.get("kronos_revision") != DEFAULT_KRONOS_SOURCE_REVISION
    ):
        return None
    in_progress = body.get("forecast_in_progress")
    age = body.get("forecast_age_seconds")
    if not isinstance(in_progress, bool):
        return None
    if not in_progress:
        return KronosHealth(False, None)
    if isinstance(age, bool) or not isinstance(age, (int, float)):
        return None
    parsed_age = float(age)
    if not math.isfinite(parsed_age) or parsed_age < 0:
        return None
    return KronosHealth(True, parsed_age)


def start_kronos(
    process_factory: ProcessFactory,
    project_root: Path,
    environment: Mapping[str, str],
) -> ChildProcess | None:
    try:
        return process_factory(
            (sys.executable, "-m", "services.kronos_inference"),
            project_root,
            environment,
        )
    except OSError:
        logger.exception("تعذّر إنشاء عملية Kronos؛ الماسح سيستمر")
        return None


def terminate_process(
    process: ChildProcess | None,
    name: str,
    timeout: float,
    *,
    kill_timeout: float = 5.0,
    deadline: float | None = None,
) -> None:
    if process is None or process.poll() is not None:
        return

    def remaining(requested: float) -> float:
        if deadline is None:
            return requested
        return max(0.05, min(requested, deadline - monotonic()))

    logger.info("إرسال SIGTERM إلى %s (pid=%d)", name, process.pid)
    process.terminate()
    try:
        process.wait(timeout=remaining(timeout))
    except subprocess.TimeoutExpired:
        logger.error("%s لم يتوقف خلال %.1fث؛ إرسال SIGKILL", name, timeout)
        process.kill()
        try:
            process.wait(timeout=remaining(kill_timeout))
        except subprocess.TimeoutExpired:
            logger.critical("%s لم يُحصد قبل انتهاء ميزانية الإيقاف", name)


def shutdown_children(
    scanner: ChildProcess | None,
    kronos: ChildProcess | None,
    *,
    scanner_timeout: float,
) -> None:
    # أبقِ Kronos حيًا أولًا حتى يفرغ scanner.shutdown() طابور Shadow. خدمات
    # Render ذات القرص تستخدم نافذة إيقاف ثابتة مدتها 30ث ولا تقبل تمديدها في
    # Blueprint؛ إذا استنفد الماسح مهلته نوقف Kronos لفك أي طلب HTTP عالق، ثم
    # نمنح الماسح فرصة أخيرة لحفظ النتيجة وإغلاق SQLite قبل SIGKILL.
    deadline = monotonic() + 25.0

    def remaining(requested: float) -> float:
        return max(0.05, min(requested, deadline - monotonic()))

    if scanner is None or scanner.poll() is not None:
        terminate_process(
            kronos,
            "kronos-inference",
            2.0,
            kill_timeout=1.0,
            deadline=deadline,
        )
        return

    logger.info("إرسال SIGTERM إلى runner-scanner (pid=%d)", scanner.pid)
    scanner.terminate()
    try:
        scanner.wait(timeout=remaining(scanner_timeout))
    except subprocess.TimeoutExpired:
        logger.warning(
            "runner-scanner لم يتوقف خلال %.1fث؛ إيقاف Kronos لفك الطلب الجاري",
            scanner_timeout,
        )
        terminate_process(
            kronos,
            "kronos-inference",
            2.0,
            kill_timeout=1.0,
            deadline=deadline,
        )
        kronos = None
        try:
            scanner.wait(timeout=remaining(5.0))
        except subprocess.TimeoutExpired:
            logger.error("runner-scanner لم يغلق SQLite؛ إرسال SIGKILL")
            scanner.kill()
            try:
                scanner.wait(timeout=remaining(1.0))
            except subprocess.TimeoutExpired:
                logger.critical(
                    "runner-scanner لم يُحصد قبل انتهاء ميزانية الإيقاف"
                )
        return
    terminate_process(
        kronos,
        "kronos-inference",
        2.0,
        kill_timeout=1.0,
        deadline=deadline,
    )


def run(
    values: Mapping[str, str] | None = None,
    *,
    process_factory: ProcessFactory = _spawn,
    readiness_probe: ReadinessProbe = _ready,
    health_probe: HealthProbe = _health,
    stop_event: threading.Event | None = None,
) -> int:
    environment = dict(os.environ if values is None else values)
    stop = stop_event or threading.Event()
    enabled = _bool(environment, "KRONOS_SHADOW_ENABLED", False)
    startup_timeout = _bounded_float(
        environment, "KRONOS_STARTUP_TIMEOUT_SEC", 60.0, 1.0, 300.0
    )
    restart_delay = _bounded_float(
        environment, "KRONOS_RESTART_DELAY_SEC", 5.0, 1.0, 300.0
    )
    health_interval = _bounded_float(
        environment, "KRONOS_HEALTH_INTERVAL_SEC", 10.0, 2.0, 60.0
    )
    hang_timeout = _bounded_float(
        environment, "KRONOS_HANG_TIMEOUT_SEC", 150.0, 10.0, 600.0
    )
    health_failure_limit = _bounded_int(
        environment, "KRONOS_HEALTH_FAILURE_LIMIT", 3, 1, 10
    )
    scanner_timeout = _bounded_float(
        environment, "KRONOS_SCANNER_SHUTDOWN_TIMEOUT_SEC", 15.0, 1.0, 20.0
    )
    layout = runtime_layout()
    scanner_env, service_env, endpoint = child_environments(environment, layout)
    scanner_env["KRONOS_SHADOW_ENABLED"] = "true" if enabled else "false"

    kronos: ChildProcess | None = None
    scanner: ChildProcess | None = None
    next_restart = 0.0
    kronos_started_at = 0.0
    next_readiness_probe = 0.0
    next_health_probe = 0.0
    kronos_ready = False
    health_failures = 0
    try:
        if enabled:
            try:
                validate_artifact(layout)
            except RuntimeError:
                # Shadow must never take the alerting loop down. The build step
                # normally catches this; this fallback protects a damaged artifact.
                logger.exception(
                    "Kronos artifact غير صالح؛ تشغيل الماسح مع تعطيل Shadow"
                )
                enabled = False
                scanner_env["KRONOS_SHADOW_ENABLED"] = "false"

        if enabled:
            kronos = start_kronos(process_factory, layout.project_root, service_env)
            now = monotonic()
            if kronos is None:
                next_restart = now + restart_delay
            else:
                kronos_started_at = now
                next_readiness_probe = now
                logger.info("KRONOS_LOCAL_STARTING endpoint=%s", endpoint)
        else:
            logger.warning("Kronos Shadow معطّل؛ تشغيل الماسح وحده")

        if stop.is_set():
            return 0
        scanner = process_factory(
            (sys.executable, "-m", "runner_scanner.main"),
            layout.project_root,
            scanner_env,
        )

        while not stop.wait(0.25):
            scanner_code = scanner.poll()
            if scanner_code is not None:
                logger.error("runner-scanner خرج بصورة غير متوقعة: code=%d", scanner_code)
                return scanner_code if scanner_code != 0 else 1

            if not enabled:
                continue
            if kronos is not None and kronos.poll() is not None:
                logger.error(
                    "Kronos خرج بصورة غير متوقعة: code=%s؛ الماسح مستمر",
                    kronos.returncode,
                )
                kronos = None
                next_restart = monotonic() + restart_delay
                kronos_ready = False
                health_failures = 0

            now = monotonic()
            if kronos is None:
                if now < next_restart:
                    continue
                kronos = start_kronos(
                    process_factory, layout.project_root, service_env
                )
                now = monotonic()
                if kronos is None:
                    next_restart = now + restart_delay
                    continue
                kronos_started_at = now
                next_readiness_probe = now
                kronos_ready = False
                health_failures = 0
                logger.info("KRONOS_LOCAL_RESTARTING endpoint=%s", endpoint)
                continue

            if not kronos_ready:
                if now < next_readiness_probe:
                    continue
                if readiness_probe(endpoint, 1.0):
                    kronos_ready = True
                    health_failures = 0
                    next_health_probe = monotonic() + health_interval
                    logger.info(
                        "KRONOS_LOCAL_READY endpoint=%s revision=%s",
                        endpoint,
                        DEFAULT_KRONOS_SOURCE_REVISION,
                    )
                    continue
                now = monotonic()
                if now - kronos_started_at >= startup_timeout:
                    logger.error(
                        "Kronos المحلي لم يصبح جاهزًا خلال %.1fث؛ إعادة تشغيل Shadow",
                        startup_timeout,
                    )
                    terminate_process(kronos, "kronos-inference", 5.0)
                    kronos = None
                    next_restart = monotonic() + restart_delay
                else:
                    next_readiness_probe = now + 0.5
                continue

            if now < next_health_probe:
                continue
            health = health_probe(endpoint, 1.5)
            now = monotonic()
            hung = False
            if health is None:
                health_failures += 1
                logger.warning(
                    "فشل فحص صحة Kronos المحلي (%d/%d)",
                    health_failures,
                    health_failure_limit,
                )
            else:
                health_failures = 0
                hung = (
                    health.forecast_in_progress
                    and health.forecast_age_seconds is not None
                    and health.forecast_age_seconds > hang_timeout
                )
            if hung or health_failures >= health_failure_limit:
                reason = "استدلال عالق" if hung else "فشل health متكرر"
                logger.error("إعادة تشغيل Kronos المحلي: %s؛ الماسح مستمر", reason)
                terminate_process(kronos, "kronos-inference", 5.0)
                kronos = None
                kronos_ready = False
                health_failures = 0
                next_restart = monotonic() + restart_delay
            else:
                next_health_probe = now + health_interval
        return 0
    finally:
        shutdown_children(scanner, kronos, scanner_timeout=scanner_timeout)


def main() -> int:
    logging.basicConfig(
        level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    stop_event = threading.Event()

    def _handle_signal(signum: int, _frame: FrameType | None) -> None:
        logger.info("استلم supervisor إشارة %s — بدء الإيقاف المرتب", signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)
    try:
        return run(stop_event=stop_event)
    except (OSError, RuntimeError, ValueError) as exc:
        logger.critical("تعذّر بدء worker الموحّد: %s", exc)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
