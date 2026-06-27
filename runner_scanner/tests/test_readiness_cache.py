"""اختبار كاش الأطر في الجاهزية (للباكتيست) — يثبت أنه **بلا أي فقد دقّة**
وأنه يعيد استخدام الأطر الثابتة (يومي/أسبوعي/شهري) دون إعادة حسابها.

البوت الحي لا يمرّر frame_cache → سلوكه غير متأثّر إطلاقًا.
"""

from __future__ import annotations

from runner_scanner import classic_ta
from runner_scanner.config import Config
from runner_scanner.tests.fixtures import uptrend_daily_bars

CFG = Config()


def test_frame_cache_is_lossless():
    """نفس النتيجة تمامًا مع الكاش وبدونه (نفس المدخلات الثابتة)."""
    daily = uptrend_daily_bars(260)
    hourly = uptrend_daily_bars(60)
    without = classic_ta.compute_readiness(CFG, daily, hourly=hourly)
    with_c = classic_ta.compute_readiness(CFG, daily, hourly=hourly, frame_cache={})
    assert without.classic_score == with_c.classic_score
    assert without.pillar_score == with_c.pillar_score
    assert without.trend == with_c.trend
    assert without.rsi == with_c.rsi
    assert without.macd_bull == with_c.macd_bull
    assert without.divergence == with_c.divergence
    assert without.above_ma50 == with_c.above_ma50
    assert without.golden_cross == with_c.golden_cross


def test_frame_cache_reuses_constant_frames(monkeypatch):
    """الاستدعاء الثاني بنفس الكاش يعيد حساب الساعة فقط (3 أطر ثابتة من الكاش)."""
    calls = {"n": 0}
    orig = classic_ta.score_timeframe

    def counting(bars):
        calls["n"] += 1
        return orig(bars)

    monkeypatch.setattr(classic_ta, "score_timeframe", counting)
    daily = uptrend_daily_bars(260)
    hourly = uptrend_daily_bars(60)
    cache: dict = {}
    classic_ta.compute_readiness(CFG, daily, hourly=hourly, frame_cache=cache)
    first = calls["n"]                       # شهري+أسبوعي+يومي+ساعة = 4
    assert first == 4
    classic_ta.compute_readiness(CFG, daily, hourly=hourly, frame_cache=cache)
    assert calls["n"] - first == 1           # الساعة فقط (الثابتة من الكاش)


def test_frame_cache_lossless_without_hourly():
    """بلا إطار ساعة أيضًا: الكاش لا يغيّر النتيجة."""
    daily = uptrend_daily_bars(260)
    without = classic_ta.compute_readiness(CFG, daily)
    with_c = classic_ta.compute_readiness(CFG, daily, frame_cache={})
    assert without.classic_score == with_c.classic_score
