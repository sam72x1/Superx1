"""اختبارات إصلاحات التدقيق: الكاش · حسم التتبّعات · حدود تيليجرام · التقويم."""

from __future__ import annotations

import os
import tempfile
from datetime import date, datetime, timezone

from runner_scanner.cache import DailyCache
from runner_scanner.config import Config
from runner_scanner import market_calendar as mc
from runner_scanner.models import (
    Candidate, Catalyst, FloatSource, MomentumResult, ReadinessResult,
    RiskPlan, Session, SnapshotEntry,
)
from runner_scanner.state import Store

CFG = Config.from_env()


# ── #2 الكاش اليومي ──────────────────────────────────────────────
def test_cache_memoizes_within_day():
    cache = DailyCache()
    calls = {"n": 0}

    def fetch():
        calls["n"] += 1
        return "data"

    assert cache.get("2026-06-26", "k", fetch) == "data"
    assert cache.get("2026-06-26", "k", fetch) == "data"
    assert calls["n"] == 1                 # جلب واحد لنفس اليوم/المفتاح


def test_cache_resets_on_new_day():
    cache = DailyCache()
    calls = {"n": 0}

    def fetch():
        calls["n"] += 1
        return calls["n"]

    cache.get("2026-06-26", "k", fetch)
    cache.get("2026-06-27", "k", fetch)    # يوم جديد → مسح وإعادة جلب
    assert calls["n"] == 2


def test_cache_ttl_refetches_after_window():
    """PERF-19: كاش TTL يعيد الجلب فقط بعد مضيّ المهلة — لا كل دورة (الخبر:
    نظرته الخلفية 48س فمحفّز عمره ~5د مقبول، ومنعُ آلاف النداءات المتطابقة)."""
    clock = {"t": 1000.0}
    cache = DailyCache(clock=lambda: clock["t"])
    calls = {"n": 0}

    def fetch():
        calls["n"] += 1
        return f"news-{calls['n']}"

    assert cache.get_ttl("2026-06-26", "news:X", 300, fetch) == "news-1"
    clock["t"] = 1200.0                     # +200ث < 300 → إصابة كاش
    assert cache.get_ttl("2026-06-26", "news:X", 300, fetch) == "news-1"
    assert calls["n"] == 1
    clock["t"] = 1400.0                     # +400ث ≥ 300 → إعادة جلب
    assert cache.get_ttl("2026-06-26", "news:X", 300, fetch) == "news-2"
    assert calls["n"] == 2


def test_cache_ttl_cleared_on_new_day():
    """PERF-19: مدخلات TTL تُمسح كذلك عند تغيّر اليوم (لا خبر أمس اليوم)."""
    clock = {"t": 0.0}
    cache = DailyCache(clock=lambda: clock["t"])
    calls = {"n": 0}

    def fetch():
        calls["n"] += 1
        return calls["n"]

    cache.get_ttl("2026-06-26", "news:X", 300, fetch)
    cache.get_ttl("2026-06-27", "news:X", 300, fetch)   # يوم جديد → إعادة جلب
    assert calls["n"] == 2


# ── #3 حسم التتبّعات المعلّقة ────────────────────────────────────
def _store():
    return Store(os.path.join(tempfile.mkdtemp(), "a.sqlite3"))


def _alert(st, ticker, day_dt):
    c = Candidate(snapshot=SnapshotEntry(ticker, 3.0, 2.4, 2.4, 3.1, 2.3,
                                         1e6, 2.8, 25.0), session=Session.REGULAR)
    c.momentum = MomentumResult(score=35, rvol=8, rvol_5min=22,
                                change_5min_pct=3, vwap_distance_pct=4,
                                above_vwap=True, volume_rising=True)
    c.readiness = ReadinessResult(classic_score=80, pillar_score=40,
                                  trend="صاعد", rsi=60, macd_bull=True,
                                  divergence="لا شيء", above_ma50=True,
                                  above_ma200=True, golden_cross=True)
    c.float_shares = 5e6
    c.float_source = FloatSource.FLOAT_ENDPOINT
    c.catalyst = Catalyst(has_news=True)
    c.final_score = 80
    c.risk = RiskPlan(stop_price=2.7, stop_pct=10, entry_ref=3.0,
                      targets=[3.6, 3.9, 4.2], stop_basis="دعم 5د")
    st.log_candidate(c, day_dt)
    st.mark_alerted(ticker, 80, day_dt)


def test_finalize_stale_closes_prior_day_open_rows():
    st = _store()
    # تنبيه أمس بقي مفتوحًا (ما اكتملت نافذته قبل الإغلاق)
    yesterday = datetime(2026, 6, 25, 19, 0, tzinfo=timezone.utc)
    _alert(st, "OLD", yesterday)
    # اليوم: نحسم المعلّقات
    closed = st.finalize_stale(datetime(2026, 6, 26, 14, 0, tzinfo=timezone.utc))
    assert closed == 1
    rows = st.fetch_resolved(only_alerts=True)
    assert any(r["ticker"] == "OLD" and r["result"] == "timeout" for r in rows)


def test_finalize_stale_skips_today():
    st = _store()
    today = datetime(2026, 6, 26, 14, 0, tzinfo=timezone.utc)
    _alert(st, "NOW", today)
    closed = st.finalize_stale(today)
    assert closed == 0                     # تتبّعات اليوم لا تُحسم مبكرًا


# ── #7 حدود تيليجرام (429) ──────────────────────────────────────
def test_retry_after_parsing():
    from runner_scanner.alerts import TelegramSender

    class _Resp:
        def __init__(self, body, headers=None):
            self._body = body
            self.headers = headers or {}

        def json(self):
            return self._body

    assert TelegramSender._retry_after(
        _Resp({"parameters": {"retry_after": 7}})) == 7.0
    assert TelegramSender._retry_after(
        _Resp({}, {"Retry-After": "3"})) == 3.0


# ── #5 تقويم العطلات ─────────────────────────────────────────────
def test_calendar_known_holidays():
    assert mc.is_holiday(date(2026, 12, 25))       # الكريسماس
    assert mc.is_holiday(date(2026, 4, 3))         # الجمعة العظيمة
    assert mc.is_holiday(date(2026, 7, 3))         # الاستقلال (مُلاحَظ)
    assert not mc.is_holiday(date(2026, 6, 29))    # اثنين عادي


def test_calendar_early_close():
    assert mc.is_early_close(date(2026, 11, 27))   # الجمعة السوداء
    assert mc.is_early_close(date(2026, 12, 24))   # عشية الكريسماس
