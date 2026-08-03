"""اختبارات طبقة Kronos الظلية — بلا شبكة أو PyTorch."""

from __future__ import annotations

from datetime import datetime, timezone
import math
import threading
from types import SimpleNamespace

import requests

from runner_scanner.kronos_shadow import (
    KronosShadowClient,
    KronosShadowResult,
    KronosShadowWorker,
    kronos_experiment_id,
    kronos_selection_revision,
    kronos_dedupe_key,
    prepare_forecast_payload,
    should_run_on_5m_close,
)
from runner_scanner.models import Bar
from runner_scanner.config import Config
from runner_scanner.state import Store


def _ms(hour: int, minute: int) -> int:
    return int(datetime(2026, 6, 26, hour, minute, tzinfo=timezone.utc).timestamp() * 1000)


def _bar(minute: int, *, vw: float = 0.0, close: float = 10.2) -> Bar:
    return Bar(
        t_ms=_ms(14, minute),
        o=10.0,
        h=10.5,
        l=9.5,
        c=close,
        v=100.0,
        vw=vw,
    )


def _payload() -> dict:
    payload = prepare_forecast_payload(
        "runr",
        [_bar(0), _bar(5), _bar(10)],
        now=datetime(2026, 6, 26, 14, 20, tzinfo=timezone.utc),
        lookback=3,
        min_lookback=2,
        pred_len=3,
        horizons=(1, 3),
    )
    assert payload is not None
    return payload


def _success_response(payload: dict) -> dict:
    return {
        "status": "ok",
        "model": "Kronos-small",
        "model_revision": "model-sha",
        "tokenizer": "Kronos-Tokenizer-base",
        "tokenizer_revision": "tokenizer-sha",
        "kronos_revision": "kronos-sha",
        "service_revision": "service-sha",
        "market_timezone": "America/New_York",
        "device": "cpu",
        "max_context": 512,
        "lookback": len(payload["bars"]),
        "pred_len": len(payload["future_timestamps"]),
        "latency_ms": 75.5,
        "returns_pct": {"1": 1.25, "3": -0.5},
    }


class _Response:
    def __init__(self, status_code=200, data=None, json_error=False):
        self.status_code = status_code
        self._data = data
        self._json_error = json_error

    def json(self):
        if self._json_error:
            raise ValueError("ليس JSON")
        return self._data


class _Session:
    """جلسة وهمية تحفظ نداء POST أو ترمي السلوك المحقون."""

    def __init__(self, behavior):
        self.behavior = behavior
        self.calls = []

    def post(self, url, json=None, headers=None, timeout=None,
             allow_redirects=None, stream=None):
        self.calls.append({
            "url": url,
            "json": json,
            "headers": headers,
            "timeout": timeout,
            "allow_redirects": allow_redirects,
            "stream": stream,
        })
        if isinstance(self.behavior, Exception):
            raise self.behavior
        return self.behavior


def test_prepare_payload_filters_partial_invalid_and_applies_lookback():
    now = datetime(2026, 6, 26, 14, 22, tzinfo=timezone.utc)
    invalid = Bar(t_ms=_ms(14, 7), o=10, h=10.5, l=9.5,
                  c=math.nan, v=100)
    partial = _bar(20)                    # تغلق 14:25، لذلك لا تدخل
    payload = prepare_forecast_payload(
        " runr ",
        [_bar(0, vw=10.1), invalid, _bar(5), _bar(10), _bar(15), partial],
        now=now,
        lookback=2,
        min_lookback=2,
        pred_len=3,
        horizons=(3, 1, 99),
    )

    assert payload is not None
    assert payload["ticker"] == "RUNR"
    assert [row["timestamp"] for row in payload["bars"]] == [
        "2026-06-26T14:10:00.000Z",
        "2026-06-26T14:15:00.000Z",
    ]
    # vw صفري → السعر النموذجي (high+low+close)/3.
    assert payload["bars"][0]["amount"] == 100 * ((10.5 + 9.5 + 10.2) / 3)
    assert payload["future_timestamps"] == [
        "2026-06-26T14:20:00.000Z",
        "2026-06-26T14:25:00.000Z",
        "2026-06-26T14:30:00.000Z",
    ]
    assert payload["horizons"] == [1, 3]


