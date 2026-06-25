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
