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


def test_assistant_sha_reports_real_version(monkeypatch):
    monkeypatch.setenv("RENDER_GIT_COMMIT", "abcdef1234567")
    sc = _scanner(code_version="abcdef1")
    sent = _capture(sc)
    sc.assistant._dispatch("/sha")          # أمر صريح
    sc.assistant._dispatch("sha")           # كلمة عادية (لا تذهب لـ Claude)
    assert len(sent) == 2
    assert all("abcdef1" in m and "SHA" in m for m in sent)
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
