"""اختبار تكامل للحلقة الكاملة (Scanner.run_cycle) بلا إنترنت ولا تيليجرام."""

from __future__ import annotations

import os
import sqlite3
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo

from runner_scanner.config import Config
import runner_scanner.main as main_module
from runner_scanner.main import Scanner, _kronos_session_end
from runner_scanner.models import Session
from runner_scanner.tests.fixtures import FakeClient, make_snapshot

ET = ZoneInfo("America/New_York")
ET_NOW = datetime(2026, 6, 25, 10, 30, tzinfo=ET)   # جلسة رسمية


class CycleClient(FakeClient):
    """FakeClient + full_snapshot يرجّع سهم قوي + ضوضاء تُفلتر."""

    def __init__(self, *args, snapshot_price_ns=0, **kwargs):
        super().__init__(*args, **kwargs)
        self.snapshot_price_ns = snapshot_price_ns

    def full_snapshot(self):
        entries = [
            make_snapshot(ticker="STRONG", last=2.5, prev=2.0, vol=1_500_000,
                          change_pct=25.0),     # سهم قوي يُقبل
            make_snapshot(ticker="WEAK", last=5.0, prev=4.9, vol=40_000,
                          change_pct=2.0),       # تحت العتبة → لا يُكشف
            make_snapshot(ticker="PENNY", last=0.40, prev=0.30, vol=900_000,
                          change_pct=33.0),      # سعر منخفض → بوّابة ترفض
            make_snapshot(ticker="CHAMP", last=3.0, prev=2.7, vol=1_200_000,
                          change_pct=8.0),       # تحت العتبة (لكنه بطل موروث)
        ]
        for entry in entries:
            entry.price_observed_ns = self.snapshot_price_ns
        return entries


def _scanner():
    db = os.path.join(tempfile.mkdtemp(), "cycle.sqlite3")
    cfg = Config(dry_run=True, db_path=db, telegram_bot_token="x",
                 telegram_chat_id="x", massive_api_key="x", halts_enabled=False)
    sc = Scanner(cfg)
    sc.client = CycleClient()    # حقن عميل وهمي
    sc.short = None              # لا جلب شورت شبكي في الاختبارات
    sc._kronos_now_fn = lambda: ET_NOW
    return sc


def test_kronos_session_end_respects_premarket_and_early_close():
    cfg = Config(massive_api_key="x")
    early_day = datetime(2026, 11, 27, 7, 0, tzinfo=ET)

    assert _kronos_session_end(cfg, Session.PREMARKET, early_day) == datetime(
        2026, 11, 27, 9, 30, tzinfo=ET)
    assert _kronos_session_end(cfg, Session.REGULAR, early_day) == datetime(
        2026, 11, 27, 13, 0, tzinfo=ET)


def test_scanner_never_starts_kronos_worker_when_shadow_store_is_unavailable(
    tmp_path, monkeypatch,
):
    path = tmp_path / "broken-shadow.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute("CREATE VIEW kronos_forecasts AS SELECT 1 AS x")
    conn.commit()
    conn.close()

    def unexpected_client(*args, **kwargs):
        raise AssertionError("Kronos client must not start without its store")

    monkeypatch.setattr(main_module, "KronosShadowClient", unexpected_client)
    cfg = Config(
        dry_run=True,
        db_path=str(path),
        massive_api_key="x",
        halts_enabled=False,
        kronos_shadow_enabled=True,
        kronos_service_url="https://kronos.example",
    )

    scanner = Scanner(cfg)
    assert scanner.store.kronos_available is False
    assert scanner.kronos is None
    scanner.shutdown()


def test_full_cycle_sends_one_alert():
    sc = _scanner()
    sent = sc.run_cycle(et_now=ET_NOW)
    assert sent == 1
    assert sc.store.already_alerted("STRONG") is True
    # المرفوضة لم تُنبَّه
    assert sc.store.already_alerted("PENNY") is False
    sc.shutdown()


def test_accepted_candidate_is_submitted_to_kronos_shadow_and_worker_stops():
    class _Kronos:
        def __init__(self):
            self.submitted = []
            self.stop_timeouts = []
            self.is_alive = False

        def submit(self, ticker, *, session="", session_end_at=None, now=None):
            self.submitted.append((ticker, session, session_end_at, now))
            return True

        def stop(self, timeout=None):
            self.stop_timeouts.append(timeout)

    sc = _scanner()
    worker = _Kronos()
    sc.kronos = worker

    assert sc.run_cycle(et_now=ET_NOW) == 1
    assert worker.submitted == [(
        "STRONG", "رسمي",
        datetime(2026, 6, 25, 16, 0, tzinfo=ET), None,
    )]
    sc.shutdown()
    assert len(worker.stop_timeouts) == 1


