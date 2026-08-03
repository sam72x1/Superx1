"""تكامل Kronos بوضع الظل — لا يدخل في البوابات أو الدرجة.

هذي الوحدة لا تستورد PyTorch ولا حزمة Kronos. دورها محصور في تجهيز شموع
صالحة، وإرسالها إلى خدمة استدلال مستقلة، ثم حفظ النتيجة بأفضل جهد. أي عطل
في الشبكة أو النموذج أو التخزين يبقى معزولًا عن حلقة المسح الرئيسية.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import hashlib
from ipaddress import ip_address
import json
import logging
import math
from pathlib import Path
from queue import Empty, Full, Queue
import threading
import time
from typing import Any, Callable, Mapping, Optional, Sequence
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import requests

from .models import Bar

logger = logging.getLogger(__name__)


BAR_INTERVAL_MS = 5 * 60 * 1000
ABSOLUTE_MIN_LOOKBACK = 2
MIN_LOOKBACK = 32
MAX_LOOKBACK = 512
MAX_PRED_LEN = 120
DEFAULT_MAX_PAYLOAD_BYTES = 512 * 1024
DEFAULT_MAX_RESPONSE_BYTES = 256 * 1024
MARKET_TZ = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class KronosShadowResult:
    """نتيجة موحّدة وآمنة لنداء خدمة Kronos."""

    status: str
    model: str = ""
    model_revision: str = ""
    tokenizer: str = ""
    tokenizer_revision: str = ""
    kronos_revision: str = ""
    service_revision: str = ""
    market_timezone: str = ""
    device: str = ""
    max_context: Optional[int] = None
    returns_pct: dict[str, float] = field(default_factory=dict)
    horizons: tuple[int, ...] = ()
    lookback: Optional[int] = None
    pred_len: Optional[int] = None
    latency_ms: Optional[float] = None
    service_latency_ms: Optional[float] = None
    error: str = ""

    @property
    def ok(self) -> bool:
        """هل رجعت الخدمة توقعًا صالحًا؟"""
        return self.status == "ok" and not self.error

    @property
    def revision(self) -> str:
        """اسم مختصر متوافق لنسخة النموذج."""
        return self.model_revision

    @property
    def returns(self) -> dict[str, float]:
        """اسم مختصر متوافق لعوائد الآفاق."""
        return self.returns_pct


def _utc_datetime(value: Optional[datetime]) -> datetime:
    """تطبيع الوقت إلى UTC؛ الوقت بلا منطقة يُعامل كـUTC."""
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso_utc_from_ms(t_ms: int) -> str:
    dt = datetime.fromtimestamp(t_ms / 1000.0, tz=timezone.utc)
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _finite_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _normalise_horizons(horizons: Sequence[int], pred_len: int) -> tuple[int, ...]:
    out: set[int] = set()
    try:
        values = iter(horizons)
    except TypeError:
        return ()
    for raw in values:
        try:
            value = int(raw)
        except (TypeError, ValueError, OverflowError):
            continue
        if 0 < value <= pred_len:
            out.add(value)
    return tuple(sorted(out))


def _validate_service_url(value: str) -> tuple[str, str]:
    """يرجع الرابط المطبّع أو خطأ آمن؛ HTTP مسموح للـloopback فقط."""
    candidate = str(value or "").strip().rstrip("/")
    if not candidate:
        return "", "رابط خدمة Kronos غير مضبوط"
    try:
        parsed = urlsplit(candidate)
        host = parsed.hostname or ""
        if (
            parsed.scheme not in {"http", "https"}
            or not host
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            return "", "رابط خدمة Kronos غير صالح"
        if parsed.scheme == "http":
            is_loopback = host.casefold() == "localhost"
            if not is_loopback:
                try:
                    is_loopback = ip_address(host).is_loopback
                except ValueError:
                    is_loopback = False
            if not is_loopback:
                return "", "HTTPS إلزامي لخدمة Kronos غير المحلية"
    except (TypeError, ValueError):
        return "", "رابط خدمة Kronos غير صالح"
    return candidate, ""


def kronos_experiment_id(
    *,
    model: str,
    model_revision: str,
    tokenizer: str,
    tokenizer_revision: str,
    kronos_revision: str,
    service_revision: str,
    market_timezone: str,
    device: str,
    max_context: int,
    client_revision: str,
    lookback: int,
    pred_len: int,
    horizons: Sequence[int],
    attempt_kind: str = "",
) -> str:
    """هوية ثابتة تمنع خلط أو استبدال تجارب بإعدادات مختلفة."""
    identity = {
        "model": str(model),
        "model_revision": str(model_revision),
        "tokenizer": str(tokenizer),
        "tokenizer_revision": str(tokenizer_revision),
        "kronos_revision": str(kronos_revision),
        "service_revision": str(service_revision),
        "market_timezone": str(market_timezone),
        "device": str(device),
        "max_context": int(max_context),
        "client_revision": str(client_revision),
        "lookback": int(lookback),
        "pred_len": int(pred_len),
        "horizons": list(sorted(int(value) for value in horizons)),
        # نجاحات التجربة تبقى في cohort واحد. أما الأحداث التشغيلية غير
        # الناجحة فتحتاج نوعًا ثابتًا كي لا يستبدل queue_full حدث إيقاف/خطأ
        # لاحقًا لنفس الرمز والإغلاق عبر مفتاح SQLite نفسه.
        "attempt_kind": str(attempt_kind),
    }
    encoded = json.dumps(
        identity, ensure_ascii=True, allow_nan=False,
        sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def kronos_client_revision(
    *, max_context_lag_sec: float, observation_grace_min: float,
    selection_revision: str,
) -> str:
    """بصمة كود تجهيز/قياس العميل وقيم بروتوكول الحقيقة الفعلية.

    تضم الوحدة الحالية ومنطق تثبيت actuals في ``state.py``، إضافةً إلى القيم
    القابلة للضبط التي تغيّر انتقاء العينة أو label. أي تغيير فيها يفتح cohort
    جديدًا بدل خلط نتائج غير متجانسة.
    """
    package_root = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for filename in (
        "kronos_shadow.py",
        "state.py",
        "massive_client.py",
        "models.py",
        "main.py",
        "config.py",
    ):
        path = package_root / filename
        body = path.read_bytes()
        name = filename.encode("utf-8")
        digest.update(len(name).to_bytes(4, "big"))
        digest.update(name)
        digest.update(len(body).to_bytes(8, "big"))
        digest.update(body)
    policy = json.dumps(
        {
            "bar_interval_ms": BAR_INTERVAL_MS,
            "max_context_lag_sec": float(max_context_lag_sec),
            "observation_grace_min": float(observation_grace_min),
            "selection_revision": str(selection_revision),
        },
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    digest.update(policy)
    return digest.hexdigest()


def kronos_selection_revision(config: Any) -> str:
    """بصمة محافظة لكود اختيار المرشح وقيم Config الفعلية بلا أسرار.

    إعادة ضبط cohort عند تغيير بوابة/درجة أو cadence أكثر أمانًا علميًا من
    نسبة نتائج توزيعين مختلفين إلى التجربة نفسها. قيم الأسرار والمسارات لا
    تدخل البصمة؛ يُسجّل فقط كون الاعتماد مضبوطًا عند احتمال تأثيره.
    """
    package_root = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for path in sorted(package_root.glob("*.py")):
        name = path.name.encode("utf-8")
        body = path.read_bytes()
        digest.update(len(name).to_bytes(4, "big"))
        digest.update(name)
        digest.update(len(body).to_bytes(8, "big"))
        digest.update(body)

    values = vars(config) if hasattr(config, "__dict__") else {}
    protected_markers = (
        "key", "token", "secret", "chat_id", "owner_id", "user_agent",
        "db_path", "save_dir", "service_id",
    )
    safe_values: dict[str, Any] = {}
    for name, value in sorted(values.items()):
        if any(marker in name.casefold() for marker in protected_markers):
            safe_values[name] = {"configured": bool(value)}
        else:
            safe_values[name] = value
    encoded = json.dumps(
        safe_values,
        ensure_ascii=True,
        allow_nan=False,
        default=str,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest.update(encoded)
    return digest.hexdigest()


def _bar_record(bar: Bar, now_ms: int) -> Optional[dict[str, Any]]:
    """تحويل شمعة مكتملة وصالحة إلى سجل Kronos، وإلا ``None``."""
    try:
        t_ms = int(bar.t_ms)
    except (TypeError, ValueError, OverflowError):
        return None
    if t_ms <= 0 or t_ms + BAR_INTERVAL_MS > now_ms:
        return None

    o = _finite_float(bar.o)
    h = _finite_float(bar.h)
    low = _finite_float(bar.l)
    c = _finite_float(bar.c)
    volume = _finite_float(bar.v)
    if None in (o, h, low, c, volume):
        return None
    assert o is not None and h is not None and low is not None
    assert c is not None and volume is not None
    if min(o, h, low, c) <= 0 or volume < 0:
        return None
    if h < max(o, c) or low > min(o, c) or h < low:
        return None

    vwap = _finite_float(bar.vw)
    price_for_amount = vwap if vwap is not None and vwap > 0 else (h + low + c) / 3.0
    amount = volume * price_for_amount
    if not math.isfinite(amount) or amount < 0:
        return None
    return {
        "timestamp": _iso_utc_from_ms(t_ms),
        "open": o,
        "high": h,
        "low": low,
        "close": c,
        "volume": volume,
        "amount": amount,
    }


def prepare_forecast_payload(
    ticker: str,
    bars: Sequence[Bar],
    *,
    now: Optional[datetime] = None,
    lookback: int = 512,
    min_lookback: int = MIN_LOOKBACK,
    pred_len: int = 18,
    horizons: Sequence[int] = (6, 12, 18),
) -> Optional[dict[str, Any]]:
    """تجهيز عقد ``/v1/forecast`` من شموع 5 دقائق.

    تُستبعد الشمعة الجارية والقيم غير المحدودة أو غير المنطقية، وتُرتّب
    الشموع زمنيًا ثم يؤخذ آخر ``lookback`` فقط. ``None`` تعني أن البيانات لا
    تكفي لنداء آمن، وليست سببًا لإيقاف الماسح.
    """
    try:
        clean_ticker = str(ticker).strip().upper()
        requested_lookback = int(lookback)
        requested_min_lookback = int(min_lookback)
        requested_pred_len = int(pred_len)
    except (TypeError, ValueError, OverflowError):
        return None
    if not clean_ticker or len(clean_ticker) > 32:
        return None
    if not ABSOLUTE_MIN_LOOKBACK <= requested_min_lookback <= MAX_LOOKBACK:
        return None
    if not requested_min_lookback <= requested_lookback <= MAX_LOOKBACK:
        return None
    if not 0 < requested_pred_len <= MAX_PRED_LEN:
        return None

    clean_horizons = _normalise_horizons(horizons, requested_pred_len)
    if not clean_horizons:
        return None

    now_ms = int(_utc_datetime(now).timestamp() * 1000)
    # عند تكرار الطابع نأخذ آخر نسخة صالحة وصلتنا من المزوّد.
    by_timestamp: dict[str, dict[str, Any]] = {}
    try:
        for bar in bars:
            record = _bar_record(bar, now_ms)
            if record is not None:
                by_timestamp[record["timestamp"]] = record
    except (TypeError, AttributeError):
        return None
    clean_bars = sorted(by_timestamp.values(), key=lambda row: row["timestamp"])
    clean_bars = clean_bars[-requested_lookback:]
    if len(clean_bars) < requested_min_lookback:
        return None

    last_ms = int(
        datetime.fromisoformat(clean_bars[-1]["timestamp"].replace("Z", "+00:00"))
        .timestamp() * 1000
    )
    future_timestamps = [
        _iso_utc_from_ms(last_ms + BAR_INTERVAL_MS * step)
        for step in range(1, requested_pred_len + 1)
    ]
    return {
        "ticker": clean_ticker,
        "bars": clean_bars,
        "future_timestamps": future_timestamps,
        "horizons": list(clean_horizons),
    }


def five_minute_close_ms(now: Optional[datetime] = None) -> int:
    """طابع آخر إغلاق 5 دقائق لاستخدامه في منع التكرار."""
    now_ms = int(_utc_datetime(now).timestamp() * 1000)
    return (now_ms // BAR_INTERVAL_MS) * BAR_INTERVAL_MS


def kronos_dedupe_key(ticker: str, now: Optional[datetime] = None) -> str:
    """مفتاح ثابت لنفس الرمز ونفس إغلاق الخمس دقائق."""
    return f"{str(ticker).strip().upper()}:{five_minute_close_ms(now)}"


def should_run_on_5m_close(
    ticker: str,
    last_key: str,
    now: Optional[datetime] = None,
) -> bool:
    """منع دورة الـ45 ثانية من تكرار توقع الشمعة نفسها."""
    clean_ticker = str(ticker).strip()
    return bool(clean_ticker) and kronos_dedupe_key(clean_ticker, now) != last_key


class KronosShadowClient:
    """عميل HTTP محدود وآمن لخدمة الاستدلال المنفصلة."""

    def __init__(
        self,
        base_url: str,
        *,
        token: str = "",
        timeout: float = 10.0,
        max_payload_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        session: Optional[requests.Session] = None,
        clock: Callable[[], float] = time.perf_counter,
    ):
        self.base_url, self._base_url_error = _validate_service_url(base_url)
        self.token = str(token or "").strip()
        timeout_value = float(timeout)
        if not math.isfinite(timeout_value) or timeout_value <= 0:
            raise ValueError("مهلة Kronos يجب أن تكون رقمًا موجبًا محدودًا")
        self.timeout = timeout_value
        self.max_payload_bytes = max(1, int(max_payload_bytes))
        self.max_response_bytes = max(1, int(max_response_bytes))
        self._http = session or requests.Session()
        self._clock = clock

    @staticmethod
    def _error(message: str, latency_ms: Optional[float] = None) -> KronosShadowResult:
        return KronosShadowResult(
            status="error",
            latency_ms=latency_ms,
            error=str(message)[:500],
        )

    def forecast(self, payload: Optional[Mapping[str, Any]]) -> KronosShadowResult:
        """إرسال توقع واحد؛ يرجع نتيجة آمنة عند أي فشل ولا يرمي استثناء."""
        try:
            started = self._clock()
        except Exception:
            started = time.perf_counter()

        def elapsed() -> float:
            try:
                return max(0.0, (self._clock() - started) * 1000.0)
            except Exception:  # pragma: no cover - حزام أمان لساعة محقونة تالفة
                return 0.0

        try:
            if self._base_url_error:
                return self._error(self._base_url_error, elapsed())
            if not isinstance(payload, Mapping):
                return self._error("حمولة Kronos غير صالحة", elapsed())
            body = dict(payload)
            encoded = json.dumps(
                body, ensure_ascii=False, allow_nan=False, separators=(",", ":")
            ).encode("utf-8")
            if len(encoded) > self.max_payload_bytes:
                return self._error("حمولة Kronos تجاوزت الحد المسموح", elapsed())

            headers = {"Content-Type": "application/json"}
            if self.token:
                headers["Authorization"] = f"Bearer {self.token}"
            response = self._http.post(
                f"{self.base_url}/v1/forecast",
                json=body,
                headers=headers,
                timeout=self.timeout,
                allow_redirects=False,
                stream=True,
            )
            try:
                status_code = int(getattr(response, "status_code", 0) or 0)
                if status_code < 200 or status_code >= 300:
                    return self._error(
                        f"خدمة Kronos أعادت HTTP {status_code or 'غير معروف'}",
                        elapsed(),
                    )
                data, response_error = self._bounded_json_response(response)
                if response_error:
                    return self._error(response_error, elapsed())
                return self._parse_response(data, body, elapsed())
            finally:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
        except (requests.RequestException, TypeError, ValueError, OverflowError) as exc:
            return self._error(f"تعذّر اتصال Kronos: {exc}", elapsed())
        except Exception as exc:  # best-effort: تكامل الظل لا يسقط الماسح أبدًا
            logger.debug("فشل غير متوقع في عميل Kronos", exc_info=True)
            return self._error(f"تعذّر توقع Kronos: {exc}", elapsed())

    def _bounded_json_response(self, response: Any) -> tuple[Any, str]:
        """قراءة streaming بحد صلب يمنع ردًا ضخمًا من إسقاط الماسح."""
        headers = getattr(response, "headers", {}) or {}
        raw_length = headers.get("Content-Length") if hasattr(headers, "get") else None
        if raw_length not in (None, ""):
            try:
                content_length = int(str(raw_length), 10)
            except (TypeError, ValueError, OverflowError):
                return None, "رد Kronos يحمل Content-Length غير صالح"
            if content_length < 0:
                return None, "رد Kronos يحمل Content-Length غير صالح"
            if content_length > self.max_response_bytes:
                return None, "رد Kronos تجاوز الحد المسموح"

        iterator = getattr(response, "iter_content", None)
        if callable(iterator):
            raw = bytearray()
            for chunk in iterator(chunk_size=64 * 1024):
                if not chunk:
                    continue
                if isinstance(chunk, str):
                    chunk = chunk.encode("utf-8")
                if not isinstance(chunk, (bytes, bytearray)):
                    return None, "رد Kronos ليس بيانات صالحة"
                if len(raw) + len(chunk) > self.max_response_bytes:
                    return None, "رد Kronos تجاوز الحد المسموح"
                raw.extend(chunk)
            try:
                return json.loads(
                    raw.decode("utf-8"),
                    parse_constant=lambda value: (_ for _ in ()).throw(
                        ValueError(f"non-finite {value}")),
                ), ""
            except (UnicodeDecodeError, ValueError, RecursionError):
                return None, "رد Kronos ليس JSON صالحًا"

        # توافق مع جلسات الاختبار/المحولات القديمة فقط؛ requests الحقيقي يوفّر
        # iter_content عند ``stream=True``.
        try:
            data = response.json()
            encoded = json.dumps(data, allow_nan=False).encode("utf-8")
        except (AttributeError, TypeError, ValueError, RecursionError):
            return None, "رد Kronos ليس JSON صالحًا"
        if len(encoded) > self.max_response_bytes:
            return None, "رد Kronos تجاوز الحد المسموح"
        return data, ""

    def _parse_response(
        self,
        data: Any,
        payload: Mapping[str, Any],
        latency_ms: float,
    ) -> KronosShadowResult:
        if not isinstance(data, Mapping):
            return self._error("رد Kronos ليس كائنًا", latency_ms)
        if data.get("status") != "ok":
            remote_error = data.get("error")
            if isinstance(remote_error, Mapping):
                remote_error = remote_error.get("message") or remote_error.get("code")
            remote_error = str(remote_error or "الخدمة لم تُرجع حالة نجاح")
            return self._error(f"فشل Kronos: {remote_error}", latency_ms)

        model = data.get("model")
        model_revision = data.get("model_revision")
        tokenizer = data.get("tokenizer")
        tokenizer_revision = data.get("tokenizer_revision")
        kronos_revision = data.get("kronos_revision")
        service_revision = data.get("service_revision")
        market_timezone = data.get("market_timezone")
        device = data.get("device")
        if not all(isinstance(v, str) and v.strip()
                   for v in (
                       model, model_revision, tokenizer, tokenizer_revision,
                       kronos_revision, service_revision, market_timezone, device,
                   )):
            return self._error("رد Kronos يفتقد هوية النموذج أو نسخه", latency_ms)

        raw_returns = data.get("returns_pct")
        if not isinstance(raw_returns, Mapping):
            return self._error("رد Kronos يفتقد عوائد الآفاق", latency_ms)
        requested_horizons = _normalise_horizons(
            payload.get("horizons") or (),
            len(payload.get("future_timestamps") or ()),
        )
        returns_pct: dict[str, float] = {}
        for horizon in requested_horizons:
            raw_value = raw_returns.get(str(horizon))
            value = _finite_float(raw_value)
            if value is None:
                return self._error(
                    f"رد Kronos يفتقد عائد الأفق {horizon}", latency_ms
                )
            returns_pct[str(horizon)] = value
        if not returns_pct:
            return self._error("رد Kronos لا يحتوي آفاقًا صالحة", latency_ms)

        try:
            lookback = int(data.get("lookback"))
            pred_len = int(data.get("pred_len"))
            max_context = int(data.get("max_context"))
        except (TypeError, ValueError, OverflowError):
            return self._error("رد Kronos يفتقد أطوال الإدخال والتوقع", latency_ms)
        if lookback != len(payload.get("bars") or ()):
            return self._error("طول سياق Kronos في الرد لا يطابق الطلب", latency_ms)
        if pred_len != len(payload.get("future_timestamps") or ()):
            return self._error("طول توقع Kronos في الرد لا يطابق الطلب", latency_ms)
        if max_context < lookback or max_context > MAX_LOOKBACK:
            return self._error("سعة سياق Kronos في الرد غير صالحة", latency_ms)
        service_latency = _finite_float(data.get("latency_ms"))
        if service_latency is None or service_latency < 0:
            return self._error("زمن استدلال Kronos غير صالح", latency_ms)

        return KronosShadowResult(
            status="ok",
            model=model.strip(),
            model_revision=model_revision.strip(),
            tokenizer=tokenizer.strip(),
            tokenizer_revision=tokenizer_revision.strip(),
            kronos_revision=kronos_revision.strip(),
            service_revision=service_revision.strip(),
            market_timezone=market_timezone.strip(),
            device=device.strip(),
            max_context=max_context,
            returns_pct=returns_pct,
            horizons=requested_horizons,
            lookback=lookback,
            pred_len=pred_len,
            latency_ms=latency_ms,
            service_latency_ms=service_latency,
        )


@dataclass(frozen=True)
class _ShadowTask:
    ticker: str
    requested_at: datetime
    close_ms: int
    session: str = ""
    session_end_at: Optional[datetime] = None


class KronosShadowWorker:
    """عامل خلفي محدود يمنع استدلال Kronos من تعطيل دورة المسح."""

    def __init__(
        self,
        *,
        client: KronosShadowClient,
        massive_client_factory: Callable[[], Any],
        store: Any,
        context_days: int = 7,
        min_lookback: int = MIN_LOOKBACK,
        lookback: int = 512,
        pred_len: int = 18,
        horizons: Sequence[int] = (6, 12, 18),
        queue_size: int = 16,
        max_context_lag_sec: float = 360.0,
        observation_grace_min: float = 3.0,
        selection_revision: str = "unspecified",
        now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ):
        self.client = client
        self.massive_client_factory = massive_client_factory
        self.store = store
        self.context_days = max(1, min(int(context_days), 366))
        self.min_lookback = max(
            ABSOLUTE_MIN_LOOKBACK, min(int(min_lookback), MAX_LOOKBACK))
        self.lookback = max(
            self.min_lookback, min(int(lookback), MAX_LOOKBACK))
        self.pred_len = max(1, min(int(pred_len), MAX_PRED_LEN))
        self.horizons = _normalise_horizons(horizons, self.pred_len)
        if not self.horizons:
            raise ValueError("KRONOS_HORIZONS لا يحتوي أفقًا داخل pred_len")
        context_lag = float(max_context_lag_sec)
        if not math.isfinite(context_lag) or context_lag < 0:
            raise ValueError("KRONOS_MAX_CONTEXT_LAG_SEC غير صالح")
        self.max_context_lag_sec = context_lag
        observation_grace = float(observation_grace_min)
        if not math.isfinite(observation_grace) or not 0 <= observation_grace < 5:
            raise ValueError("KRONOS_OBSERVATION_GRACE_MIN يجب أن يكون بين 0 وأقل من 5")
        self.observation_grace_min = observation_grace
        self.client_revision = kronos_client_revision(
            max_context_lag_sec=self.max_context_lag_sec,
            observation_grace_min=self.observation_grace_min,
            selection_revision=str(selection_revision or "unspecified"),
        )
        queue_capacity = max(1, int(queue_size))
        self._queue: Queue[_ShadowTask] = Queue(maxsize=queue_capacity)
        # سجلات ضغط الطابور تُحفَظ خارج submit حتى لا ينتظر خيط الماسح
        # SQLite. الطابور الثاني محدود أيضًا كي تبقى الذاكرة مضبوطة عند
        # استمرار بطء التخزين؛ أي فقد فيه يظهر صراحةً في runtime_stats.
        self._audit_queue: Queue[_ShadowTask] = Queue(maxsize=queue_capacity)
        self._now_fn = now_fn
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._last_submitted: dict[str, int] = {}
        self._last_audited: dict[str, int] = {}
        self._last_forecast_close: dict[str, int] = {}
        self._runtime_counts = {
            "enqueued": 0,
            "duplicate": 0,
            "queue_full": 0,
            "audit_duplicate": 0,
            "audit_dropped": 0,
            "rejected_stopped": 0,
            "invalid_ticker": 0,
            "stale_before_fetch": 0,
            "handled": 0,
            "discarded_on_stop": 0,
            "save_failed": 0,
        }

    def start(self) -> None:
        """تشغيل العامل مرة واحدة؛ النداءات المتكررة آمنة."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="kronos-shadow",
                daemon=True,
            )
            self._thread.start()

    @property
    def is_alive(self) -> bool:
        """هل خيط العامل يعمل حاليًا؟"""
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    @property
    def runtime_stats(self) -> dict[str, int | bool]:
        """عدادات best-effort منذ إقلاع العملية لكشف ضغط الطابور."""
        with self._lock:
            return {
                **self._runtime_counts,
                "queue_depth": self._queue.qsize(),
                "queue_capacity": self._queue.maxsize,
                "audit_queue_depth": self._audit_queue.qsize(),
                "audit_queue_capacity": self._audit_queue.maxsize,
                "worker_alive": (
                    self._thread is not None and self._thread.is_alive()
                ),
            }

    def _bump_runtime(self, key: str, amount: int = 1) -> None:
        with self._lock:
            self._runtime_counts[key] += amount

    def submit(
        self,
        ticker: str,
        *,
        session: str = "",
        session_end_at: Optional[datetime] = None,
        now: Optional[datetime] = None,
    ) -> bool:
        """إضافة رمز إن لم يُرسَل لنفس إغلاق 5د؛ الامتلاء يعني تخطّي آمن."""
        clean_ticker = str(ticker or "").strip().upper()
        if not clean_ticker:
            self._bump_runtime("invalid_ticker")
            return False
        requested_at = _utc_datetime(now if now is not None else self._now_fn())
        close_ms = five_minute_close_ms(requested_at)
        session_end = (
            _utc_datetime(session_end_at) if session_end_at is not None else None
        )
        task = _ShadowTask(
            clean_ticker,
            requested_at,
            close_ms,
            str(session or "").strip(),
            session_end,
        )
        with self._lock:
            if self._stop.is_set():
                self._runtime_counts["rejected_stopped"] += 1
                return False
            if self._last_submitted.get(clean_ticker, -1) >= close_ms:
                self._runtime_counts["duplicate"] += 1
                return False
            try:
                self._queue.put_nowait(task)
            except Full:
                self._runtime_counts["queue_full"] += 1
                if self._last_audited.get(clean_ticker, -1) >= close_ms:
                    self._runtime_counts["audit_duplicate"] += 1
                else:
                    try:
                        self._audit_queue.put_nowait(task)
                    except Full:
                        self._runtime_counts["audit_dropped"] += 1
                        self._runtime_counts["save_failed"] += 1
                    else:
                        # dedupe مستقل عن _last_submitted كي تبقى إعادة محاولة
                        # inference لنفس الإغلاق ممكنة إذا انخفض الضغط لاحقًا.
                        self._last_audited[clean_ticker] = close_ms
            else:
                self._last_submitted[clean_ticker] = close_ms
                self._runtime_counts["enqueued"] += 1
                return True

        logger.debug("طابور Kronos ممتلئ؛ تخطّي %s", clean_ticker)
        return False

    def stop(self, timeout: Optional[float] = None) -> None:
        """إنهاء الجارية وحفظ المنتظر كتجاوز دائم، ثم انتظار توقف الخيط."""
        discarded: list[_ShadowTask] = []
        with self._lock:
            self._stop.set()
            # القفل يمنع submit من الإضافة بعد التفريغ وقبل ملاحظة حالة التوقف.
            while True:
                try:
                    task = self._queue.get_nowait()
                except Empty:
                    break
                else:
                    self._runtime_counts["discarded_on_stop"] += 1
                    discarded.append(task)
                    self._queue.task_done()
            thread = self._thread
        for task in discarded:
            self._save_stopped_task(task)
        if thread is not None and thread is not threading.current_thread():
            safe_timeout = None if timeout is None else max(0.0, float(timeout))
            thread.join(safe_timeout)
        # عند عدم تشغيل الخيط (شائع في الاختبارات/الإقلاع الفاشل) أو بعد
        # توقفه، لا نترك سجلات التدقيق معلقة. الحجب هنا مقبول لأنه مسار إيقاف.
        if thread is None or not thread.is_alive():
            while self._drain_one_audit():
                pass

    def _run(self) -> None:
        massive_client: Any = None
        while True:
            try:
                # المهمة المقبولة أولى من telemetry؛ بطء SQLite في سجل امتلاء
                # لا يجوز أن يجعل استدلالًا موجودًا في الطابور يتقادم.
                task = self._queue.get_nowait()
            except Empty:
                if self._drain_one_audit():
                    continue
                if self._stop.is_set():
                    return
                try:
                    task = self._queue.get(timeout=0.1)
                except Empty:
                    continue
            if self._stop.is_set():
                self._bump_runtime("discarded_on_stop")
                self._save_stopped_task(task)
                self._queue.task_done()
                continue
            try:
                if massive_client is None:
                    massive_client = self.massive_client_factory()
                self._process(task, massive_client)
            except Exception as exc:  # كل مهمة معزولة عن التالية
                logger.debug("فشلت مهمة Kronos لـ %s", task.ticker, exc_info=True)
                self._save(
                    task,
                    KronosShadowResult(status="error", error=f"تعذّرت مهمة Kronos: {exc}"),
                    lookback=self.lookback,
                    pred_len=self.pred_len,
                )
                # فشل إنشاء العميل المستقل قد يكون عابرًا؛ أعد المحاولة بالمهمة التالية.
                massive_client = None
            finally:
                self._bump_runtime("handled")
                self._queue.task_done()

    def _drain_one_audit(self) -> bool:
        """حفظ سجل queue_full واحد من خيط الخلفية، إن وجد."""
        try:
            task = self._audit_queue.get_nowait()
        except Empty:
            return False
        try:
            self._save_queue_full_task(task)
        finally:
            self._audit_queue.task_done()
        return True

    def _save_queue_full_task(self, task: _ShadowTask) -> None:
        saved = self._save(
            task,
            KronosShadowResult(
                status="skipped",
                error="queue_full: طابور Kronos ممتلئ؛ لم يُرسل المرشح إلى الاستدلال",
            ),
            lookback=self.lookback,
            pred_len=self.pred_len,
            asof_at=datetime.fromtimestamp(
                task.close_ms / 1000.0, tz=timezone.utc
            ),
            received_at=task.requested_at,
        )
        if not saved:
            # اسمح بمحاولة تدقيق لاحقة لنفس الفرصة عند تعافي التخزين.
            with self._lock:
                if self._last_audited.get(task.ticker) == task.close_ms:
                    self._last_audited.pop(task.ticker, None)

    def _save_stopped_task(self, task: _ShadowTask) -> None:
        try:
            asof_at = datetime.fromtimestamp(
                task.close_ms / 1000.0, tz=timezone.utc
            )
            received_at = _utc_datetime(self._now_fn())
        except Exception:
            self._bump_runtime("save_failed")
            logger.debug(
                "تعذّر تجهيز سجل إيقاف Kronos لـ%s", task.ticker,
                exc_info=True,
            )
            return
        self._save(
            task,
            KronosShadowResult(
                status="skipped",
                error="worker_stopped: توقف عامل Kronos قبل الاستدلال",
            ),
            lookback=self.lookback,
            pred_len=self.pred_len,
            asof_at=asof_at,
            received_at=received_at,
        )

    def _process(self, task: _ShadowTask, massive_client: Any) -> None:
        decision_cutoff = datetime.fromtimestamp(
            task.close_ms / 1000.0, tz=timezone.utc
        )
        dequeued_at = _utc_datetime(self._now_fn())
        queue_lag_sec = (dequeued_at - decision_cutoff).total_seconds()
        if queue_lag_sec < 0 or queue_lag_sec > self.max_context_lag_sec:
            # افصل انهيار backlog مبكرًا: لا نطلب bars لمهمة انتهت صلاحيتها.
            self._bump_runtime("stale_before_fetch")
            self._save(
                task,
                KronosShadowResult(
                    status="skipped",
                    error="انتهت نافذة حداثة Kronos داخل الطابور قبل جلب السياق",
                ),
                lookback=self.lookback,
                pred_len=self.pred_len,
                asof_at=decision_cutoff,
                received_at=dequeued_at,
            )
            return
        requested_market = task.requested_at.astimezone(MARKET_TZ)
        start = (
            requested_market - timedelta(days=self.context_days)
        ).date().isoformat()
        end = requested_market.date().isoformat()
        bars = massive_client.bars_5min(task.ticker, start, end)
        retrieved_at = _utc_datetime(self._now_fn())
        payload = prepare_forecast_payload(
            task.ticker,
            bars,
            # لا نسمح لانتظار الطابور بإدخال شموع ظهرت بعد قرار Superx1.
            now=decision_cutoff,
            lookback=self.lookback,
            # نجاحات التجربة تستعمل طولًا ثابتًا؛ وإلا يصبح كل سهم رقيق
            # cohort مختلفًا ويقفز التقرير إلى عينة صغيرة عشوائية.
            min_lookback=self.lookback,
            pred_len=self.pred_len,
            horizons=self.horizons,
        )
        if payload is None:
            result = KronosShadowResult(
                status="skipped",
                error="لا توجد شموع مكتملة وصالحة لـKronos",
            )
            self._save(
                task, result, lookback=self.lookback, pred_len=self.pred_len,
                received_at=retrieved_at)
            return
        last_bar_start = datetime.fromisoformat(
            payload["bars"][-1]["timestamp"].replace("Z", "+00:00"))
        forecast_asof = last_bar_start + timedelta(milliseconds=BAR_INTERVAL_MS)
        forecast_close_ms = int(forecast_asof.timestamp() * 1000)
        decision_at = _utc_datetime(self._now_fn())
        if forecast_close_ms != task.close_ms:
            self._save(
                task,
                KronosShadowResult(
                    status="skipped",
                    error="لم يصل إغلاق 5د المطابق لوقت قرار Superx1",
                ),
                lookback=len(payload["bars"]),
                pred_len=len(payload["future_timestamps"]),
                base_close=float(payload["bars"][-1]["close"]),
                asof_at=forecast_asof,
                received_at=decision_at,
            )
            return
        context_lag_sec = (decision_at - forecast_asof).total_seconds()
        if context_lag_sec < 0 or context_lag_sec > self.max_context_lag_sec:
            logger.debug(
                "سياق Kronos قديم %.1fث لـ%s؛ تخطّي بدل توقع رجعي",
                context_lag_sec, task.ticker)
            self._save(
                task,
                KronosShadowResult(
                    status="skipped",
                    error="سياق Kronos خارج نافذة الحداثة المسموحة",
                ),
                lookback=len(payload["bars"]),
                pred_len=len(payload["future_timestamps"]),
                base_close=float(payload["bars"][-1]["close"]),
                asof_at=forecast_asof,
                received_at=decision_at,
            )
            return
        last_target_at = forecast_asof + timedelta(
            milliseconds=BAR_INTERVAL_MS * len(payload["future_timestamps"]))
        if task.session_end_at is not None and last_target_at >= task.session_end_at:
            self._save(
                task,
                KronosShadowResult(
                    status="skipped",
                    error="آفاق Kronos تتجاوز نهاية جلسة السوق",
                ),
                lookback=len(payload["bars"]),
                pred_len=len(payload["future_timestamps"]),
                base_close=float(payload["bars"][-1]["close"]),
                asof_at=forecast_asof,
                received_at=decision_at,
            )
            return
        # سهم رقيق قد يبقي آخر aggregate قديمًا عبر دورات كثيرة. نربط القياس
        # بإغلاق الشمعة الحقيقي، ولا نعيد استدلال نفس السياق كل خمس دقائق.
        with self._lock:
            if self._last_forecast_close.get(task.ticker, -1) >= forecast_close_ms:
                return
        result = self.client.forecast(payload)
        received_at = _utc_datetime(self._now_fn())
        earliest_target_at = forecast_asof + timedelta(
            milliseconds=BAR_INTERVAL_MS * min(self.horizons))
        if result.ok and received_at >= earliest_target_at:
            result = KronosShadowResult(
                status="skipped",
                model=result.model,
                model_revision=result.model_revision,
                tokenizer=result.tokenizer,
                tokenizer_revision=result.tokenizer_revision,
                kronos_revision=result.kronos_revision,
                service_revision=result.service_revision,
                market_timezone=result.market_timezone,
                device=result.device,
                max_context=result.max_context,
                lookback=result.lookback,
                pred_len=result.pred_len,
                latency_ms=result.latency_ms,
                service_latency_ms=result.service_latency_ms,
                error="اكتمل الاستدلال بعد أول أفق قياس؛ أُهمل منعًا للقياس الرجعي",
            )
        saved = self._save(
            task,
            result,
            lookback=result.lookback or len(payload["bars"]),
            pred_len=result.pred_len or len(payload["future_timestamps"]),
            base_close=float(payload["bars"][-1]["close"]),
            asof_at=forecast_asof,
            received_at=received_at,
        )
        if result.ok and saved:
            with self._lock:
                self._last_forecast_close[task.ticker] = max(
                    forecast_close_ms,
                    self._last_forecast_close.get(task.ticker, -1),
                )

    def _save(
        self,
        task: _ShadowTask,
        result: KronosShadowResult,
        *,
        lookback: int,
        pred_len: int,
        base_close: Optional[float] = None,
        asof_at: Optional[datetime] = None,
        received_at: Optional[datetime] = None,
    ) -> bool:
        """حفظ best-effort؛ فشل القرص لا يوقف العامل أو الماسح."""
        try:
            numeric_returns = {
                int(horizon): value
                for horizon, value in result.returns_pct.items()
                if str(horizon).isdigit()
            }
            stored_asof = asof_at or datetime.fromtimestamp(
                task.close_ms / 1000.0, tz=timezone.utc)
            experiment_id = kronos_experiment_id(
                model=result.model,
                model_revision=result.model_revision,
                tokenizer=result.tokenizer,
                tokenizer_revision=result.tokenizer_revision,
                kronos_revision=result.kronos_revision,
                service_revision=result.service_revision,
                market_timezone=result.market_timezone,
                device=result.device,
                max_context=result.max_context or MAX_LOOKBACK,
                client_revision=self.client_revision,
                lookback=lookback,
                pred_len=pred_len,
                horizons=self.horizons,
                attempt_kind=self._attempt_kind(result),
            )
            self.store.save_kronos_forecast(
                task.ticker,
                stored_asof,
                status=result.status,
                experiment_id=experiment_id,
                session=task.session,
                model=result.model,
                model_revision=result.model_revision,
                tokenizer=result.tokenizer,
                tokenizer_revision=result.tokenizer_revision,
                kronos_revision=result.kronos_revision,
                service_revision=result.service_revision,
                market_timezone=result.market_timezone,
                device=result.device,
                max_context=result.max_context,
                client_revision=self.client_revision,
                observation_grace_min=self.observation_grace_min,
                lookback=lookback,
                pred_len=pred_len,
                base_close=base_close,
                returns=numeric_returns,
                latency_ms=result.latency_ms,
                service_latency_ms=result.service_latency_ms,
                error=result.error,
                requested_at=task.requested_at,
                received_at=received_at,
            )
            return True
        except Exception:
            self._bump_runtime("save_failed")
            logger.debug("تعذّر حفظ توقع Kronos لـ %s", task.ticker, exc_info=True)
            return False

    @staticmethod
    def _attempt_kind(result: KronosShadowResult) -> str:
        """تصنيف ثابت لمحاولة غير ناجحة؛ التفاصيل الكاملة تبقى في error."""
        if result.status == "ok":
            return ""
        raw = str(result.error or "").strip()
        prefix, separator, _ = raw.partition(":")
        reason = prefix.strip() if separator else raw
        return f"{result.status}:{reason[:128]}"
