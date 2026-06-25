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
    """FakeClient + full_snapshot يرجّع رَنر قوي + ضوضاء تُفلتر."""

    def full_snapshot(self):
        return [
            make_snapshot(ticker="STRONG", last=2.5, prev=2.0, vol=1_500_000,
                          change_pct=25.0),     # رَنر قوي يُقبل
            make_snapshot(ticker="WEAK", last=5.0, prev=4.9, vol=40_000,
                          change_pct=2.0),       # تحت العتبة → لا يُكشف
            make_snapshot(ticker="PENNY", last=0.40, prev=0.30, vol=900_000,
                          change_pct=33.0),      # سعر منخفض → بوّابة ترفض
        ]


def _scanner():
    db = os.path.join(tempfile.mkdtemp(), "cycle.sqlite3")
    cfg = Config(dry_run=True, db_path=db, telegram_bot_token="x",
                 telegram_chat_id="x", massive_api_key="x", halts_enabled=False)
    sc = Scanner(cfg)
    sc.client = CycleClient()    # حقن عميل وهمي
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


def test_cycle_logs_closed_loop_for_all_processed():
    sc = _scanner()
    sc.run_cycle(et_now=ET_NOW)
    # closed_loop يحوي مدخلات (المقبول + المرفوضين المُعالَجين)
    rows = sc.store._conn.execute(
        "SELECT ticker, rejected FROM closed_loop").fetchall()
    tickers = {r["ticker"] for r in rows}
    assert "STRONG" in tickers
    sc.shutdown()
