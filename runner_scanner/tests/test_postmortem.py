"""اختبارات تشريح الفشل — قاعدي (بلا مفتاح) + ذكي (Claude وهمي)."""

from __future__ import annotations

from runner_scanner import postmortem
from runner_scanner.config import Config


def _row(**kw):
    base = {
        "ticker": "TEST", "session": "رسمي", "result": "loss",
        "max_gain_pct": 1.0, "max_draw_pct": -12.0, "first_price": 3.0,
        "stop_price": 2.7, "score": 72, "readiness": 75, "momentum": 28,
        "rvol": 6.0, "rvol_5min": 10.0, "had_news": 0, "catalyst_head": None,
        "dilution_risk": None, "short_pct": None, "analyst_dir": None,
    }
    base.update(kw)
    return base


class _FakeClaude:
    available = True

    def __init__(self, payload):
        self._p = payload

    def structured(self, model, system, prompt, tool, max_tokens=350):
        return self._p


def test_rule_reason_flags_dilution():
    cause, lesson = postmortem._rule_reason(_row(dilution_risk="مرتفع"))
    assert "تخفيف" in cause and lesson


def test_rule_reason_flags_high_short():
    cause, _ = postmortem._rule_reason(_row(short_pct=35.0))
    assert "شورت" in cause


def test_rule_reason_flags_no_catalyst():
    cause, _ = postmortem._rule_reason(_row(had_news=0))
    assert "محفّز" in cause


def test_rule_reason_timeout():
    cause, lesson = postmortem._rule_reason(_row(result="timeout"))
    assert "النافذة" in cause


def test_rule_reason_win_is_positive():
    cause, _ = postmortem._rule_reason(
        _row(result="win", had_news=1, readiness=85, momentum=40))
    assert "نجاح" in cause


def test_explain_uses_claude_when_available():
    cfg = Config(anthropic_api_key="x", postmortem_enabled=True)
    fake = _FakeClaude({"cause": "طرح مخفِّف ضغط السعر", "lesson": "راقب SEC"})
    cause, lesson = postmortem.explain(cfg, _row(), client=fake)
    assert cause == "طرح مخفِّف ضغط السعر" and lesson == "راقب SEC"


def test_explain_falls_back_without_key():
    cfg = Config(anthropic_api_key="", postmortem_enabled=True)
    cause, lesson = postmortem.explain(cfg, _row(dilution_risk="متوسط"))
    assert "تخفيف" in cause   # تفسير قاعدي


def test_failure_message_format():
    cfg = Config(anthropic_api_key="")
    msg = postmortem.build_failure_message(cfg, _row(result="loss"))
    assert "تشريح" in msg and "$TEST" in msg and "كسر الوقف" in msg


def test_why_message_handles_win():
    cfg = Config(anthropic_api_key="")
    msg = postmortem.build_why_message(cfg, _row(result="win", had_news=1))
    assert "$TEST" in msg and "نجاح" in msg


def test_why_message_explains_rejection():
    cfg = Config(anthropic_api_key="")
    rejected = _row(result="", rejected=1, is_alert=0, change_pct=95.0,
                    reject_reason="جاهزية فنية 45 < 60 (غير جاهز فنيًا)")
    msg = postmortem.build_why_message(cfg, rejected)
    # السبب يظهر مهرَّبًا (45 &lt; 60) — هروب HTML مقصود
    assert "لم يُنبَّه" in msg and "جاهزية فنية 45 &lt; 60" in msg
    assert "+95%" in msg
