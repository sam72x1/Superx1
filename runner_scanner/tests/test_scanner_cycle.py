"""اختبار تكامل للحلقة الكاملة (Scanner.run_cycle) بلا إنترنت ولا تيليجرام."""

from __future__ import annotations

import os
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo

from runner_scanner.config import Config
from runner_scanner.main import Scanner
from runner_scanner.tests.fixtures import FakeClient, make_snapshot

ET = ZoneInfo("America/New_York")
ET_NOW = datetime(2026, 6, 25, 10, 30, tzinfo=ET)   # جلسة رسمية


class CycleClient(FakeClient):
    """FakeClient + full_snapshot يرجّع سهم قوي + ضوضاء تُفلتر."""

    def full_snapshot(self):
        return [
            make_snapshot(ticker="STRONG", last=2.5, prev=2.0, vol=1_500_000,
                          change_pct=25.0),     # سهم قوي يُقبل
            make_snapshot(ticker="WEAK", last=5.0, prev=4.9, vol=40_000,
                          change_pct=2.0),       # تحت العتبة → لا يُكشف
            make_snapshot(ticker="PENNY", last=0.40, prev=0.30, vol=900_000,
                          change_pct=33.0),      # سعر منخفض → بوّابة ترفض
            make_snapshot(ticker="CHAMP", last=3.0, prev=2.7, vol=1_200_000,
                          change_pct=8.0),       # تحت العتبة (لكنه بطل موروث)
        ]


def _scanner():
    db = os.path.join(tempfile.mkdtemp(), "cycle.sqlite3")
    cfg = Config(dry_run=True, db_path=db, telegram_bot_token="x",
                 telegram_chat_id="x", massive_api_key="x", halts_enabled=False)
    sc = Scanner(cfg)
    sc.client = CycleClient()    # حقن عميل وهمي
    sc.short = None              # لا جلب شورت شبكي في الاختبارات
    return sc


def test_full_cycle_sends_one_alert():
    sc = _scanner()
    sent = sc.run_cycle(et_now=ET_NOW)
    assert sent == 1
    assert sc.store.already_alerted("STRONG") is True
    # المرفوضة لم تُنبَّه
    assert sc.store.already_alerted("PENNY") is False
    sc.shutdown()


def test_dedup_prevents_second_alert_same_day():
    sc = _scanner()
    assert sc.run_cycle(et_now=ET_NOW) == 1
    # دورة ثانية بنفس اليوم → منع التكرار يصفّر الإرسال
    assert sc.run_cycle(et_now=ET_NOW) == 0
    sc.shutdown()


def test_champion_inherited_is_analyzed_below_threshold():
    from runner_scanner.models import Session
    from runner_scanner.state import trade_date_str
    sc = _scanner()
    day = trade_date_str(ET_NOW)
    # الرسمي يرث أبطال بري اليوم → نحفظ CHAMP كبطل بري
    sc.store.save_champions(Session.PREMARKET.value, day, [("CHAMP", 40.0, 3.0)])
    sc.run_cycle(et_now=ET_NOW)
    rows = {r["ticker"] for r in sc.store._conn.execute(
        "SELECT ticker FROM tracking").fetchall()}
    assert "CHAMP" in rows        # حُلّل رغم أنه تحت العتبة (موروث بأولوية)
    sc.shutdown()


def _add_resolved_activity(sc):
    """يضيف نتيجة محسومة (نشاط) ليُرسَل التقرير."""
    from runner_scanner.models import (
        Candidate, Catalyst, FloatSource, MomentumResult, ReadinessResult,
        RiskPlan, Session, SnapshotEntry)
    from datetime import timezone
    t0 = datetime(2026, 6, 30, 18, 0, tzinfo=timezone.utc)
    c = Candidate(snapshot=SnapshotEntry("WIN", 3.0, 2.4, 2.4, 3.1, 2.3,
                                         1_000_000, 2.8, 25.0),
                  session=Session.REGULAR)
    c.momentum = MomentumResult(score=35, rvol=8, rvol_5min=22,
                                change_5min_pct=3, vwap_distance_pct=4,
                                above_vwap=True, volume_rising=True)
    c.readiness = ReadinessResult(classic_score=80, pillar_score=40,
                                  trend="صاعد", rsi=60, macd_bull=True,
                                  divergence="لا شيء", above_ma50=True,
                                  above_ma200=True, golden_cross=True)
    c.float_shares = 5_000_000
    c.float_source = FloatSource.FLOAT_ENDPOINT
    c.catalyst = Catalyst(has_news=True)
    c.final_score = 80
    c.risk = RiskPlan(stop_price=2.7, stop_pct=10, entry_ref=3.0,
                      targets=[3.6, 3.9, 4.2], stop_basis="دعم 5د")
    sc.store.log_candidate(c, t0)
    sc.store.mark_alerted("WIN", 80, t0)
    sc.store.update_outcomes({"WIN": 3.7}, t0)


def test_report_fires_on_scheduled_day():
    from zoneinfo import ZoneInfo
    sc = _scanner()
    _add_resolved_activity(sc)
    # ثلاثاء 22:00 ET → الرياض الأربعاء 05:00 (يوم مجدوَل، بعد ساعة الفجر)
    et = datetime(2026, 6, 30, 22, 0, tzinfo=ZoneInfo("America/New_York"))
    sc._maybe_daily_report(et_now=et)
    key = et.astimezone(ZoneInfo("Asia/Riyadh")).strftime("%Y-%m-%d")
    assert sc.store.get_meta("last_dev_report") == key   # أُرسل
    # استدعاء ثانٍ نفس اليوم → لا تكرار (المفتاح ثابت)
    sc._maybe_daily_report(et_now=et)
    assert sc.store.get_meta("last_dev_report") == key
    sc.shutdown()


def test_report_skips_non_scheduled_day():
    from zoneinfo import ZoneInfo
    sc = _scanner()
    _add_resolved_activity(sc)
    # اثنين 22:00 ET → الرياض الثلاثاء (ليس ضمن الأربعاء/السبت)
    et = datetime(2026, 6, 29, 22, 0, tzinfo=ZoneInfo("America/New_York"))
    sc._maybe_daily_report(et_now=et)
    assert sc.store.get_meta("last_dev_report") is None   # لم يُرسل
    sc.shutdown()


def test_cycle_logs_tracking_for_all_processed():
    sc = _scanner()
    sc.run_cycle(et_now=ET_NOW)
    # جدول tracking يحوي مدخلات (المقبول + المرفوضين المُعالَجين)
    rows = sc.store._conn.execute(
        "SELECT ticker, rejected FROM tracking").fetchall()
    tickers = {r["ticker"] for r in rows}
    assert "STRONG" in tickers
    sc.shutdown()


def test_top_n_caps_to_highest_gainers():
    """top_n_runners يحصر التحليل بأعلى N صعودًا فقط."""
    db = os.path.join(tempfile.mkdtemp(), "topn.sqlite3")
    cfg = Config(dry_run=True, db_path=db, telegram_bot_token="x",
                 telegram_chat_id="x", massive_api_key="x",
                 halts_enabled=False, top_n_runners=1)
    sc = Scanner(cfg)
    sc.client = CycleClient()    # PENNY +33% أعلى من STRONG +25%
    sc.short = None
    sc.run_cycle(et_now=ET_NOW)
    tickers = {r["ticker"] for r in
               sc.store._conn.execute("SELECT ticker FROM tracking").fetchall()}
    # مع top_n=1: فقط الأعلى صعودًا (PENNY) يُعالَج، STRONG لا
    assert "PENNY" in tickers
    assert "STRONG" not in tickers
    sc.shutdown()
