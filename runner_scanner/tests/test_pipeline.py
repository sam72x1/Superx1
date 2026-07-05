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


def test_pipeline_wires_labeled_targets_and_ma_into_card():
    """تحقّق دمج طرف-لطرف: الخط الكامل يُنتج أهدافًا موسومة + متوسطات، والبطاقة
    تعرضها (منهجية المستخدم مدموجة فعلًا لا مجرد دوال منعزلة)."""
    from runner_scanner.alerts import build_card
    cand = process_candidate(
        CFG, FakeClient(), make_snapshot(change_pct=25.0),
        halts=None, session=Session.REGULAR, et_now=ET_NOW)
    assert cand.is_rejected is False
    rp = cand.risk
    # الأهداف موسومة بأنواعها (مصدرها الخط لا بناء يدوي)
    assert rp.target_kinds and len(rp.target_kinds) == len(rp.targets)
    assert all(k for k in rp.target_kinds)
    # المتوسطان اليوميان محسوبان ومُمرَّران للعرض (تاريخ 260 يومًا كافٍ)
    assert rp.ma20 is not None and rp.ma50 is not None
    card = build_card(CFG, cand)
    assert "🎯 الهدف 1:" in card and "📐 المتوسطات" in card


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
