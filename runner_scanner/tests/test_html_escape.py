"""اختبارات سلامة HTML لتيليجرام — صنف بق «محرف <>& غير مُهرَّب يُسقط الرسالة».

الخلفية: كل رسالة تُرسَل بـ parse_mode="HTML". محرف < أو > حرفي واحد (أو نص
خارجي غير مُهرَّب) يجعل تيليجرام يرفض الرسالة كاملة (400) فتُفقَد بصمت — وهذا
بالضبط ما أوقف وصول «📊 تقرير الباكتيست» (تسمية ADX «ضعيف <25»).

الثابتة (invariant) المُختبَرة: بعد إزالة الوسوم المقصودة (<b>/<i>/<code>)، يجب
ألّا يبقى أي < أو > في الرسالة — وإلا فهو وسم مشوّه سيرفضه تيليجرام.
"""

from __future__ import annotations

from datetime import datetime, timezone

from runner_scanner import backtest
from runner_scanner.alerts import build_card, build_followup
from runner_scanner.config import Config

# نعيد استخدام بنّاء البطاقة الجاهز من اختبارات البطاقة
from runner_scanner.tests.test_card_news import _card_candidate

_ALLOWED_TAGS = ("<b>", "</b>", "<i>", "</i>", "<code>", "</code>")


def _assert_html_safe(msg: str) -> None:
    """يتأكّد أن الرسالة لن يرفضها تيليجرام: لا < أو > خارج الوسوم المقصودة،
    ولا & عارٍ (يجب أن يكون كيانًا &amp;/&lt;/&gt;)."""
    stripped = msg
    for tag in _ALLOWED_TAGS:
        stripped = stripped.replace(tag, "")
    assert "<" not in stripped, f"< شارد (وسم مشوّه) في:\n{msg}"
    assert ">" not in stripped, f"> شارد (وسم مشوّه) في:\n{msg}"
    # كل & يجب أن يبدأ كيانًا صالحًا (&amp; &lt; &gt;)
    for i, ch in enumerate(msg):
        if ch == "&":
            assert msg[i:i + 5] in ("&amp;",) or msg[i:i + 4] in ("&lt;", "&gt;"), \
                f"& عارٍ (غير كيان) في الموضع {i}:\n{msg}"


# ── 📊 تقرير الباكتيست: السبب المباشر لاختفاء التقرير ──────────────
def test_backtest_report_html_safe_with_adx_and_low_target():
    """ADX<25 + هدف أول <10% كانا يُدخلان محرف < حرفيًا → التقرير يُرفَض."""
    res = backtest.BacktestResult(start="x", end="y", days=1)
    base = {"session": "رسمي", "readiness": 70, "score": 70}
    # ≥3 صفقات ADX ضعيف لإظهار فئة «ضعيف تحت 25»، وبعضها هدفه < 10%
    res.trades = [
        {**base, "adx": 18, "result": "win", "realized_pct": 12,
         "target1_pct": 6, "target_hit": 1, "max_gain_pct": 12},
        {**base, "adx": 20, "result": "loss", "realized_pct": -5,
         "target1_pct": 8, "target_hit": 0, "max_gain_pct": 2},
        {**base, "adx": 22, "result": "win", "realized_pct": 9,
         "target1_pct": 7, "target_hit": 1, "max_gain_pct": 9},
    ]
    rep = backtest.format_report(res)
    _assert_html_safe(rep)
    assert "ضعيف تحت 25" in rep          # التسمية الجديدة بلا <
    assert "أقل من 10%" in rep           # السطر الجديد بلا <


# ── 🟢 بطاقة التنبيه: رمز السهم من الفيد ──────────────────────────
def test_build_card_html_safe_with_hostile_ticker():
    """رمز فيه <>& (رد API مشوّه/رمز شاذ) يجب ألّا يُسقط البطاقة."""
    cand = _card_candidate()
    cand.snapshot.ticker = "A<B&C>D"
    card = build_card(Config(code_version="x"), cand,
                      now=datetime(2026, 6, 26, 15, 31, tzinfo=timezone.utc))
    _assert_html_safe(card)
    assert "&lt;" in card and "&amp;" in card   # هُرِّبت لا أُسقِطت