def test_prepare_payload_uses_bar_vwap_for_amount_and_never_emits_nan():
    payload = prepare_forecast_payload(
        "X",
        [_bar(0, vw=10.25), _bar(5, vw=10.25)],
        now=datetime(2026, 6, 26, 14, 10, tzinfo=timezone.utc),
        lookback=2,
        min_lookback=2,
        pred_len=1,
        horizons=(1,),
    )
    assert payload is not None
    assert payload["bars"][0]["amount"] == 1025.0
    assert prepare_forecast_payload(
        "X",
        [Bar(t_ms=_ms(14, 0), o=10, h=10, l=10, c=10, v=math.inf)],
        now=datetime(2026, 6, 26, 14, 5, tzinfo=timezone.utc),
        min_lookback=2,
        pred_len=1,
        horizons=(1,),
    ) is None


def test_dedupe_key_changes_only_after_next_5m_close():
    at_1431 = datetime(2026, 6, 26, 14, 31, tzinfo=timezone.utc)
    at_1434 = datetime(2026, 6, 26, 14, 34, 59, tzinfo=timezone.utc)
    at_1435 = datetime(2026, 6, 26, 14, 35, tzinfo=timezone.utc)
    key = kronos_dedupe_key("runr", at_1431)
    assert key == kronos_dedupe_key("RUNR", at_1434)
    assert not should_run_on_5m_close("RUNR", key, at_1434)
    assert should_run_on_5m_close("RUNR", key, at_1435)


def test_experiment_identity_changes_with_every_behavioral_runtime_dimension():
    identity = {
        "model": "model", "model_revision": "model-sha",
        "tokenizer": "tokenizer", "tokenizer_revision": "tokenizer-sha",
        "kronos_revision": "source-sha", "service_revision": "service-sha",
        "market_timezone": "America/New_York", "device": "cpu",
        "max_context": 512, "client_revision": "client-sha",
        "lookback": 256, "pred_len": 18, "horizons": (6, 12, 18),
    }
    baseline = kronos_experiment_id(**identity)

    for field, alternate in (
        ("service_revision", "service-sha-2"),
        ("client_revision", "client-sha-2"),
        ("device", "mps"),
        ("max_context", 256),
        ("market_timezone", "UTC"),
        ("attempt_kind", "error:queue_full"),
    ):
        changed = {**identity, field: alternate}
        assert kronos_experiment_id(**changed) != baseline


def test_selection_revision_changes_with_poll_or_gate_but_not_secret_value():
    baseline = kronos_selection_revision(Config(
        massive_api_key="secret-a", poll_interval_sec=45,
    ))

    assert kronos_selection_revision(Config(
        massive_api_key="secret-b", poll_interval_sec=45,
    )) == baseline
    assert kronos_selection_revision(Config(
        massive_api_key="secret-a", poll_interval_sec=30,
    )) != baseline
    assert kronos_selection_revision(Config(
        massive_api_key="secret-a", trigger_change_pct=12.0,
    )) != baseline


def test_client_posts_contract_with_optional_bearer_and_parses_result():
    payload = _payload()
    session = _Session(_Response(data=_success_response(payload)))
    times = iter((10.0, 10.25))
    client = KronosShadowClient(
        "https://kronos.example/",
        token="secret",
        timeout=4.5,
        session=session,
        clock=lambda: next(times),
    )

    result = client.forecast(payload)

    assert result.ok
    assert result.model == "Kronos-small"
    assert result.revision == "model-sha"
    assert result.returns == {"1": 1.25, "3": -0.5}
    assert result.horizons == (1, 3)
    assert result.latency_ms == 250.0
    assert result.service_latency_ms == 75.5
    assert result.device == "cpu"
    assert result.max_context == 512
    assert session.calls == [{
        "url": "https://kronos.example/v1/forecast",
        "json": payload,
        "headers": {
            "Content-Type": "application/json",
            "Authorization": "Bearer secret",
        },
        "timeout": 4.5,
        "allow_redirects": False,
        "stream": True,
    }]


def test_client_omits_authorization_without_token():
    payload = _payload()
    session = _Session(_Response(data=_success_response(payload)))
    result = KronosShadowClient("https://kronos.example", session=session).forecast(payload)
    assert result.ok
    assert "Authorization" not in session.calls[0]["headers"]


def test_client_requires_https_except_for_loopback():
    payload = _payload()
    session = _Session(_Response(data=_success_response(payload)))

    result = KronosShadowClient(
        "http://kronos.example", token="secret", session=session,
    ).forecast(payload)

    assert result.status == "error"
    assert "HTTPS" in result.error
    assert session.calls == []
    local = _Session(_Response(data=_success_response(payload)))
    assert KronosShadowClient(
        "http://127.0.0.1:8080", token="secret", session=local,
    ).forecast(payload).ok


