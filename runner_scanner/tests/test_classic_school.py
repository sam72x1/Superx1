"""اختبارات مدرسة التحليل الكلاسيكي: الشموع + ADX + بولينجر + StochRSI."""

from __future__ import annotations

from runner_scanner.candles import candle_signal
from runner_scanner.config import Config
from runner_scanner import classic_ta, indicators as ind
from runner_scanner.models import Bar
from runner_scanner.tests.fixtures import (
    downtrend_daily_bars, uptrend_daily_bars,
)

CFG = Config.from_env()


# ── المعايرة (الأهم: لا تنكسر بوّابة ≥70) ────────────────────────
def test_readiness_calibration_preserved():
    up = classic_ta.compute_readiness(CFG, uptrend_daily_bars())
    dn = classic_ta.compute_readiness(CFG, downtrend_daily_bars())
    assert up.classic_score >= 70      # صاعد جاهز
    assert dn.classic_score < 70       # هابط غير جاهز


def test_readiness_exposes_new_fields():
    up = classic_ta.compute_readiness(CFG, uptrend_daily_bars())
    assert up.adx > 0
    assert 0.0 <= up.stoch_rsi <= 1.0
    assert up.bb_pct_b is not None


# ── ADX / DMI ────────────────────────────────────────────────────
def test_adx_uptrend_bullish_di():
    bars = uptrend_daily_bars()
    adx, plus_di, minus_di = ind.adx_dmi(
        [b.h for b in bars], [b.l for b in bars], [b.c for b in bars])
    assert plus_di > minus_di          # تحيّز صاعد
    assert adx > 20                    # اتجاه فعّال


def test_adx_none_on_short_history():
    assert ind.adx_dmi([1, 2], [1, 2], [1, 2]) is None


# ── بولينجر %B + StochRSI ────────────────────────────────────────
def test_bollinger_pct_b_range():
    rising = list(range(1, 40))
    pb = ind.bollinger_pct_b(rising)
    assert pb is not None and pb > 0.5   # سعر صاعد فوق الوسط
    assert ind.bollinger_pct_b([1, 2, 3]) is None


def test_stoch_rsi_bounds():
    import math
    closes = [10 + math.sin(i / 3) + i * 0.05 for i in range(60)]
    sr = ind.stoch_rsi(closes)
    assert sr is not None and 0.0 <= sr <= 1.0


# ── نماذج الشموع (بسياق الاتجاه) ─────────────────────────────────
def _uptrend_then(last_bars):
    base = [Bar(t_ms=i, o=5 + i * 0.1, h=5.25 + i * 0.1, l=4.95 + i * 0.1,
                c=5.2 + i * 0.1, v=1000) for i in range(8)]
    return base + last_bars


def test_bearish_engulfing_after_uptrend():
    bars = _uptrend_then([
        Bar(t_ms=8, o=5.85, h=5.95, l=5.82, c=5.92, v=1000),   # صاعدة صغيرة
        Bar(t_ms=9, o=5.94, h=5.96, l=5.6, c=5.62, v=2000),    # هابطة تبتلع
    ])
    sig, name = candle_signal(bars)
    assert sig < 0                      # إشارة هبوطية (تحذير قمة)


def test_shooting_star_after_uptrend():
    bars = _uptrend_then([
        Bar(t_ms=8, o=6.0, h=6.6, l=5.98, c=6.05, v=1500),     # ظل علوي طويل
    ])
    sig, name = candle_signal(bars)
    assert sig < 0


def test_three_white_soldiers_bullish():
    bars = [Bar(t_ms=i, o=5, h=5.1, l=4.9, c=5.0, v=1000) for i in range(5)]
    bars += [
        Bar(t_ms=5, o=5.0, h=5.5, l=4.98, c=5.45, v=1000),
        Bar(t_ms=6, o=5.3, h=5.9, l=5.28, c=5.85, v=1000),
        Bar(t_ms=7, o=5.7, h=6.3, l=5.68, c=6.25, v=1000),
    ]
    sig, name = candle_signal(bars)
    assert sig > 0                      # إشارة صعودية


def test_candle_signal_empty_on_short():
    assert candle_signal([]) == (0.0, "")
