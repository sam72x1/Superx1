"""اختبارات تثبيت إصلاحات التدقيق (لا ترجع البقول المؤكَّدة)."""

from __future__ import annotations

from datetime import date

from runner_scanner.alerts import _TG_LIMIT, _split_message, build_card
from runner_scanner.catalyst import catalyst_bonus
from runner_scanner.config import Config
from runner_scanner.models import AnalystResult, Candidate, Catalyst, Session
from runner_scanner.sec_radar import SecRadar
from runner_scanner.tests.fixtures import make_snapshot

TODAY = date(2026, 6, 26)
_CIK_MAP = {"0": {"cik_str": 11111, "ticker": "DILUT", "title": "X"}}
_CIK10 = "0000011111"


# ── #2/#6: هروب HTML في البطاقة (نص خارجي/محلّل) ─────────────────
def test_card_escapes_external_headline_and_analyst():
    cfg = Config()
    c = Candidate(snapshot=make_snapshot(), session=Session.REGULAR)
    c.final_score = 80
    c.catalyst = Catalyst(has_news=True, headline="Q3 <rev> & AT&T deal",
                          publisher="A & B", category="📑 عقد/صفقة")
    c.analyst = AnalystResult(direction="صعودي", thesis="strong < 2x P&L",
                              materiality=8)
    card = build_card(cfg, c)
    assert "&lt;rev&gt;" in card and "&amp;" in card
    assert "<rev>" not in card        # لا وسم خام يتسرّب
    assert "&lt; 2x P&amp;L" in card


# ── #3: خبر الطرح/التخفيف لا يُمنح مكافأة درجة ────────────────────
def test_negative_catalyst_no_bonus():
    cfg = Config()
    neg = Catalyst(has_news=True, headline="announces public offering",
                   category="⚠️ طرح/تخفيف (سلبي)")
    assert catalyst_bonus(cfg, neg) == 0.0


def test_negative_catalyst_classified_on_the_fly():
    cfg = Config()
    neg = Catalyst(has_news=True, headline="priced shelf offering")  # بلا category
    assert catalyst_bonus(cfg, neg) == 0.0


def test_positive_catalyst_keeps_bonus():
    cfg = Config()
    pos = Catalyst(has_news=True, headline="FDA approval granted",
                   category="💊 موافقة/تجارب سريرية")
    assert catalyst_bonus(cfg, pos) == cfg.catalyst_score_bonus


# ── #1: كاش CIK لا يتسمّم عند فشل عابر (يعيد المحاولة) ────────────
class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class _FlakySession:
    """يفشل تحميل خريطة CIK أول مرة، ثم ينجح."""

    def __init__(self):
        self.headers = {}
        self.map_calls = 0
        self._subs = {"filings": {"recent": {
            "form": ["424B5"], "filingDate": ["2026-06-10"]}}}

    def get(self, url, timeout=12):
        if "company_tickers" in url:
            self.map_calls += 1
            return _Resp(503, None) if self.map_calls == 1 else _Resp(200, _CIK_MAP)
        if _CIK10 in url:
            return _Resp(200, self._subs)
        return _Resp(404, None)


# ── رسالة طويلة تُقسَّم بدل أن تُرفَض (4096) ──────────────────────
def test_short_message_not_split():
    assert _split_message("سطر قصير") == ["سطر قصير"]


def test_long_message_split_under_limit():
    text = "\n".join(f"سطر رقم {i} بمحتوى كافٍ للحشو" * 6 for i in range(400))
    chunks = _split_message(text)
    assert len(chunks) > 1
    assert all(len(c) <= _TG_LIMIT for c in chunks)
    # لا يضيع محتوى (تجميع الأجزاء يعيد النص بفواصل أسطر)
    assert "\n".join(chunks).replace("\n", "") == text.replace("\n", "")


def test_cik_map_retries_after_transient_failure():
    radar = SecRadar(Config(), session=_FlakySession())
    # أول استدعاء: فشل تحميل الخريطة → None (لكن لا يتجمّد)
    assert radar.check("DILUT", today=TODAY) is None
    assert radar._cik_map is None      # لم يُكاش فارغًا
    # ثاني استدعاء: المصدر صار متاحًا → يُحمّل ويعمل
    res = radar.check("DILUT", today=TODAY)
    assert res is not None and res.risk == "مرتفع"
