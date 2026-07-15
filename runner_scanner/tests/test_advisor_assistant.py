"""اختبارات المستشار + المساعد التفاعلي + ريندر (بلا شبكة)."""

from __future__ import annotations

import os
import tempfile

from runner_scanner.config import Config
from runner_scanner.main import Scanner
from runner_scanner.render_client import RenderClient


def _scanner(**over):
    db = os.path.join(tempfile.mkdtemp(), "adv.sqlite3")
    cfg = Config(dry_run=True, db_path=db, telegram_bot_token="x",
                 telegram_chat_id="42", massive_api_key="x",
                 halts_enabled=False, **over)
    sc = Scanner(cfg)
    sc.short = None
    return sc


def _capture(sc):
    sent = []
    sc.telegram.send = lambda t: sent.append(t) or True
    return sent


# ── المساعد التفاعلي ─────────────────────────────────────────────
def test_assistant_status_and_top():
    sc = _scanner()
    sent = _capture(sc)
    sc.last_runners = [("AAA", 45.0), ("BBB", 30.0)]
    sc.assistant._dispatch("/status")
    sc.assistant._dispatch("/top")
    assert any("حالة البوت" in m for m in sent)
    assert any("AAA" in m for m in sent)
    sc.shutdown()


def test_assistant_ask_without_key():
    sc = _scanner(anthropic_api_key="")
    sent = _capture(sc)
    sc.assistant._dispatch("/ask وش الوضع؟")
    assert any("غير مفعّل" in m for m in sent)
    sc.shutdown()


def test_assistant_restart_requires_confirm():
    sc = _scanner()
    sent = _capture(sc)
    sc.assistant._dispatch("/restart")
    assert any("confirm" in m for m in sent)        # يطلب تأكيدًا
    sc.shutdown()


def test_assistant_ignores_foreign_chat():
    sc = _scanner()
    sent = _capture(sc)
    # رسالة من محادثة غير المالك → تُتجاهل
    sc.assistant._handle_update({"update_id": 1, "message": {
        "chat": {"id": 999}, "text": "/status"}})
    assert sent == []
    sc.shutdown()


def test_assistant_help():
    sc = _scanner()
    sent = _capture(sc)
    sc.assistant._dispatch("/help")
    assert any("/status" in m and "/ask" in m for m in sent)
    sc.shutdown()


def test_assistant_backtest_triggers_run():
    sc = _scanner()
    sent = _capture(sc)
    calls = []
    sc._run_backtest_bg = (
        lambda et_now, quick=False, with_grid=True: calls.append(quick))
    import time
    sc.assistant._dispatch("/backtest")            # افتراضي = سريع
    time.sleep(0.1)                                  # يُنهي الأول ويحرّر single-flight
    sc.assistant._dispatch("/backtest كامل")       # كامل
    time.sleep(0.1)
    assert any("بدء باكتيست" in m for m in sent)
    assert sorted(calls) == [False, True]            # سريع + كامل
    sc.shutdown()


def test_assistant_backtest_month_uses_explicit_dates():
    sc = _scanner()
    sent = _capture(sc)
    calls = []
    sc._run_backtest_bg = (lambda et_now, quick=False, with_grid=True,
                           start=None, end=None: calls.append((start, end)))
    sc.assistant._dispatch("/backtest 4 2025")     # أبريل 2025 كاملًا
    import time
    time.sleep(0.1)
    assert any("أبريل 2025" in m for m in sent)
    assert calls == [("2025-04-01", "2025-04-30")]
    sc.shutdown()


def test_assistant_backtest_month_tolerant_phrasing():
    """يفهم الصياغة الطبيعية «شهر 4 كامل» (يتجاهل الكلمات الزائدة)."""
    sc = _scanner()
    _capture(sc)
    calls = []
    sc._run_backtest_bg = (lambda et_now, quick=False, with_grid=True,
                           start=None, end=None: calls.append((start, end)))
    sc.assistant._dispatch("/backtest شهر 4 2025 كامل")
    import time
    time.sleep(0.1)
    assert calls == [("2025-04-01", "2025-04-30")]
    sc.shutdown()


def test_assistant_backtest_queue_runs_months_sequentially():
    """«/backtest 3 4 2025» يشغّل شهرين بالتتابع (طابور ليلي)."""
    sc = _scanner()
    _capture(sc)
    calls = []
    sc._run_backtest_bg = (lambda et_now, quick=False, with_grid=True,
                           start=None, end=None: calls.append((start, end)))
    sc.assistant._dispatch("/backtest 3 4 2025")
    import time
    time.sleep(0.2)
    # مارس ثم أبريل 2025 بالتتابع، بالترتيب
    assert calls == [("2025-03-01", "2025-03-31"),
                     ("2025-04-01", "2025-04-30")]
    sc.shutdown()


def test_assistant_single_flight_blocks_second_backtest():
    """SEC-24: باكتيست يعمل → طلب ثانٍ يُرفض (لا خيوط متوازية تحرق API)."""
    sc = _scanner()
    sent = _capture(sc)
    sc.assistant._bt_running.set()      # محاكاة باكتيست جارٍ
    sc.assistant._dispatch("/backtest كامل")
    assert any("يعمل بالفعل" in m for m in sent)
    sc.shutdown()