# ── 🎯 رسائل المتابعة: رمز السهم في كل الفروع ─────────────────────
def test_build_followup_html_safe_all_branches():
    cfg = Config()
    for etype in ("target", "stop", "surge", "missed", "other"):
        ev = {"ticker": "X<Y&Z>", "price": 1.23, "gain_pct": 12.0,
              "type": etype, "level": 1, "reason": "زخم <ضعيف> & رفض"}
        _assert_html_safe(build_followup(cfg, ev))


# ── 🛰 ملخّص Render: اسم الخدمة + رسالة الـ commit خارجيان ─────────
def test_render_summary_html_safe_with_hostile_commit():
    from runner_scanner.render_client import RenderClient

    rc = RenderClient(Config(render_api_key="k", render_service_id="srv-x"))
    rc.service_status = lambda: {"name": "svc <prod> & co", "suspended": ""}
    rc.latest_deploy = lambda: {"commit_id": "abc123",
                                "status": "live",
                                "commit_message": "fix <Widget> & parsing"}
    _assert_html_safe(rc.summary())


# ── 🌙 بريفنغ fallback: لا هروب مزدوج لملخّص Render (مُهرَّب أصلًا) ──
def test_briefing_fallback_no_double_escape():
    """summary() يُهرّب داخليًا، فبريفنغ fallback يجب ألّا يهرّبه ثانيةً
    (وإلا يرى المستخدم &amp;amp; / &amp;lt; حرفيًا)."""
    from runner_scanner.advisor import build_briefing

    class _Store:
        def fetch_day(self, day):
            return []

        def fetch_missed(self, pct):
            return []

    summary_pre_escaped = "Render «svc &amp; co»: شغّالة ✅ · آخر نشر abc (live) fix &lt;x&gt;"
    # بلا مفتاح Claude → فرع fallback (يدمج render_summary المُهرَّب أصلًا)
    out = build_briefing(Config(), _Store(), render_summary=summary_pre_escaped)
    _assert_html_safe(out)
    assert "&amp;amp;" not in out and "&amp;lt;" not in out   # لا هروب مزدوج
    assert "&amp; co" in out and "&lt;x&gt;" in out           # الكيانات الأصلية سليمة


# ── ⚙️ معايرة: سبب شريحة RVol كان فيه < حرفي ──────────────────────
def test_calibration_proposals_html_safe():
    from runner_scanner.calibration import format_proposals, propose_calibrations

    class _Store:
        def fetch_resolved(self, only_alerts=False):
            # شريحة RVol منخفضة خاسرة → يولّد اقتراح RVOL_MIN (السبب كان فيه <)
            return ([{"result": "loss", "rvol": 6.0, "readiness": 85,
                      "score": 85, "had_news": 1} for _ in range(8)]
                    + [{"result": "win", "rvol": 12.0, "readiness": 85,
                        "score": 85, "had_news": 1} for _ in range(8)])

        def fetch_missed(self, pct):
            return []

    text = format_proposals(propose_calibrations(_Store(), Config()))
    assert "RVOL_MIN" in text
    _assert_html_safe(text)


# ── 🚨 تنبيه العطل: جسم استجابة خام فيه <>& يُسقط الرسالة (BUG-05) ─────
def test_fault_alert_html_safe():
    """raise_fault برسالة فيها HTML خام (جسم خطأ مزوّد) → المُرسَل مُهرَّب.
    قبل الإصلاح: < واحد يُسقط الرسالة الوحيدة التي تخبرك أن البوت عمي —
    وهي مزيلة التكرار فلا تُعاد أبدًا."""
    from runner_scanner.monitor import HealthMonitor
    sent = []
    mon = HealthMonitor(notify=sent.append)
    mon.raise_fault("api", 'خطأ 500 على /x: <html><body>Bad & Gateway</body></html>')
    assert sent
    _assert_html_safe(sent[0])
    assert "&amp;" in sent[0] and "&lt;html&gt;" in sent[0]   # الكيانات سليمة