def test_client_network_http_and_bad_json_fail_safely():
    payload = _payload()
    behaviors = [
        requests.ConnectionError("down"),
        _Response(status_code=503, data={}),
        _Response(json_error=True),
    ]
    for behavior in behaviors:
        session = _Session(behavior)
        result = KronosShadowClient("https://kronos.example", session=session).forecast(payload)
        assert result.status == "error"
        assert result.error


def test_client_extracts_safe_error_message_from_service_contract():
    payload = _payload()
    response = _Response(data={
        "status": "error",
        "error": {"code": "invalid_request", "message": "حمولة غير صالحة"},
    })

    result = KronosShadowClient(
        "https://kronos.example", session=_Session(response)).forecast(payload)

    assert result.status == "error"
    assert result.error == "فشل Kronos: حمولة غير صالحة"


def test_client_rejects_malformed_response_and_oversized_payload():
    payload = _payload()
    malformed = _success_response(payload)
    malformed["returns_pct"] = {"1": float("nan"), "3": 2.0}
    result = KronosShadowClient(
        "https://kronos.example",
        session=_Session(_Response(data=malformed)),
    ).forecast(payload)
    assert result.status == "error"

    session = _Session(_Response(data=_success_response(payload)))
    result = KronosShadowClient(
        "https://kronos.example",
        max_payload_bytes=10,
        session=session,
    ).forecast(payload)
    assert result.status == "error"
    assert session.calls == []


def test_client_caps_streamed_response_before_json_decode():
    payload = _payload()

    class _LargeResponse:
        status_code = 200
        headers = {}
        closed = False

        def iter_content(self, chunk_size):
            assert chunk_size == 64 * 1024
            yield b"{" + (b"x" * 32)

        def close(self):
            self.closed = True

    response = _LargeResponse()
    result = KronosShadowClient(
        "https://kronos.example",
        max_response_bytes=16,
        session=_Session(response),
    ).forecast(payload)

    assert result.status == "error"
    assert "تجاوز" in result.error
    assert response.closed


class _Massive:
    def __init__(self, bars):
        self.bars = bars
        self.calls = []

    def bars_5min(self, ticker, start, end):
        self.calls.append((ticker, start, end))
        return self.bars


class _Store:
    def __init__(self):
        self.calls = []
        self.saved = threading.Event()

    def save_kronos_forecast(self, ticker, asof_at, **kwargs):
        self.calls.append((ticker, asof_at, kwargs))
        self.saved.set()


def test_worker_fetches_independent_context_dedupes_and_saves():
    now = datetime(2026, 6, 26, 14, 20, 30, tzinfo=timezone.utc)
    bars = [_bar(0), _bar(5), _bar(10), _bar(15)]
    massive = _Massive(bars)
    store = _Store()
    payload = prepare_forecast_payload(
        "RUNR", bars, now=now, lookback=3, min_lookback=2,
        pred_len=3, horizons=(1, 3))
    assert payload is not None
    client = KronosShadowClient(
        "https://kronos.example",
        session=_Session(_Response(data=_success_response(payload))),
    )
    factory_calls = []

    def factory():
        factory_calls.append(True)
        return massive

    worker = KronosShadowWorker(
        client=client,
        massive_client_factory=factory,
        store=store,
        context_days=3,
        min_lookback=2,
        lookback=3,
        pred_len=3,
        horizons=(1, 3),
        queue_size=2,
        now_fn=lambda: now,
    )
    worker.start()
    try:
        assert worker.submit("runr", now=now)
        assert not worker.submit("RUNR", now=now)     # نفس الرمز/الشمعة
        assert store.saved.wait(2.0)
    finally:
        worker.stop()

    assert len(factory_calls) == 1
    assert massive.calls == [("RUNR", "2026-06-23", "2026-06-26")]
    assert len(store.calls) == 1
    ticker, asof_at, saved = store.calls[0]
    assert ticker == "RUNR"
    assert asof_at == datetime(2026, 6, 26, 14, 20, tzinfo=timezone.utc)
    assert saved["status"] == "ok"
    assert saved["returns"] == {1: 1.25, 3: -0.5}
    assert saved["base_close"] == 10.2
    assert saved["service_latency_ms"] == 75.5
    assert saved["device"] == "cpu"
    assert saved["max_context"] == 512
    assert len(saved["client_revision"]) == 64


