"""اختبارات ركيزتي التحليل + الدمج والشروط."""

from __future__ import annotations

from runner_scanner.config import Config
from runner_scanner import classic_ta, intraday_ta, scoring
from runner_scanner.models import (
    Candidate, FloatSource, MomentumResult, ReadinessResult, Session,
)
from runner_scanner.tests.fixtures import (
    downtrend_daily_bars, make_snapshot, rising_5min_bars, uptrend_daily_bars,
)

CFG = Config.from_env()


def test_readiness_uptrend_scores_high():
    r = classic_ta.compute_readiness(CFG, uptrend_daily_bars())
    assert r.classic_score >= 70
    assert r.trend == "صاعد"
    assert r.limited_history is False


def test_readiness_downtrend_scores_low():
    r = classic_ta.compute_readiness(CFG, downtrend_daily_bars())
    assert r.classic_score < 70
    assert r.trend == "هابط"


def test_readiness_limited_history_flagged():
    r = classic_ta.compute_readiness(CFG, uptrend_daily_bars(30))
    assert r.limited_history is True


def test_momentum_rising_runner():
    snap = make_snapshot(last=2.45)
    m = intraday_ta.compute_momentum(
        CFG, snap, Session.REGULAR, rising_5min_bars(),
        avg_daily_volume=2_000_000, elapsed_fraction=0.2)
    assert 0 <= m.score <= CFG.momentum_pillar_max
    assert m.rvol > 0
    assert m.above_vwap in (True, False)


def _ready_cand(classic=80.0, momentum=35.0, news=False, float_known=True):
    c = Candidate(snapshot=make_snapshot(), session=Session.REGULAR)
    c.momentum = MomentumResult(
        score=momentum, rvol=10, rvol_5min=22, change_5min_pct=3,
        vwap_distance_pct=5, above_vwap=True, volume_rising=True)
    c.readiness = ReadinessResult(
        classic_score=classic, pillar_score=classic / 100 * 50, trend="صاعد",
        rsi=60, macd_bull=True, divergence="لا شيء", above_ma50=True,
        above_ma200=True, golden_cross=True)
    if float_known:
        c.float_shares = 5_000_000
        c.float_source = FloatSource.FLOAT_ENDPOINT
    from runner_scanner.models import Catalyst
    c.catalyst = Catalyst(has_news=news)
    return c


def test_scoring_rejects_below_readiness_gate():
    c = _ready_cand(classic=60.0)   # < 70
    res = scoring.score_candidate(CFG, c)
    assert res.passed is False and "جاهزية" in res.reason


def test_scoring_rejects_weak_momentum():
    c = _ready_cand(classic=85.0, momentum=10.0)   # < floor 25
    res = scoring.score_candidate(CFG, c)
    assert res.passed is False and "زخم" in res.reason


def test_scoring_accepts_strong_candidate():
    c = _ready_cand(classic=85.0, momentum=40.0)
    res = scoring.score_candidate(CFG, c)
    assert res.passed is True
    assert res.final_score >= CFG.alert_score_min


def test_news_is_bonus_not_gate():
    without = scoring.score_candidate(CFG, _ready_cand(news=False)).final_score
    with_news = scoring.score_candidate(CFG, _ready_cand(news=True)).final_score
    assert with_news > without              # الخبر يرفع الدرجة
    # لكن غياب الخبر لا يرفض (لو بقية الشروط قوية)
    assert scoring.score_candidate(CFG, _ready_cand(news=False)).passed is True


def test_unknown_float_penalized_not_rejected():
    c = _ready_cand(float_known=False)
    c.float_shares = None
    res = scoring.score_candidate(CFG, c)
    assert res.passed is True   # لا رفض، فقط خصم
