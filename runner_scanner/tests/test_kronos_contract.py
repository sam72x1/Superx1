"""اختبار توافق عقد عميل Superx1 مع تطبيق خدمة Kronos بلا شبكة."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json as jsonlib

from runner_scanner.kronos_shadow import KronosShadowClient, prepare_forecast_payload
from runner_scanner.models import Bar
from services.kronos_inference.config import ServiceConfig
from services.kronos_inference.service import ForecastApplication


class _Engine:
    def health(self):
        return {"status": "ok", "model_loaded": False}

    def is_ready(self):
        return True

    def forecast(self, request):
        return {
            "status": "ok",
            "returns_pct": {str(h): float(h) for h in request.horizons},
            "model": "NeoQuasar/Kronos-small",
            "model_revision": "model-sha",
            "tokenizer": "NeoQuasar/Kronos-Tokenizer-base",
            "tokenizer_revision": "tokenizer-sha",
            "kronos_revision": "source-sha",
            "service_revision": "service-sha",
            "market_timezone": "America/New_York",
            "device": "cpu",
            "max_context": 512,
            "lookback": request.lookback,
            "pred_len": request.pred_len,
            "latency_ms": 2.5,
        }


class _Response:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


class _ApplicationSession:
    def __init__(self, application):
        self.application = application

    def post(self, url, json=None, headers=None, timeout=None,
             allow_redirects=None, stream=None):
        raw = jsonlib.dumps(
            json, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
        status, body = self.application.handle_forecast(raw, headers or {})
        return _Response(status, body)


def test_scanner_client_and_service_share_the_same_forecast_contract():
    start = datetime(2026, 6, 26, 14, 0, tzinfo=timezone.utc)
    bars = [
        Bar(
            t_ms=int((start + timedelta(minutes=5 * index)).timestamp() * 1000),
            o=10.0, h=10.5, l=9.5, c=10.2, v=1_000.0, vw=10.1,
        )
        for index in range(32)
    ]
    now = start + timedelta(minutes=160)
    payload = prepare_forecast_payload(
        "RUNR", bars, now=now, lookback=32, pred_len=3, horizons=(1, 3),
    )
    assert payload is not None
    application = ForecastApplication(
        ServiceConfig(api_token="secret"), _Engine(),
    )
    client = KronosShadowClient(
        "https://kronos.example",
        token="secret",
        session=_ApplicationSession(application),
    )

    result = client.forecast(payload)

    assert result.ok
    assert result.returns_pct == {"1": 1.0, "3": 3.0}
    assert result.kronos_revision == "source-sha"
    assert result.device == "cpu"
    assert result.max_context == 512
