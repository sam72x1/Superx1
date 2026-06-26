"""اختبارات المحلّل الذكي (Claude) — بعميل وهمي (بلا شبكة)."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from runner_scanner.analyst import ClaudeAnalyst
from runner_scanner.config import Config
from runner_scanner.models import AnalystResult, Candidate, Catalyst, Session
from runner_scanner.pipeline import process_candidate
from runner_scanner.tests.fixtures import FakeClient, make_snapshot

ET = ZoneInfo("America/New_York")
ET_NOW = datetime(2026, 6, 26, 10, 30, tzinfo=ET)


class _FakeClaude:
    available = True

    def __init__(self, payload):
        self._payload = payload

    def structured(self, model, system, prompt, tool, max_tokens=700):
        return self._payload


def _analyst(payload, enabled=True, key="x"):
    cfg = Config(analyst_enabled=enabled, anthropic_api_key=key)
    return ClaudeAnalyst(cfg, client=_FakeClaude(payload))


def _cand():
    c = Candidate(snapshot=make_snapshot())
    c.catalyst = Catalyst(has_news=True, headline="X offering",
                          publisher="GlobeNewswire")
    return c


def test_analyst_parses_result():
    an = _analyst({"catalyst_type": "شراكة", "direction": "صعودي",
                   "materiality": 7, "thesis": "أطروحة قوية", "warning": ""})
    res = an.analyze(_cand())
    assert res.direction == "صعودي" and res.materiality == 7
    assert res.is_bearish is False


def test_analyst_flags_bearish():
    res = AnalystResult(direction="هبوطي", warning="طرح مخفِّف")
    assert res.is_bearish is True
    res2 = AnalystResult(direction="صعودي", warning="تحذير سيولة")
    assert res2.is_bearish is True            # أي تحذير = هبوطي


def test_analyst_disabled_or_no_key_returns_none():
    assert _analyst({}, enabled=False).analyze(_cand()) is None
    assert _analyst({}, key="").analyze(_cand()) is None


def test_pipeline_bearish_analyst_penalizes():
    # محلّل يرجّع محفّزًا هبوطيًا قويًا → خصم قد يُسقط التنبيه
    bearish = _analyst({"direction": "هبوطي", "materiality": 9,
                        "thesis": "طرح مخفِّف", "warning": "offering يقتل الرَنر"})
    cfg = Config(analyst_enabled=True, anthropic_api_key="x",
                 analyst_bearish_penalty=30.0)
    cand = process_candidate(
        cfg, FakeClient(), make_snapshot(change_pct=25.0),
        halts=None, session=Session.REGULAR, et_now=ET_NOW, analyst=bearish)
    # الخصم الكبير (30) يُسقط الدرجة تحت العتبة → رفض المحفّز الهبوطي
    assert cand.is_rejected is True
    assert "هبوطي" in (cand.rejected_reason or "")


def test_pipeline_bullish_analyst_keeps_alert():
    bullish = _analyst({"direction": "صعودي", "materiality": 8,
                        "thesis": "محفّز قوي", "warning": ""})
    cfg = Config(analyst_enabled=True, anthropic_api_key="x")
    cand = process_candidate(
        cfg, FakeClient(), make_snapshot(change_pct=25.0),
        halts=None, session=Session.REGULAR, et_now=ET_NOW, analyst=bullish)
    assert cand.is_rejected is False
    assert cand.analyst is not None and cand.analyst.direction == "صعودي"