def test_worker_queue_is_bounded_before_start():
    store = _Store()
    worker = KronosShadowWorker(
        client=KronosShadowClient(""),
        massive_client_factory=lambda: _Massive([]),
        store=store,
        pred_len=1,
        horizons=(1,),
        queue_size=1,
    )
    now = datetime(2026, 6, 26, 14, 20, tzinfo=timezone.utc)
    assert worker.submit("A", now=now)
    assert not worker.submit("A", now=now)
    assert not worker.submit("B", now=now)       # الطابور ممتلئ، بلا انتظار
    assert worker.runtime_stats["enqueued"] == 1
    assert worker.runtime_stats["duplicate"] == 1
    assert worker.runtime_stats["queue_full"] == 1
    assert worker.runtime_stats["queue_depth"] == 1
    assert worker.runtime_stats["audit_queue_depth"] == 1
    assert store.calls == []

    worker.start()
    try:
        assert store.saved.wait(2.0)
    finally:
        worker.stop()

    queue_full_calls = [call for call in store.calls if call[0] == "B"]
    assert len(queue_full_calls) == 1
    ticker, asof_at, saved = queue_full_calls[0]
    assert ticker == "B"
    assert asof_at == datetime(2026, 6, 26, 14, 20, tzinfo=timezone.utc)
    assert saved["status"] == "skipped"
    assert "ممتلئ" in saved["error"]


def test_queue_full_submit_never_waits_for_audit_store():
    entered = threading.Event()
    main_fetched = threading.Event()
    release = threading.Event()
    returned = threading.Event()

    class _BlockingStore:
        def save_kronos_forecast(self, *_args, **_kwargs):
            entered.set()
            release.wait(2.0)

    class _TrackingMassive(_Massive):
        def bars_5min(self, ticker, start, end):
            main_fetched.set()
            return super().bars_5min(ticker, start, end)

    massive = _TrackingMassive([])

    worker = KronosShadowWorker(
        client=KronosShadowClient(""),
        massive_client_factory=lambda: massive,
        store=_BlockingStore(),
        pred_len=1,
        horizons=(1,),
        queue_size=1,
        now_fn=lambda: now,
    )
    now = datetime(2026, 6, 26, 14, 20, tzinfo=timezone.utc)
    assert worker.submit("A", now=now)

    submitter = threading.Thread(
        target=lambda: (worker.submit("B", now=now), returned.set())
    )
    submitter.start()
    try:
        assert returned.wait(0.5)
        assert not entered.is_set()
        assert worker.runtime_stats["audit_queue_depth"] == 1

        worker.start()
        assert main_fetched.wait(1.0)
        assert entered.wait(1.0)
    finally:
        release.set()
        submitter.join(1.0)
        worker.stop(2.0)


def test_queue_full_audit_queue_is_bounded_and_exposes_loss():
    store = _Store()
    worker = KronosShadowWorker(
        client=KronosShadowClient(""),
        massive_client_factory=lambda: _Massive([]),
        store=store,
        pred_len=1,
        horizons=(1,),
        queue_size=1,
    )
    now = datetime(2026, 6, 26, 14, 20, tzinfo=timezone.utc)
    assert worker.submit("A", now=now)
    assert not worker.submit("B", now=now)
    assert not worker.submit("C", now=now)

    stats = worker.runtime_stats
    assert stats["queue_full"] == 2
    assert stats["audit_queue_depth"] == 1
    assert stats["audit_queue_capacity"] == 1
    assert stats["audit_dropped"] == 1
    assert stats["save_failed"] == 1

    worker.stop()
    assert {call[0] for call in store.calls} == {"A", "B"}


def test_queue_full_audit_dedupes_without_blocking_inference_retry():
    store = _Store()
    worker = KronosShadowWorker(
        client=KronosShadowClient(""),
        massive_client_factory=lambda: _Massive([]),
        store=store,
        pred_len=1,
        horizons=(1,),
        queue_size=2,
    )
    now = datetime(2026, 6, 26, 14, 20, tzinfo=timezone.utc)
    assert worker.submit("A", now=now)
    assert worker.submit("D", now=now)
    assert not worker.submit("B", now=now)
    assert not worker.submit("B", now=now)
    assert not worker.submit("C", now=now)

    stats = worker.runtime_stats
    assert stats["queue_full"] == 3
    assert stats["audit_duplicate"] == 1
    assert stats["audit_queue_depth"] == 2
    assert stats["audit_dropped"] == 0

    worker.stop()
    audited = {
        call[0] for call in store.calls
        if str(call[2]["error"]).startswith("queue_full:")
    }
    assert audited == {"B", "C"}