def test_assistant_owner_id_rejects_foreign_sender():
    """SEC-24: مع ضبط TELEGRAM_OWNER_ID، أمر من from.id غير المالك يُتجاهل
    (يحمي حين يكون chat_id مجموعة)؛ ومن المالك يُنفَّذ."""
    sc = _scanner(telegram_owner_id="7")
    sent = _capture(sc)
    sc.last_runners = [("AAA", 45.0)]
    # نفس المحادثة لكن مرسِل غريب → يُتجاهل
    sc.assistant._handle_update({"update_id": 1, "message": {
        "chat": {"id": 42}, "from": {"id": 999}, "text": "/top"}})
    assert sent == []
    # المالك → يُنفَّذ
    sc.assistant._handle_update({"update_id": 2, "message": {
        "chat": {"id": 42}, "from": {"id": 7}, "text": "/top"}})
    assert any("AAA" in m for m in sent)
    sc.shutdown()


def test_assistant_ask_rate_limited():
    """SEC-24: تجاوز حدّ /ask بالدقيقة → رفض (يكبح إنفاق Anthropic)."""
    sc = _scanner(telegram_ask_per_min=2, anthropic_api_key="x")
    sent = _capture(sc)
    # ردّ ثابت بلا شبكة (available=True لأن المفتاح مضبوط)
    sc.claude.chat = lambda *a, **k: "رد"
    for _ in range(2):
        sc.assistant._dispatch("/ask س")
    sc.assistant._dispatch("/ask س")           # الثالث يتجاوز الحدّ
    assert any("تجاوزت حدّ الأسئلة" in m for m in sent)
    assert sum(1 for m in sent if m == "رد") == 2   # مرّرنا اثنين فقط
    sc.shutdown()


def test_assistant_backtest_needs_key():
    sc = _scanner()
    sc.cfg.massive_api_key = ""               # محاكاة غياب المفتاح
    sent = _capture(sc)
    sc._run_backtest_bg = lambda et_now, quick=False: None
    sc.assistant._dispatch("/backtest")
    assert any("MASSIVE_API_KEY" in m for m in sent)
    sc.shutdown()


def test_assistant_sha_reports_real_version(monkeypatch):
    monkeypatch.setenv("RENDER_GIT_COMMIT", "abcdef1234567")
    sc = _scanner(code_version="abcdef1")
    sent = _capture(sc)
    sc.assistant._dispatch("/sha")          # أمر صريح
    sc.assistant._dispatch("sha")           # كلمة عادية (لا تذهب لـ Claude)
    assert len(sent) == 2
    assert all("abcdef1" in m and "SHA" in m for m in sent)
    sc.shutdown()


# ── بريفنغ نهاية الجلسة: اليوم الصحيح + تخطّي غير أيام التداول ────────
def test_ar_weekday_computed_in_code():
    """اسم اليوم يُحسب في الكود لا يُترك للنموذج (كان يُخطئ: الخميس بدل الجمعة)."""
    from runner_scanner import advisor
    assert advisor._ar_weekday("2026-07-03") == "الجمعة"
    assert advisor._ar_weekday("2026-07-04") == "السبت"
    assert advisor._ar_weekday("غير صالح") == ""


def test_briefing_header_has_correct_weekday():
    """ترويسة البريفنغ (fallback بلا Claude) تحمل اليوم الصحيح من التاريخ."""
    from datetime import datetime, timezone
    from runner_scanner.advisor import build_briefing

    class _Store:
        def fetch_day(self, day):
            return []

        def fetch_missed(self, pct):
            return []

    out = build_briefing(Config(anthropic_api_key=""), _Store(),
                         now=datetime(2026, 7, 3, 17, 0, tzinfo=timezone.utc))
    assert "الجمعة 2026-07-03" in out        # الجمعة لا الخميس


def _et(y, mo, d, h):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime(y, mo, d, h, 0, tzinfo=ZoneInfo("America/New_York"))


def test_briefing_skipped_on_holiday():
    """3 يوليو 2026 عطلة الاستقلال المُلاحَظة → لا بريفنغ (ليست جلسة)."""
    sc = _scanner(anthropic_api_key="")
    sent = _capture(sc)
    sc._maybe_advisor_briefing(_et(2026, 7, 3, 21))
    assert sent == []
    sc.shutdown()


def test_briefing_skipped_on_weekend():
    """السبت (4 يوليو 2026) → لا بريفنغ نهاية جلسة."""
    sc = _scanner(anthropic_api_key="")
    sent = _capture(sc)
    sc._maybe_advisor_briefing(_et(2026, 7, 4, 21))
    assert sent == []
    sc.shutdown()


def test_briefing_fires_on_trading_day_with_weekday():
    """يوم تداول عادي (الخميس 2 يوليو) → بريفنغ باليوم الصحيح."""
    sc = _scanner(anthropic_api_key="")
    sent = _capture(sc)
    sc._maybe_advisor_briefing(_et(2026, 7, 2, 21))
    assert any("بريفنغ نهاية الجلسة" in m for m in sent)
    assert any("الخميس 2026-07-02" in m for m in sent)
    sc.shutdown()


# ── ريندر ────────────────────────────────────────────────────────
def test_render_not_available_without_keys():
    cfg = Config()
    rc = RenderClient(cfg)
    assert rc.available is False
    assert "غير مربوط" in rc.summary()
    assert rc.restart() is False


def test_render_summary_with_mock(monkeypatch):
    cfg = Config(render_api_key="k", render_service_id="srv-1")
    rc = RenderClient(cfg)
    rc.service_status = lambda: {"name": "runner-scanner", "suspended": "not_suspended"}
    rc.latest_deploy = lambda: {"commit_id": "abc1234", "status": "live",
                                "commit_message": "تحديث"}
    s = rc.summary()
    assert "runner-scanner" in s and "abc1234" in s and "شغّالة" in s
