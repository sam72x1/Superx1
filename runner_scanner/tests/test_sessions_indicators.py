"""اختبارات الجلسات والمؤشرات."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from runner_scanner.config import Config
from runner_scanner import indicators as ind, sessions
from runner_scanner.models import Session
from runner_scanner.tests.fixtures import uptrend_daily_bars

ET = ZoneInfo("America/New_York")
CFG = Config.from_env()


def _et(h, m=0):
    return datetime(2026, 6, 25, h, m, tzinfo=ET)  # خميس (يوم تداول)


def test_session_classification():
    assert sessions.classify_session(CFG, _et(3)) is Session.CLOSED   # قبل 4ص
    assert sessions.classify_session(CFG, _et(5)) is Session.PREMARKET
    assert sessions.classify_session(CFG, _et(10)) is Session.REGULAR
    assert sessions.classify_session(CFG, _et(17)) is Session.AFTERHOURS
    assert sessions.classify_session(CFG, _et(21)) is Session.CLOSED


def test_weekend_is_closed():
    sat = datetime(2026, 6, 27, 10, 0, tzinfo=ET)
    assert sessions.classify_session(CFG, sat) is Session.CLOSED


def test_opening_range_window():
    """نافذة الافتتاح: أول 30د من الرسمي (منهجية «سهم الماركت»)."""
    assert sessions.is_opening_range(CFG, _et(9, 40))       # 9:40 ضمن أول 30د
    assert not sessions.is_opening_range(CFG, _et(10, 5))   # 10:05 خارجها
    assert not sessions.is_opening_range(CFG, _et(5))       # بريماركت ليس افتتاحًا


def test_session_move_hint_per_session():
    """الحركة النموذجية تختلف بالجلسة: الافتتاح أعلى من بقية الرسمي."""
    assert sessions.session_move_hint_pct(
        CFG, Session.REGULAR, _et(9, 40)) == CFG.session_move_open_pct
    assert sessions.session_move_hint_pct(
        CFG, Session.REGULAR, _et(11)) == CFG.session_move_regular_pct
    assert sessions.session_move_hint_pct(
        CFG, Session.PREMARKET, _et(5)) == CFG.session_move_premarket_pct
    assert sessions.session_move_hint_pct(CFG, Session.CLOSED, _et(3)) is None


def test_premarket_rvol_uses_premarket_baseline():
    # نفس الحجم: بريماركت يطلع RVol أعلى لأنه مقارن بقاعدة بريماركت الصغيرة
    pre = sessions.compute_rvol(CFG, Session.PREMARKET, 200_000, 5_000_000)
    reg = sessions.compute_rvol(CFG, Session.REGULAR, 200_000, 5_000_000,
                                elapsed_fraction=1.0)
    assert pre > reg


def test_indicators_basic():
    closes = [b.c for b in uptrend_daily_bars(250)]
    assert ind.sma(closes, 20) is not None
    assert ind.sma(closes, 999) is None        # تاريخ غير كافٍ
    r = ind.rsi(closes)
    assert r is not None and 0 <= r <= 100
    assert ind.macd(closes) is not None
    assert ind.linreg_slope_pct(closes) > 0     # صاعد
    assert ind.trend_label(5.0) == "صاعد"
    assert ind.trend_label(-5.0) == "هابط"
    assert ind.trend_label(0.2) == "عرضي"


def test_session_vwap_weighted():
    from runner_scanner.models import Bar
    bars = [Bar(t_ms=0, o=10, h=10, l=10, c=10, v=100),
            Bar(t_ms=1, o=20, h=20, l=20, c=20, v=300)]
    # المتوسط المرجّح بالحجم = (10*100 + 20*300)/400 = 17.5
    assert abs(ind.session_vwap(bars) - 17.5) < 1e-9


def test_session_vwap_empty():
    assert ind.session_vwap([]) is None


def test_session_volume_baselines_from_hourly():
    from runner_scanner.models import Bar
    bars = []
    for day in (23, 24, 25):
        for h in range(4, 20):
            dt = datetime(2026, 6, day, h, 0, tzinfo=ET)
            ms = int(dt.timestamp() * 1000)
            # بريماركت 4-8 = 50k، أفترهاوس 16-19 = 40k، رسمي = 200k
            if 4 <= h < 9:
                v = 50_000
            elif 16 <= h < 20:
                v = 40_000
            else:
                v = 200_000
            bars.append(Bar(t_ms=ms, o=2, h=2.1, l=1.9, c=2.0, v=v))
    pre, aft = sessions.session_volume_baselines(CFG, bars, today_et="2026-06-26")
    assert pre is not None and aft is not None
    assert aft == 160_000          # 4 ساعات × 40k = 160k/يوم
    # RVol بقاعدة حقيقية يختلف عن التقدير
    real = sessions.compute_rvol(CFG, Session.AFTERHOURS, 320_000, 5_000_000,
                                 avg_afterhours_volume=aft)
    assert abs(real - 2.0) < 0.01   # 320k ÷ 160k = 2.0


def test_session_volume_baselines_empty():
    assert sessions.session_volume_baselines(CFG, []) == (None, None)
