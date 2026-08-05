"""الحلقة المغلقة: صفقاتك الفعلية + صدق تنبيه الفرصة الفائتة.

البوت كان يعرف ما **اقترحه** ولا يعرف ما **فعلتَه** — فكل تحليلاته عن أداء
الاقتراحات لا عن أدائك (توقيت دخولك · حجمك · انزلاقك). وتنبيه «الفرصة
الفائتة» كان يعرض القمة وحدها وهي FOMO خالص.
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone

import pytest

from runner_scanner.alerts import build_followup
from runner_scanner.config import Config
from runner_scanner.state import Store

T0 = datetime(2026, 7, 29, 14, 0, tzinfo=timezone.utc)


@pytest.fixture()
def store():
    fd, path = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    try:
        yield Store(path)
    finally:
        os.unlink(path)


def test_open_and_close_round_trip(store):
    """دخول ثم خروج: الأرقام تعود كما سُجّلت."""
    store.open_trade("RMZ", 300, 10.40, T0)
    assert len(store.my_open_trades()) == 1
    row = store.close_trade("RMZ", 11.20, T0)
    assert row is not None
    assert row["shares"] == 300 and row["entry"] == 10.40
    assert row["exit_price"] == 11.20
    assert store.my_open_trades() == []
    assert len(store.my_closed_trades()) == 1


def test_close_without_open_returns_none(store):
    """خروج بلا دخول مسجَّل ⇒ None (المساعد يردّ برسالة، لا ينهار)."""
    assert store.close_trade("NOPE", 5.0, T0) is None


def test_fifo_when_multiple_positions_same_ticker(store):
    """صفقتان على نفس الرمز ⇒ يُغلق الأقدم أولًا (FIFO)، لا الأحدث."""
    store.open_trade("RMZ", 100, 10.0, T0)
    store.open_trade("RMZ", 200, 12.0, T0)
    row = store.close_trade("RMZ", 13.0, T0)
    assert row["entry"] == 10.0 and row["shares"] == 100
    assert len(store.my_open_trades()) == 1
    assert store.my_open_trades()[0]["entry"] == 12.0


def test_ticker_normalised(store):
    """رمز بحروف صغيرة أو بعلامة $ ⇒ يُطبَّع فيجده الإغلاق."""
    store.open_trade("rmz", 50, 9.0, T0)
    assert store.close_trade("RMZ", 9.5, T0) is not None


def test_my_trades_survives_existing_db(store):
    """§7: القرص دائم — الجدول يُنشأ على قاعدة قائمة بلا كسر."""
    store.open_trade("AAA", 10, 1.0, T0)
    reopened = Store(store._conn.execute(
        "PRAGMA database_list").fetchone()[2])
    assert len(reopened.my_open_trades()) == 1


def test_missed_alert_shows_trough_not_just_peak():
    """BUG-41: القمة وحدها تخدع. بيانات المستخدم: 412 مرفوض RVol وسيط قمتهم
    +1.92% ووسيط قاعهم −7.31% و52% لمسوا الوقف. الرسالة الآن تحمل القاع."""
    cfg = Config(massive_api_key="x")
    msg = build_followup(cfg, {
        "ticker": "RMZ", "type": "missed", "price": 14.0, "gain_pct": 40.0,
        "draw_pct": -9.0, "hit_stop": True, "stop_first": True,
        "reason": "RVol 0.7x < 5.0x"})
    assert "+40%" in msg              # القمة باقية
    assert "-9%" in msg               # ومعها القاع
    assert "وقفه لُمس قبل قمته" in msg   # والحسم الزمني
    assert "خسارة لا فرصة" in msg


def test_missed_alert_backward_compatible_without_context():
    """صفوف قديمة بلا قاع/طوابع ⇒ الرسالة تبقى صالحة بلا سياق مُختلَق."""
    cfg = Config(massive_api_key="x")
    msg = build_followup(cfg, {
        "ticker": "OLD", "type": "missed", "price": 5.0, "gain_pct": 33.0,
        "reason": "فلوت"})
    assert "+33%" in msg and "قاعه" not in msg
