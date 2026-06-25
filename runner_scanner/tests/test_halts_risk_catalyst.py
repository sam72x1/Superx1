"""اختبارات التوقّفات والوقف والمحفّز والتخزين."""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone

from runner_scanner.config import Config
from runner_scanner import catalyst as cat, risk, state
from runner_scanner.halts import HaltTracker
from runner_scanner.models import (
    Bar, Candidate, Catalyst, FloatSource, HaltState, MomentumResult,
    ReadinessResult, Session,
)
from runner_scanner.tests.fixtures import make_snapshot

CFG = Config.from_env()


# ── التوقّفات ─────────────────────────────────────────────────────
def _tracker():
    clk = {"t": 0.0}
    ht = HaltTracker(CFG, resume_ignore_sec=180, t12_seconds=1800,
                     clock=lambda: clk["t"])
    return ht, clk


def test_halt_then_resume_then_normal():
    ht, clk = _tracker()
    ht.process_event({"ev": "T", "sym": "AAA", "c": [17]})
    assert ht.state_of("AAA") is HaltState.HALTED
    assert ht.is_tradeable("AAA") is False
    clk["t"] = 10
    ht.process_event({"ev": "T", "sym": "AAA", "c": [18]})
    assert ht.state_of("AAA") is HaltState.RESUMED       # داخل نافذة التجاهل
    clk["t"] = 200
    assert ht.state_of("AAA") is HaltState.NORMAL        # بعد النافذة


def test_long_halt_becomes_t12():
    ht, clk = _tracker()
    ht.process_event({"ev": "T", "sym": "BBB", "c": [17]})
    clk["t"] = 5000
    assert ht.state_of("BBB") is HaltState.T12
    assert ht.is_excluded("BBB") is True


def test_normal_trade_clears_phantom_halt():
    ht, clk = _tracker()
    ht.process_event({"ev": "T", "sym": "CCC", "c": [17]})
    clk["t"] = 5
    ht.process_event({"ev": "T", "sym": "CCC", "c": []})   # صفقة عادية
    assert ht.state_of("CCC") is HaltState.RESUMED


def test_unknown_ticker_is_normal():
    ht, _ = _tracker()
    assert ht.is_tradeable("ZZZ") is True


# ── الوقف والأهداف ────────────────────────────────────────────────
def test_stop_respects_min_distance():
    # دعم قريب جدًا → الوقف يُدفع للحد الأدنى
    bars = [Bar(t_ms=i, o=2.48, h=2.5, l=2.47, c=2.49, v=10000)
            for i in range(6)]
    rp = risk.build_risk_plan(CFG, entry=2.5, closed_bars_5min=bars)
    assert rp.stop_pct >= CFG.stop_min_pct


def test_stop_respects_max_distance():
    # دعم بعيد جدًا → الوقف يُقصّ للسقف
    bars = [Bar(t_ms=i, o=2.0, h=2.1, l=1.0, c=1.5, v=10000)
            for i in range(6)]
    rp = risk.build_risk_plan(CFG, entry=3.0, closed_bars_5min=bars)
    assert rp.stop_pct <= CFG.stop_max_pct


def test_targets_are_above_entry():
    bars = [Bar(t_ms=i, o=2.0, h=2.6, l=1.9, c=2.3, v=10000)
            for i in range(6)]
    rp = risk.build_risk_plan(CFG, entry=2.5, closed_bars_5min=bars)
    assert all(t > 2.5 for t in rp.targets)


# ── المحفّز ───────────────────────────────────────────────────────
def test_fresh_news_counts_old_does_not():
    now = datetime(2026, 6, 25, 12, 0, tzinfo=timezone.utc)
    fresh = Catalyst(has_news=True, published_utc="2026-06-25T09:00:00Z")
    old = Catalyst(has_news=True, published_utc="2026-06-20T09:00:00Z")
    assert cat.evaluate_catalyst(CFG, fresh, now).has_news is True
    assert cat.evaluate_catalyst(CFG, old, now).has_news is False
    assert cat.evaluate_catalyst(CFG, None, now).has_news is False


def test_catalyst_bonus_only_when_present():
    assert cat.catalyst_bonus(CFG, Catalyst(has_news=True)) == CFG.catalyst_score_bonus
    assert cat.catalyst_bonus(CFG, Catalyst(has_news=False)) == 0.0


# ── التخزين / منع التكرار ────────────────────────────────────────
def _store():
    path = os.path.join(tempfile.mkdtemp(), "t.sqlite3")
    return state.Store(path)


def test_dedup_persists():
    st = _store()
    assert st.already_alerted("AAA") is False
    st.mark_alerted("AAA", 80.0)
    assert st.already_alerted("AAA") is True


def test_dedup_reloads_after_restart():
    path = os.path.join(tempfile.mkdtemp(), "t.sqlite3")
    st1 = state.Store(path)
    st1.mark_alerted("BBB", 75.0)
    st1.close()
    st2 = state.Store(path)                  # محاكاة إعادة تشغيل
    assert st2.already_alerted("BBB") is True


def test_closed_loop_logs_candidate():
    st = _store()
    c = Candidate(snapshot=make_snapshot(), session=Session.REGULAR)
    c.momentum = MomentumResult(
        score=40, rvol=10, rvol_5min=22, change_5min_pct=3,
        vwap_distance_pct=5, above_vwap=True, volume_rising=True)
    c.readiness = ReadinessResult(
        classic_score=80, pillar_score=40, trend="صاعد", rsi=60,
        macd_bull=True, divergence="لا شيء", above_ma50=True,
        above_ma200=True, golden_cross=True)
    c.float_shares = 5_000_000
    c.float_source = FloatSource.FLOAT_ENDPOINT
    c.catalyst = Catalyst(has_news=True)
    c.final_score = 82.0
    st.log_candidate(c)   # لا يرفع استثناء = نجاح
