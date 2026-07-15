"""اختبارات عميل Claude (llm.py) — تصعيد تعفّن الإعداد (401/404) بلا شبكة."""

from __future__ import annotations

from runner_scanner import llm


class _Resp:
    def __init__(self, status, payload=None, text=""):
        self.status_code = status
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


def test_config_error_escalates_on_404(monkeypatch):
    """معرّف نموذج مسحوب (404) → on_config_error يُنادى (لا تحذير صامت).
    هذا الموضع الوحيد الذي يتعفّن فيه عقد خارجي بلا إنذار (التقرير)."""
    flagged = []
    client = llm.ClaudeClient("k", on_config_error=flagged.append)
    monkeypatch.setattr(llm.requests, "post",
                        lambda *a, **k: _Resp(404, text="not_found_error"))
    out = client.chat("claude-bad-model", "sys", "hi")
    assert out is None                      # best-effort: يرجّع None لا يُسقط
    assert flagged and "404" in flagged[0]  # أُبلغ عنه (سيصير عطلًا في main)


def test_transient_error_does_not_escalate(monkeypatch):
    """429/500 خطأ عابر → لا يُبلَّغ كتعفّن إعداد (يبقى تحذيرًا فقط)."""
    flagged = []
    client = llm.ClaudeClient("k", on_config_error=flagged.append)
    monkeypatch.setattr(llm.requests, "post",
                        lambda *a, **k: _Resp(429, text="rate_limited"))
    assert client.chat("m", "s", "p") is None
    assert not flagged                       # لم يُبلَّغ (عابر لا تعفّن)


def test_structured_404_escalates(monkeypatch):
    """المسار المنظّم (المحلّل) يصعّد 401/404 مثل النصّي."""
    flagged = []
    client = llm.ClaudeClient("k", on_config_error=flagged.append)
    monkeypatch.setattr(llm.requests, "post",
                        lambda *a, **k: _Resp(401, text="authentication_error"))
    tool = {"name": "t", "input_schema": {"type": "object"}}
    assert client.structured("m", "s", "p", tool) is None
    assert flagged and "401" in flagged[0]
