from __future__ import annotations

import json
import threading
import unittest
from typing import Any

from services.kronos_inference.config import ServiceConfig
from services.kronos_inference.engine import EngineUnavailable
from services.kronos_inference.service import ForecastApplication
from services.kronos_inference.tests.helpers import valid_payload


class FakeEngine:
    def __init__(self) -> None:
        self.calls = 0
        self.ready = True

    def health(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "model_loaded": False,
            "kronos_revision": "source-revision",
            "service_revision": "service-revision",
            "market_timezone": "America/New_York",
            "device": "cpu",
            "max_context": 512,
        }

    def is_ready(self) -> bool:
        return self.ready

    def forecast(self, request: Any) -> dict[str, Any]:
        self.calls += 1
        return {
            "status": "ok",
            "returns_pct": {str(value): float(value) for value in request.horizons},
            "model": "NeoQuasar/Kronos-small",
            "model_revision": "model-revision",
            "tokenizer": "NeoQuasar/Kronos-Tokenizer-base",
            "tokenizer_revision": "tokenizer-revision",
            "kronos_revision": "source-revision",
            "service_revision": "service-revision",
            "market_timezone": "America/New_York",
            "device": "cpu",
            "max_context": 512,
            "lookback": request.lookback,
            "pred_len": request.pred_len,
            "latency_ms": 1.25,
        }


class UnavailableEngine(FakeEngine):
    def forecast(self, request: Any) -> dict[str, Any]:
        raise EngineUnavailable("تفصيل داخلي يجب ألا يتسرب")


class BlockingEngine(FakeEngine):
    def __init__(self) -> None:
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def forecast(self, request: Any) -> dict[str, Any]:
        self.entered.set()
        if not self.release.wait(timeout=2.0):
            raise AssertionError("لم يُحرّر اختبار التزامن المحرّك")
        return super().forecast(request)


class ApplicationTests(unittest.TestCase):
    def _body(self) -> bytes:
        return json.dumps(valid_payload(), separators=(",", ":")).encode("utf-8")

    def test_health_does_not_call_forecast(self) -> None:
        engine = FakeEngine()
        application = ForecastApplication(ServiceConfig(), engine)
        status, body = application.handle_health()
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["kronos_revision"], "source-revision")
        self.assertEqual(engine.calls, 0)

    def test_ready_has_distinct_success_and_general_failure_responses(self) -> None:
        engine = FakeEngine()
        source_revision = "a" * 40
        config = ServiceConfig(kronos_source_revision=source_revision)
        application = ForecastApplication(config, engine)

        status, body = application.handle_ready()
        self.assertEqual(status, 200)
        self.assertEqual(body, {"status": "ready", "kronos_revision": source_revision})

        engine.ready = False
        status, body = application.handle_ready()
        self.assertEqual(status, 503)
        self.assertEqual(body["status"], "not_ready")
        self.assertEqual(body["message"], "خدمة Kronos غير جاهزة")

    def test_ready_requires_bearer_when_service_has_a_token(self) -> None:
        application = ForecastApplication(
            ServiceConfig(api_token="secret"), FakeEngine()
        )

        status, body = application.handle_ready({})
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "unauthorized")
        status, body = application.handle_ready(
            {"Authorization": "Bearer secret"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ready")

    def test_handler_core_validates_then_calls_engine(self) -> None:
        engine = FakeEngine()
        application = ForecastApplication(ServiceConfig(), engine)
        status, body = application.handle_forecast(self._body(), {})
        self.assertEqual(status, 200)
        self.assertEqual(body["returns_pct"], {"1": 1.0, "2": 2.0, "3": 3.0})
        self.assertEqual(body["kronos_revision"], "source-revision")
        self.assertEqual(body["lookback"], 32)
        self.assertEqual(engine.calls, 1)

    def test_unauthorized_request_never_reaches_parser_or_engine(self) -> None:
        engine = FakeEngine()
        config = ServiceConfig(api_token="secret")
        application = ForecastApplication(config, engine)
        status, body = application.handle_forecast(b"not-json", {})
        self.assertEqual(status, 401)
        self.assertEqual(body["error"]["code"], "unauthorized")
        self.assertEqual(engine.calls, 0)

    def test_malformed_json_returns_safe_400(self) -> None:
        engine = FakeEngine()
        application = ForecastApplication(ServiceConfig(), engine)
        status, body = application.handle_forecast(b"{", {})
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["code"], "invalid_json")
        self.assertEqual(engine.calls, 0)

    def test_body_limit_is_enforced_in_core(self) -> None:
        engine = FakeEngine()
        application = ForecastApplication(ServiceConfig(max_body_bytes=4), engine)
        status, body = application.handle_forecast(b"12345", {})
        self.assertEqual(status, 413)
        self.assertEqual(body["error"]["code"], "body_too_large")

    def test_concurrent_forecast_returns_busy_without_waiting(self) -> None:
        engine = BlockingEngine()
        application = ForecastApplication(ServiceConfig(), engine)
        first_result: list[tuple[int, dict[str, Any]]] = []

        first_thread = threading.Thread(
            target=lambda: first_result.append(
                application.handle_forecast(self._body(), {})
            )
        )
        first_thread.start()
        self.assertTrue(engine.entered.wait(timeout=1.0))

        health_status, health_body = application.handle_health()
        self.assertEqual(health_status, 200)
        self.assertIs(health_body["forecast_in_progress"], True)
        self.assertGreaterEqual(health_body["forecast_age_seconds"], 0.0)

        second_status, second_body = application.handle_forecast(self._body(), {})
        self.assertEqual(second_status, 429)
        self.assertEqual(second_body["error"]["code"], "busy")

        engine.release.set()
        first_thread.join(timeout=2.0)
        self.assertFalse(first_thread.is_alive())
        self.assertEqual(first_result[0][0], 200)
        self.assertEqual(engine.calls, 1)
        _, idle_health = application.handle_health()
        self.assertIs(idle_health["forecast_in_progress"], False)
        self.assertIsNone(idle_health["forecast_age_seconds"])

    def test_internal_loading_detail_is_not_returned(self) -> None:
        application = ForecastApplication(ServiceConfig(), UnavailableEngine())
        with self.assertLogs("services.kronos_inference.service", level="ERROR"):
            status, body = application.handle_forecast(self._body(), {})
        self.assertEqual(status, 503)
        self.assertEqual(body["error"]["code"], "service_unavailable")
        self.assertNotIn("تفصيل داخلي", json.dumps(body, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