def test_queue_full_audit_survives_later_failure_for_same_close():
    store = Store(":memory:")
    worker = KronosShadowWorker(
        client=KronosShadowClient(""),
        massive_client_factory=lambda: _Massive([]),
        store=store,
        pred_len=1,
        horizons=(1,),
        queue_size=1,
    )
    now = datetime(2026, 6, 26, 14, 20, tzinfo=timezone.utc)
    assert worker.submit("A", now=now)
    assert not worker.submit("B", now=now)
    assert worker._drain_one_audit()  # noqa: SLF001 — ثبّت سجل الامتلاء أولًا
    task = SimpleNamespace(
        ticker="B",
        requested_at=now,
        close_ms=int(now.timestamp() * 1000),
        session="رسمي",
        session_end_at=None,
    )

    assert worker._save(  # noqa: SLF001 — إعادة إنتاج upsert لنفس الفرصة
        task,
        KronosShadowResult(status="error", error="service_down: unavailable"),
        lookback=worker.lookback,
        pred_len=worker.pred_len,
        asof_at=now,
    )

    rows = [row for row in store.fetch_kronos_forecasts() if row["ticker"] == "B"]
    assert len(rows) == 2
    assert {row["error"].split(":", 1)[0] for row in rows} == {
        "queue_full", "service_down"
    }
    worker.stop()
    store.close()


def test_worker_skips_context_too_old_to_be_a_prospective_forecast():
    now = datetime(2026, 6, 26, 14, 40, tzinfo=timezone.utc)
    requested = datetime(2026, 6, 26, 14, 20, 30, tzinfo=timezone.utc)
    bars = [_bar(0), _bar(5), _bar(10), _bar(15)]  # آخر إغلاق فعلي 14:20
    massive = _Massive(bars)
    store = _Store()
    payload = prepare_forecast_payload(
        "THIN", bars, now=now, lookback=3, min_lookback=2,
        pred_len=3, horizons=(1, 3))
    assert payload is not None
    session = _Session(_Response(data=_success_response(payload)))
    worker = KronosShadowWorker(
        client=KronosShadowClient("https://kronos.example", session=session),
        massive_client_factory=lambda: massive,
        store=store,
        min_lookback=2,
        lookback=3,
        pred_len=3,
        horizons=(1, 3),
        now_fn=lambda: now,
    )

    task = SimpleNamespace(
        ticker="THIN", requested_at=requested,
        close_ms=int(requested.timestamp() * 1000), session="رسمي",
        session_end_at=None)
    worker._process(task, massive)  # noqa: SLF001 — اختبار المرساة مباشرة
    worker._process(
        task, massive,  # noqa: SLF001 — نفس السياق لا يُستدل مرتين
    )

    assert massive.calls == []
    assert len(store.calls) == 2
    assert all(call[2]["status"] == "skipped" for call in store.calls)
    assert session.calls == []
    assert worker.runtime_stats["stale_before_fetch"] == 2


def test_worker_never_uses_bars_that_arrived_after_selection_time():
    requested = datetime(2026, 6, 26, 14, 20, 30, tzinfo=timezone.utc)
    processed = datetime(2026, 6, 26, 14, 24, 0, tzinfo=timezone.utc)
    bars = [
        _bar(0), _bar(5), _bar(10), _bar(15),
        _bar(20, close=99.0),  # ظهرت بعد cutoff القرار ولا يجوز أن تدخل.
    ]
    massive = _Massive(bars)
    store = _Store()
    expected_payload = prepare_forecast_payload(
        "CUT", bars,
        now=datetime(2026, 6, 26, 14, 20, tzinfo=timezone.utc),
        lookback=3, min_lookback=3, pred_len=3, horizons=(1, 3),
    )
    assert expected_payload is not None
    session = _Session(_Response(data=_success_response(expected_payload)))
    worker = KronosShadowWorker(
        client=KronosShadowClient("https://kronos.example", session=session),
        massive_client_factory=lambda: massive,
        store=store,
        min_lookback=3,
        lookback=3,
        pred_len=3,
        horizons=(1, 3),
        now_fn=lambda: processed,
    )
    task = SimpleNamespace(
        ticker="CUT", requested_at=requested,
        close_ms=int(datetime(2026, 6, 26, 14, 20, tzinfo=timezone.utc).timestamp() * 1000),
        session="رسمي", session_end_at=None,
    )

    worker._process(task, massive)  # noqa: SLF001

    assert session.calls[0]["json"]["bars"][-1]["timestamp"] == (
        "2026-06-26T14:15:00.000Z"
    )
    assert store.calls[0][2]["base_close"] == 10.2


