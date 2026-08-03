"""غلاف HTTP من مكتبة بايثون القياسية فقط."""

from __future__ import annotations

import json
import logging
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import BoundedSemaphore
from typing import Any

from .config import ServiceConfig
from .service import ForecastApplication


logger = logging.getLogger(__name__)


class ForecastHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32

    def __init__(
        self,
        address: tuple[str, int],
        application: ForecastApplication,
    ) -> None:
        self.application = application
        self._request_slots = BoundedSemaphore(application.config.max_request_threads)
        super().__init__(address, ForecastRequestHandler)

    def process_request(self, request: Any, client_address: Any) -> None:
        """حدّ صلب للخيوط يحمي من اتصالات بطيئة/متراكمة."""
        if not self._request_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._request_slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._request_slots.release()


class ForecastRequestHandler(BaseHTTPRequestHandler):
    server_version = "KronosInference"
    sys_version = ""

    @property
    def application(self) -> ForecastApplication:
        return self.server.application  # type: ignore[attr-defined]

    def version_string(self) -> str:
        return self.server_version

    def setup(self) -> None:
        super().setup()
        # ─ تشمل المهلة قراءة سطر الطلب والرؤوس أيضًا، لا الجسم وحده.
        self.connection.settimeout(self.application.config.request_timeout_seconds)

    def log_message(self, format: str, *args: Any) -> None:
        log = logger.debug if self.path in {"/health", "/ready"} else logger.info
        log("%s - %s", self.client_address[0], format % args)

    def _send_json(
        self,
        status_code: int,
        body: dict[str, Any],
        *,
        authenticate: bool = False,
    ) -> None:
        encoded = json.dumps(
            body,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if authenticate:
            self.send_header("WWW-Authenticate", 'Bearer realm="kronos-inference"')
        self.end_headers()
        self.wfile.write(encoded)

    def _not_found(self) -> None:
        self._send_json(
            404,
            {"status": "error", "error": {"code": "not_found", "message": "المسار غير موجود"}},
        )

    def _method_not_allowed(self) -> None:
        self._send_json(
            405,
            {
                "status": "error",
                "error": {"code": "method_not_allowed", "message": "طريقة HTTP غير مسموحة"},
            },
        )

    def do_GET(self) -> None:
        if self.path == "/health":
            status, body = self.application.handle_health()
            self._send_json(status, body)
            return
        if self.path == "/ready":
            status, body = self.application.handle_ready(self.headers)
            self._send_json(status, body, authenticate=status == 401)
            return
        self._not_found()

    def do_POST(self) -> None:
        if self.path != "/v1/forecast":
            self._not_found()
            return
        if not self.application.authorized(self.headers):
            self._send_json(
                401,
                {
                    "status": "error",
                    "error": {
                        "code": "unauthorized",
                        "message": "رمز Bearer مفقود أو غير صحيح",
                    },
                },
                authenticate=True,
            )
            return
        if self.headers.get("Transfer-Encoding") is not None:
            self._send_json(
                400,
                {
                    "status": "error",
                    "error": {"code": "invalid_body", "message": "Transfer-Encoding غير مدعوم"},
                },
            )
            return
        if self.headers.get_content_type() != "application/json":
            self._send_json(
                415,
                {
                    "status": "error",
                    "error": {"code": "unsupported_media_type", "message": "المطلوب application/json"},
                },
            )
            return

        lengths = self.headers.get_all("Content-Length") or []
        if len(lengths) != 1:
            self._send_json(
                411,
                {
                    "status": "error",
                    "error": {"code": "length_required", "message": "Content-Length مطلوب مرة واحدة"},
                },
            )
            return
        try:
            content_length = int(lengths[0], 10)
        except ValueError:
            content_length = -1
        if content_length < 0:
            self._send_json(
                400,
                {
                    "status": "error",
                    "error": {"code": "invalid_body", "message": "Content-Length غير صالح"},
                },
            )
            return
        if content_length > self.application.config.max_body_bytes:
            self._send_json(
                413,
                {
                    "status": "error",
                    "error": {"code": "body_too_large", "message": "جسم الطلب يتجاوز الحد المسموح"},
                },
            )
            return

        try:
            raw_body = self.rfile.read(content_length)
        except (TimeoutError, socket.timeout):
            self._send_json(
                408,
                {
                    "status": "error",
                    "error": {"code": "request_timeout", "message": "انتهت مهلة قراءة الطلب"},
                },
            )
            return
        if len(raw_body) != content_length:
            self._send_json(
                400,
                {
                    "status": "error",
                    "error": {"code": "invalid_body", "message": "جسم الطلب أقصر من Content-Length"},
                },
            )
            return

        status, body = self.application.handle_forecast(raw_body, self.headers)
        self._send_json(status, body, authenticate=status == 401)

    do_DELETE = _method_not_allowed
    do_HEAD = _method_not_allowed
    do_OPTIONS = _method_not_allowed
    do_PATCH = _method_not_allowed
    do_PUT = _method_not_allowed
    do_TRACE = _method_not_allowed


def serve(config: ServiceConfig, application: ForecastApplication) -> None:
    server = ForecastHTTPServer((config.host, config.port), application)
    logger.info("خدمة Kronos تستمع على %s:%d", config.host, config.port)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        logger.info("إيقاف خدمة Kronos")
    finally:
        server.server_close()
