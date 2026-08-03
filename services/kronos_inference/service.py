"""منطق الطلب المستقل عن مقابس HTTP لتسهيل اختباره."""

from __future__ import annotations

import logging
import threading
from time import monotonic
from typing import Any, Mapping, Protocol

from .auth import HeadersWithDuplicates, is_authorized
from .config import ServiceConfig
from .engine import EngineUnavailable, InferenceFailed
from .validation import RequestValidationError, parse_json_body, validate_forecast_request


logger = logging.getLogger(__name__)


class ForecastEngine(Protocol):
    def health(self) -> dict[str, Any]: ...

    def is_ready(self) -> bool: ...

    def forecast(self, request: Any) -> dict[str, Any]: ...


def _error(code: str, message: str) -> dict[str, Any]:
    return {"status": "error", "error": {"code": code, "message": message}}


class ForecastApplication:
    def __init__(self, config: ServiceConfig, engine: ForecastEngine) -> None:
        self.config = config
        self.engine = engine
        # المحرّك متسلسل أصلًا؛ لا نترك handlerات تنتظر خلف inference بطيء.
        self._forecast_slot = threading.BoundedSemaphore(1)
        self._activity_lock = threading.Lock()
        self._forecast_started_at: float | None = None

    def authorized(self, headers: Mapping[str, str] | HeadersWithDuplicates) -> bool:
        return is_authorized(headers, self.config.api_token)

    def handle_health(self) -> tuple[int, dict[str, Any]]:
        with self._activity_lock:
            started_at = self._forecast_started_at
        age = None if started_at is None else max(0.0, monotonic() - started_at)
        return 200, {
            **self.engine.health(),
            "forecast_in_progress": started_at is not None,
            "forecast_age_seconds": None if age is None else round(age, 3),
        }

    def handle_ready(
        self,
        headers: Mapping[str, str] | HeadersWithDuplicates | None = None,
    ) -> tuple[int, dict[str, Any]]:
        if self.config.api_token and not self.authorized(headers or {}):
            return 401, _error(
                "unauthorized", "رمز Bearer مفقود أو غير صحيح"
            )
        try:
            ready = self.engine.is_ready()
        except Exception:
            logger.exception("تعذّر فحص جاهزية Kronos")
            ready = False
        if ready:
            return 200, {
                "status": "ready",
                "kronos_revision": self.config.kronos_source_revision,
            }
        return 503, {
            "status": "not_ready",
            "message": "خدمة Kronos غير جاهزة",
        }

    def handle_forecast(
        self,
        raw_body: bytes,
        headers: Mapping[str, str] | HeadersWithDuplicates,
    ) -> tuple[int, dict[str, Any]]:
        if not self.authorized(headers):
            return 401, _error("unauthorized", "رمز Bearer مفقود أو غير صحيح")
        if len(raw_body) > self.config.max_body_bytes:
            return 413, _error("body_too_large", "جسم الطلب يتجاوز الحد المسموح")

        try:
            payload = parse_json_body(raw_body)
            request = validate_forecast_request(payload, self.config)
        except RequestValidationError as exc:
            return 400, _error(exc.code, str(exc))

        if not self._forecast_slot.acquire(blocking=False):
            return 429, _error("busy", "خدمة Kronos تنفّذ توقعًا آخر؛ أعد المحاولة")
        with self._activity_lock:
            self._forecast_started_at = monotonic()
        try:
            try:
                return 200, self.engine.forecast(request)
            except EngineUnavailable:
                logger.exception("تعذّر تجهيز محرّك Kronos")
                return 503, _error("service_unavailable", "نموذج التوقع غير متاح حاليًا")
            except InferenceFailed:
                logger.exception("فشل استدلال Kronos")
                return 500, _error("inference_failed", "تعذّر إنشاء التوقع")
            except Exception:
                logger.exception("خطأ غير متوقع في خدمة Kronos")
                return 500, _error("internal_error", "حدث خطأ داخلي")
        finally:
            with self._activity_lock:
                self._forecast_started_at = None
            self._forecast_slot.release()