def test_worker_skips_prediction_path_that_crosses_session_end():
    now = datetime(2026, 6, 26, 14, 20, 30, tzinfo=timezone.utc)
    bars = [_bar(0), _bar(5), _bar(10), _bar(15)]
    massive = _Massive(bars)
    store = _Store()
    payload = prepare_forecast_payload(
        "LATE", bars, now=now, lookback=3, min_lookback=2,
        pred_len=3, horizons=(1, 3))
    assert payload is not None
    session = _Session(_Response(data=_success_response(payload)))
    worker = KronosShadowWorker(
        client=KronosShadowClient("https://kronos.example", session=session),
        massive_client_factory=lambda: massive,
        store=store,
        min_lookback=2,
        lookback=3,
        pred_len=3,
        horizons=(1, 3),
        now_fn=lambda: now,
    )
    task = SimpleNamespace(
        ticker="LATE", requested_at=now,
        close_ms=int(now.timestamp() * 1000), session="رسمي",
        session_end_at=datetime(2026, 6, 26, 14, 35, tzinfo=timezone.utc),
    )

    worker._process(task, massive)  # noqa: SLF001

    assert len(store.calls) == 1
    assert store.calls[0][2]["status"] == "skipped"
    assert store.calls[0][2]["session"] == "رسمي"
    assert session.calls == []


def test_worker_stop_finishes_current_task_and_discards_waiting_tasks():
    now = datetime(2026, 6, 26, 14, 20, 30, tzinfo=timezone.utc)
    bars = [_bar(0), _bar(5), _bar(10), _bar(15)]
    entered = threading.Event()
    release = threading.Event()

    class _BlockingMassive(_Massive):
        def bars_5min(self, ticker, start, end):
            self.calls.append((ticker, start, end))
            entered.set()
            assert release.wait(2.0)
            return self.bars

    massive = _BlockingMassive(bars)
    store = _Store()
    payload = prepare_forecast_payload(
        "A", bars, now=now, lookback=3, min_lookback=2,
        pred_len=3, horizons=(1, 3))
    assert payload is not None
    client = KronosShadowClient(
        "https://kronos.example",
        session=_Session(_Response(data=_success_response(payload))),
    )
    worker = KronosShadowWorker(
        client=client,
        massive_client_factory=lambda: massive,
        store=store,
        min_lookback=2,
        lookback=3,
        pred_len=3,
        horizons=(1, 3),
        queue_size=2,
        now_fn=lambda: now,
    )
    worker.start()
    assert worker.submit("A", now=now)
    assert entered.wait(1.0)
    assert worker.submit("B", now=now)

    stopper = threading.Thread(target=worker.stop)
    stopper.start()
    assert worker._stop.wait(1.0)                 # تأكّد أن stop سبق تحرير المهمة
    release.set()
    stopper.join(2.0)

    assert not worker.is_alive
    assert not worker.submit("C", now=now)
    assert [call[0] for call in massive.calls] == ["A"]
    assert {call[0] for call in store.calls} == {"A", "B"}
    stopped = next(call for call in store.calls if call[0] == "B")
    assert stopped[2]["status"] == "skipped"
    assert stopped[2]["error"].startswith("worker_stopped:")


def test_stop_persists_queued_cohort_across_store_reopen(tmp_path):
    path = tmp_path / "stopped-queue.sqlite3"
    store = Store(str(path))
    worker = KronosShadowWorker(
        client=KronosShadowClient(""),
        massive_client_factory=lambda: _Massive([]),
        store=store,
        pred_len=1,
        horizons=(1,),
        queue_size=2,
    )
    now = datetime(2026, 6, 26, 14, 20, 30, tzinfo=timezone.utc)
    assert worker.submit("A", session="رسمي", now=now)
    assert worker.submit("B", session="رسمي", now=now)

    worker.stop()
    store.close()
    reopened = Store(str(path))
    rows = reopened.fetch_kronos_forecasts()

    assert {row["ticker"] for row in rows} == {"A", "B"}
    assert all(row["status"] == "skipped" for row in rows)
    assert all(row["error"].startswith("worker_stopped:") for row in rows)
    reopened.close()