def test_kronos_shadow_is_not_started_when_full_horizon_crosses_session_end():
    class _Kronos:
        is_alive = False

        def __init__(self):
            self.submitted = []

        def submit(self, ticker, **kwargs):
            self.submitted.append((ticker, kwargs))

        def stop(self, timeout=None):
            return None

    sc = _scanner()
    sc.cfg.kronos_pred_len = 66  # آخر هدف = 16:00؛ لا توجد دورة قياس بعده.
    worker = _Kronos()
    sc.kronos = worker

    assert sc.run_cycle(et_now=ET_NOW) == 1

    assert worker.submitted == []
    sc.shutdown()


def test_cycle_closes_kronos_forecast_against_snapshot_price():
    sc = _scanner()
    sc.client = CycleClient(
        snapshot_price_ns=int(ET_NOW.timestamp() * 1_000_000_000))
    asof = datetime(2026, 6, 25, 10, 0, tzinfo=ET)
    sc.store.save_kronos_forecast(
        "STRONG", asof, status="ok", model_revision="rev-1",
        returns={6: 5.0}, base_close=2.0)

    sc.run_cycle(et_now=ET_NOW)

    row = sc.store.fetch_kronos_forecasts()[0]
    assert row["actuals"] == {6: 25.0}
    assert row["completed_at"] is not None
    sc.shutdown()


def test_kronos_actual_capture_failure_never_stops_core_alert_cycle():
    sc = _scanner()

    def _fail(*args, **kwargs):
        raise RuntimeError("corrupt optional shadow row")

    sc.store.update_kronos_actuals = _fail

    assert sc.run_cycle(et_now=ET_NOW) == 1
    assert sc.store.already_alerted("STRONG") is True
    sc.shutdown()


def test_kronos_measurement_uses_time_after_snapshot_not_cycle_start():
    cycle_start = datetime(2026, 6, 25, 10, 29, 30, tzinfo=ET)
    observed_at = datetime(2026, 6, 25, 10, 30, 10, tzinfo=ET)
    measurement_now = datetime(2026, 6, 25, 10, 30, 20, tzinfo=ET)
    sc = _scanner()
    sc.client = CycleClient(
        snapshot_price_ns=int(observed_at.timestamp() * 1_000_000_000)
    )
    sc._kronos_now_fn = lambda: measurement_now
    sc.store.save_kronos_forecast(
        "STRONG", datetime(2026, 6, 25, 10, 0, tzinfo=ET),
        status="ok", model_revision="rev-1", returns={6: 5.0},
        base_close=2.0,
    )

    sc.run_cycle(et_now=cycle_start)

    assert sc.store.fetch_kronos_forecasts()[0]["actuals"] == {6: 25.0}
    sc.shutdown()


def test_dedup_prevents_second_alert_same_day():
    sc = _scanner()
    assert sc.run_cycle(et_now=ET_NOW) == 1
    # دورة ثانية بنفس اليوم → منع التكرار يصفّر الإرسال
    assert sc.run_cycle(et_now=ET_NOW) == 0
    sc.shutdown()


def test_premarket_alerts_disabled_by_default():
    """البريماركت **معطّل افتراضيًا** (أولوية الدقّة) → لا تنبيهات في البريماركت."""
    sc = _scanner()
    et_pm = datetime(2026, 6, 25, 7, 0, tzinfo=ET)   # 7ص ET = بريماركت
    assert sc.run_cycle(et_now=et_pm) == 0            # مكتوم
    assert sc.store.already_alerted("STRONG") is False
    sc.shutdown()


def _premarket_5min_bars():
    """شموع 5د في نافذة البريماركت (2026-06-25) بحجم عالٍ → RVol يعبر شرعيًا.
    بعد BUG-07 (إزالة ارتداد snap.day_volume) لم يعد الحجم اليومي يفبرك RVol
    البريماركت؛ فالتغطية الحقيقية تتطلّب حجم جلسة فعلي في نافذة البريماركت."""
    from datetime import timedelta

    from runner_scanner.models import Bar
    base = datetime(2026, 6, 25, 7, 0, tzinfo=ET)
    return [Bar(t_ms=int((base + timedelta(minutes=5 * i)).timestamp() * 1000),
                o=2.4, h=2.55, l=2.35, c=2.5, v=2_000_000, n=200)
            for i in range(6)]


def test_premarket_alerts_when_explicitly_enabled():
    """مع PREMARKET_ALERTS_ENABLED=true يُنبّه البريماركت (تغطية أوسع)."""
    db = os.path.join(tempfile.mkdtemp(), "pm.sqlite3")
    cfg = Config(dry_run=True, db_path=db, telegram_bot_token="x",
                 telegram_chat_id="x", massive_api_key="x", halts_enabled=False,
                 premarket_alerts_enabled=True)
    sc = Scanner(cfg)
    sc.client = CycleClient(bars5=_premarket_5min_bars())
    sc.short = None
    et_pm = datetime(2026, 6, 25, 7, 0, tzinfo=ET)
    assert sc.run_cycle(et_now=et_pm) == 1            # STRONG يُنبّه في البريماركت
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


