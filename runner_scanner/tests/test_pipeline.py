"""اختبار خط المعالجة الكامل بعميل وهمي (بلا إنترنت)."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from runner_scanner.config import Config
from runner_scanner.halts import HaltTracker
from runner_scanner.models import FloatSource, HaltState, Session
from runner_scanner.pipeline import process_candidate
from runner_scanner.tests.fixtures import (
    FakeClient, downtrend_daily_bars, make_snapshot,
)

ET = ZoneInfo("America/New_York")
CFG = Config.from_env()
ET_NOW = datetime(2026, 6, 25, 10, 30, tzinfo=ET)


def test_pipeline_accepts_strong_runner():
    cand = process_candidate(
        CFG, FakeClient(), make_snapshot(change_pct=25.0),
        halts=None, session=Session.REGULAR, et_now=ET_NOW)
    assert cand.is_rejected is False
    assert cand.final_score >= CFG.alert_score_min
    assert cand.risk is not None
    assert cand.catalyst.has_news is True
    assert cand.market_cap is not None


def test_pipeline_rejects_not_technically_ready():
    client = FakeClient(daily=downtrend_daily_bars())   # جاهزية < 70
    cand = process_candidate(
        CFG, client, make_snapshot(change_pct=25.0),
        halts=None, session=Session.REGULAR, et_now=ET_NOW)
    assert cand.is_rejected is True
    assert "جاهزية" in cand.rejected_reason


def test_pipeline_rejects_halted():
    ht = HaltTracker(CFG, clock=lambda: 0.0)
    ht.process_event({"ev": "T", "sym": "RUNR", "c": [17]})
    cand = process_candidate(
        CFG, FakeClient(), make_snapshot(ticker="RUNR", change_pct=25.0),
        halts=ht, session=Session.REGULAR, et_now=ET_NOW)
    assert cand.is_rejected is True
    assert cand.halt_state is HaltState.HALTED


def test_pipeline_rejects_low_price():
    cand = process_candidate(
        CFG, FakeClient(), make_snapshot(last=0.40, change_pct=25.0),
        halts=None, session=Session.REGULAR, et_now=ET_NOW)
    assert cand.is_rejected is True


def test_pipeline_accepts_without_news():
    client = FakeClient(news=False)
    cand = process_candidate(
        CFG, client, make_snapshot(change_pct=25.0),
        halts=None, session=Session.REGULAR, et_now=ET_NOW)
    # الخبر إشارة تقوية لا بوّابة → يُقبل بدون خبر لو قوي
    assert cand.is_rejected is False
    assert cand.catalyst.has_news is False


def test_pipeline_unknown_float_still_processed():
    client = FakeClient(float_shares=None, float_source=FloatSource.UNKNOWN)
    cand = process_candidate(
        CFG, client, make_snapshot(change_pct=25.0),
        halts=None, session=Session.REGULAR, et_now=ET_NOW)
    # فلوت مجهول لا يرفض صامتًا
    assert cand.is_rejected is False
