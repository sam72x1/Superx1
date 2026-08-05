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


# ── المسار الحيّ: عبر معالِجات المساعد لا Store مباشرة ─────────────
def _assistant(store):
    """مساعد تيليجرام بأدنى ماسح وهمي + التقاط الردود."""
    from runner_scanner.telegram_bot import TelegramAssistant

    class _Sc:
        cfg = Config(massive_api_key="x", telegram_bot_token="x",
                     telegram_chat_id="1")
    sc = _Sc()
    sc.store = store
    a = TelegramAssistant(sc)
    a.sent = []
    a._reply = lambda t, **k: a.sent.append(t)
    return a


def test_trade_commands_run_through_the_assistant(store):
    """BUG-43: كل معالِجات الصفقات كانت تنادي self.store — وهو **غير موجود**
    (بقية المعالِجات تستخدم self.sc.store) ⇒ الأوامر الثلاثة تنهار بـ
    AttributeError. اختباراتي السابقة نادت Store مباشرة فما لمست المسار
    الحيّ ولا مرّة — نفس نمط الفشل الذي تكرّر ثلاث مرّات هنا."""
    a = _assistant(store)
    a._handle_trade_open("RMZ 300 10.40")
    assert "سُجّل دخولك" in a.sent[-1]
    a._handle_trade_close("RMZ 11.20")
    assert "أُغلقت" in a.sent[-1]
    txt = a._my_trades_text()
    assert "صفقاتك الفعلية" in txt and "المغلقة: 1" in txt


def test_breakeven_trade_is_not_counted_as_a_loss(store):
    """صفقة على تعادل تامّ كانت تُعرض ✅ عند إغلاقها وتُحسب 🛑 في الملخّص —
    تناقض داخلي. وهو ليس هامشيًّا هنا: الباكتيست يقيس 72 من 123 صفقة (59%)
    تُغلق على تعادل تامّ بقاعدة ترقية الوقف التي تنصح بها البطاقة، فحشرها
    في الخسائر يشوّه سجلّ المستخدم تشويهًا كبيرًا."""
    a = _assistant(store)
    store.open_trade("WIN", 100, 10.0, T0)
    store.close_trade("WIN", 11.0, T0)
    store.open_trade("EVEN", 100, 10.0, T0)
    store.close_trade("EVEN", 10.0, T0)      # تعادل تامّ
    store.open_trade("LOSS", 100, 10.0, T0)
    store.close_trade("LOSS", 9.0, T0)

    txt = a._my_trades_text()
    assert "1✅" in txt and "1🛑" in txt, "التعادل حُشر مع الخسائر"
    assert "تعادل" in txt, "التعادل غير معروض رغم أنه الحالة الأشيع"
    # متوسط الخسارة يخصّ الخسائر وحدها لا مخفَّفًا بصفر التعادل
    assert "-100$" in txt.replace(",", "")