class TwoGainersClient(FakeClient):
    """سهمان مؤهّلان بالسعر (لاختبار قصّ top_n على الأعلى صعودًا)."""

    def full_snapshot(self):
        return [
            make_snapshot(ticker="HI", last=3.0, prev=2.0, vol=1_500_000,
                          change_pct=40.0),     # الأعلى
            make_snapshot(ticker="LO", last=4.0, prev=3.2, vol=1_500_000,
                          change_pct=25.0),     # أقلّ صعودًا
        ]


def test_top_n_caps_to_highest_gainers():
    """top_n_runners يحصر التحليل بأعلى N صعودًا فقط."""
    db = os.path.join(tempfile.mkdtemp(), "topn.sqlite3")
    cfg = Config(dry_run=True, db_path=db, telegram_bot_token="x",
                 telegram_chat_id="x", massive_api_key="x",
                 halts_enabled=False, top_n_runners=1)
    sc = Scanner(cfg)
    sc.client = TwoGainersClient()
    sc.short = None
    sc.run_cycle(et_now=ET_NOW)
    tickers = {r["ticker"] for r in
               sc.store._conn.execute("SELECT ticker FROM tracking").fetchall()}
    # مع top_n=1: فقط الأعلى صعودًا (HI) يُعالَج، LO لا
    assert "HI" in tickers
    assert "LO" not in tickers
    sc.shutdown()


def test_detector_excludes_pennies_and_overrange_before_topn():
    """السنتات/فوق-النطاق تُستبعد في الكشف فلا تأكل مقاعد أعلى-15."""
    from runner_scanner import detector
    snaps = [
        make_snapshot("REAL", last=3.0, change_pct=30.0),
        make_snapshot("PENNY", last=0.40, change_pct=80.0),   # صعوده الأعلى لكنه سنت
        make_snapshot("HIGH", last=45.0, change_pct=50.0),    # فوق النطاق
    ]
    cfg = Config(massive_api_key="x")
    out = detector.detect_runners(cfg, snaps)
    assert [e.ticker for e in out] == ["REAL"]   # السنت لم يأخذ مقعد REAL


def test_lost_stop_alert_fault_is_not_permanent():
    """أثر جانبي لـBUG-40: مفتاح العطل `stop_alert_lost:TICKER` لم يكن يُمسح
    أبدًا (clear_fault تُستدعى لـ'api' و'scan_stall' فقط) ⇒ يتراكم مفتاح لكل
    رمز فشل إرساله ويظهر البوت «معطلًا» في /status وفي بريفنغ المستشار إلى
    الأبد بلا مسار تعافٍ. الإبلاغ حدث لحظي لا حالة قائمة."""
    sc = _scanner()
    sc.cfg.postmortem_on_stop = False          # نعزل مسار العطل وحده
    sc.telegram.send = lambda *a, **k: False   # القناة فاشلة
    sc.store.update_outcomes = lambda *a, **k: [
        {"ticker": "STRONG", "type": "stop", "price": 2.0, "gain_pct": -7.0}]

    sc.run_cycle(ET_NOW)

    lingering = [k for k in sc.monitor.active_faults()
                 if k.startswith("stop_alert_lost:")]
    assert lingering == [], f"عطل دائم بلا تعافٍ: {lingering}"


def test_shutdown_closes_db_within_render_budget_even_if_kronos_hangs():
    """كان الماسح ينتظر خيط Kronos حتى max(http_timeout, kronos_timeout)+1
    = 121ث، بينما المشرف يمنحه 15ث ثم SIGKILL ونافذة Render للخدمات ذات
    القرص 30ث ثابتة ⇒ SIGKILL يسبق store.close() فتُترك قاعدة القرص الدائم
    بلا إغلاق نظيف. الآن نلتزم بالميزانية ونغلق القاعدة في كل الأحوال."""
    import time as _time

    sc = _scanner()
    waited = []
    closed = []

    class _HungKronos:
        is_alive = True

        def stop(self, timeout=None):
            waited.append(timeout)
            _time.sleep(0.01)          # يتجاهل الطلب ويبقى حيًّا

    sc.kronos = _HungKronos()
    sc.store.close = lambda: closed.append(True)
    sc.shutdown()

    assert closed, "القاعدة لم تُغلق رغم تعلّق Kronos"
    assert waited and waited[0] <= sc.cfg.kronos_stop_timeout_sec, \
        f"انتظار {waited[0]}ث يتجاوز ميزانية {sc.cfg.kronos_stop_timeout_sec}ث"
    assert waited[0] < 30.0, "الانتظار يتجاوز نافذة Render الثابتة"
