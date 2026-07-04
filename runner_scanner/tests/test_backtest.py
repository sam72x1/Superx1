"""اختبارات المُختبِر التاريخي (Backtester) — بعميل أساس وهمي (بلا شبكة).

يتحقّق من: تقويم أيام التداول · محاكاة النتيجة (تحفّظ) · قصّ AsOfClient
(لا تسرّب مستقبل) · تشغيل كامل end-to-end بمحاكاة.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from runner_scanner import backtest
from runner_scanner.config import Config
from runner_scanner.models import Bar, FloatSource, RiskPlan
from runner_scanner.tests.fixtures import uptrend_daily_bars

ET = ZoneInfo("America/New_York")


def _tms(y, mo, d, h, mi):
    return int(datetime(y, mo, d, h, mi, tzinfo=ET).timestamp() * 1000)


# ── تقويم أيام التداول ────────────────────────────────────────────
def test_trading_days_skips_weekends_and_holidays():
    days = backtest.trading_days("2026-07-01", "2026-07-07")
    assert "2026-07-04" not in days       # السبت
    assert "2026-07-05" not in days       # الأحد
    # 4 يوليو 2026 سبت (عطلة الاستقلال تُلاحَظ)؛ نتأكد أيام الأسبوع موجودة
    assert "2026-07-01" in days and "2026-07-02" in days


# ── محاكاة النتيجة ────────────────────────────────────────────────
def _risk(stop, t1):
    return RiskPlan(stop_price=stop, stop_pct=10, entry_ref=3.0,
                    targets=[t1, t1 * 1.1, t1 * 1.2], stop_basis="x")


def test_outcome_win():
    post = [Bar(t_ms=1000, o=3.0, h=3.7, l=3.0, c=3.6, v=1000)]
    res, gain, _, lvl = backtest.simulate_outcome(3.0, _risk(2.7, 3.6), post, 0, 90)
    assert res == "win" and gain > 0 and lvl >= 1


def test_outcome_loss():
    post = [Bar(t_ms=1000, o=3.0, h=3.1, l=2.6, c=2.7, v=1000)]
    res, _, draw, _ = backtest.simulate_outcome(3.0, _risk(2.7, 3.6), post, 0, 90)
    assert res == "loss" and draw < 0


def test_outcome_both_in_bar_is_loss_conservative():
    # شمعة لمست الهدف والوقف معًا → تحفّظ: خسارة
    post = [Bar(t_ms=1000, o=3.0, h=3.7, l=2.6, c=3.0, v=1000)]
    res, _, _, _ = backtest.simulate_outcome(3.0, _risk(2.7, 3.6), post, 0, 90)
    assert res == "loss"


def test_outcome_timeout_after_window():
    # شمعة خارج النافذة لا تُحتسب
    post = [Bar(t_ms=10 * 60_000 + 1, o=3.0, h=3.7, l=3.0, c=3.6, v=1000)]
    res, _, _, _ = backtest.simulate_outcome(3.0, _risk(2.7, 3.6), post, 0, 5)
    assert res == "timeout"


def test_outcome_target_frozen_after_stop_post_win():
    """م2: بعد الفوز عند هدف1، إن كُسر الوقف قبل هدف2 لا يُعدّ هدف2 لاحقًا
    (سيناريو الإمساك يفترض وقفًا عند التعادل — تجاوزه تفاؤل زائف)."""
    # دخول 3.0 · وقف 2.7 · أهداف [3.6, 3.96, 4.32]
    post = [Bar(t_ms=1000, o=3.0, h=3.7, l=3.0, c=3.6, v=1000),   # لمس هدف1 → فوز
            Bar(t_ms=2000, o=3.6, h=3.0, l=2.6, c=2.8, v=1000),   # كسر الوقف (2.6≤2.7)
            Bar(t_ms=3000, o=2.8, h=4.0, l=2.8, c=3.9, v=1000)]   # هدف2 بعد الكسر
    res, _, _, lvl = backtest.simulate_outcome(3.0, _risk(2.7, 3.6), post, 0, 90)
    assert res == "win"          # الخروج حُسم عند هدف1
    assert lvl == 1              # لا يُحتسب هدف2 بعد كسر الوقف (قبل الإصلاح=2)


# ── الخروج الجزئي (قياس محاكى-المسار) ─────────────────────────────
def test_partial_exit_held_half_runs_to_t2():
    """نصف عند هدف1 (+20%) + النصف الثاني يبلغ هدف2 (+32%) دون تعادل."""
    # دخول 3.0 · أهداف [3.6, 3.96, 4.32] · t1=+20%
    post = [Bar(t_ms=1000, o=3.0, h=3.7, l=3.0, c=3.6, v=1000),   # بلغ هدف1
            Bar(t_ms=2000, o=3.6, h=4.0, l=3.7, c=3.95, v=1000)]  # بلغ هدف2، لا تعادل
    r = backtest.partial_exit_realized(3.0, _risk(2.7, 3.6), post, 0, 90, 0.5)
    assert round(r) == 26   # 0.5*20 + 0.5*32 = 26 (أفضل من خروج كامل +20)


def test_partial_exit_held_half_retraces_to_breakeven():
    """نصف عند هدف1 + النصف الثاني يرجع للتعادل قبل هدف2 → 0% للنصف (متحفّظ)."""
    post = [Bar(t_ms=1000, o=3.0, h=3.7, l=3.0, c=3.6, v=1000),   # بلغ هدف1
            Bar(t_ms=2000, o=3.6, h=3.7, l=2.9, c=3.0, v=1000)]   # رجع للتعادل
    r = backtest.partial_exit_realized(3.0, _risk(2.7, 3.6), post, 0, 90, 0.5)
    assert round(r) == 10   # 0.5*20 + 0.5*0 = 10 (أسوأ من خروج كامل +20 — مكشوف)


def test_partial_exit_stop_before_t1_is_full_loss():
    """الوقف قبل الهدف1 → خسارة كاملة (لا خروج جزئي)."""
    post = [Bar(t_ms=1000, o=3.0, h=3.1, l=2.6, c=2.7, v=1000)]   # ضرب الوقف 2.7
    r = backtest.partial_exit_realized(3.0, _risk(2.7, 3.6), post, 0, 90, 0.5)
    assert round(r) == -10  # (2.7-3)/3 = -10%


# ── م5: الوقف المتعقّب (قياس محاكى-المسار) ────────────────────────
def _trail(entry, stop, t1, post, trail_pct=5.0, window=90):
    return backtest.trailing_exit_realized(
        entry, _risk(stop, t1), post, 0, window, trail_pct)


def test_trailing_locks_gain_from_peak():
    """م5: هدف1 ثم قمّة عالية ثم نزول يلمس المتعقّب → المحقّق = المتعقّب من القمة."""
    # دخول 100 · هدف1 105 · وقف 93 · متعقّب 5%
    post = [Bar(t_ms=1000, o=100, h=106, l=100, c=105, v=1),   # بلغ هدف1 (قمة=105)
            Bar(t_ms=2000, o=105, h=120, l=104, c=118, v=1),   # قمة ترتفع 120، لا لمس
            Bar(t_ms=3000, o=118, h=118, l=110, c=112, v=1)]   # 110≤114=120×0.95 → خروج
    r = _trail(100, 93, 105, post)
    assert round(r, 1) == 14.0    # (120×0.95−100)/100 = +14%


def test_trailing_stop_before_t1_is_full_loss():
    """م5: كسر الوقف قبل الهدف1 → خسارة كاملة (لا تعقّب)."""
    post = [Bar(t_ms=1000, o=100, h=104, l=92, c=95, v=1)]     # 92≤93 قبل هدف1
    assert round(_trail(100, 93, 105, post), 1) == -7.0        # (93−100)/100


def test_trailing_no_target1_is_zero():
    """م5: لم يبلغ الهدف1 ولا الوقف خلال النافذة → 0 (⏳)."""
    post = [Bar(t_ms=1000, o=100, h=104, l=98, c=102, v=1)]    # لا هدف1 ولا وقف
    assert _trail(100, 93, 105, post) == 0.0


def test_trailing_held_to_window_end_returns_locked_trail():
    """م5: النافذة تنتهي والصفقة ممسوكة → المحقّق = الوقف المتعقّب المقفول لا آخر سعر."""
    post = [Bar(t_ms=1000, o=100, h=106, l=100, c=105, v=1),   # هدف1، قمة 105
            Bar(t_ms=2000, o=105, h=130, l=108, c=125, v=1)]   # قمة 130، لا لمس ثم انتهت
    r = _trail(100, 93, 105, post)
    assert round(r, 1) == 23.5    # (130×0.95−100)/100 = +23.5% (المقفول لا 125)


def test_trailing_intracandle_low_checked_before_raising_peak():
    """م5: داخل الشمعة القاع يُفحص ضد الوقف الحالي قبل رفع الوقف من قمتها (تحفّظ)."""
    post = [Bar(t_ms=1000, o=100, h=106, l=100, c=105, v=1),   # هدف1، قمة 105
            Bar(t_ms=2000, o=105, h=200, l=99, c=150, v=1)]    # قفزت 200 لكن قاعها 99
    # الوقف الحالي=max(100,105×0.95=99.75)=100؛ 99≤100 → خروج بالتعادل، لا نُنسب قفزة 200
    assert round(_trail(100, 93, 105, post), 1) == 0.0


# ── اعتماد 2: توسيع الهدف1 (ظل قياس) ──────────────────────────────
def _wide(entry, stop, t1, post, min_rr=0.5, window=90):
    return backtest.wide_target1_realized(
        entry, _risk(stop, t1), post, 0, window, min_rr)


def test_wide_t1_reaches_wider_target():
    """توسيع هدف1 قريب يصل الهدف الأوسع → ربح أكبر."""
    # دخول 3.0 · وقف 2.7 (stop_pct=10) · هدف1 قريب 3.06 (R/R=0.2<0.5)
    # الموسّع = max(3.06, 3.0×(1+0.5×0.10)=3.15) = 3.15
    post = [Bar(t_ms=1000, o=3.0, h=3.2, l=3.0, c=3.15, v=1)]   # بلغ 3.2 ≥ 3.15
    assert round(_wide(3.0, 2.7, 3.06, post), 1) == 5.0         # (3.15-3)/3


def test_wide_t1_widening_turns_win_into_loss():
    """توسيع الهدف يمسك أطول: هدف قريب كان سيُصاب ثم انعكس للوقف → خسارة كاملة."""
    post = [Bar(t_ms=1000, o=3.0, h=3.10, l=3.0, c=3.05, v=1),   # تجاوز 3.06 لا 3.15
            Bar(t_ms=2000, o=3.05, h=3.1, l=2.6, c=2.7, v=1)]    # ثم كسر الوقف
    assert round(_wide(3.0, 2.7, 3.06, post), 1) == -10.0        # (2.7-3)/3


def test_wide_t1_already_far_unchanged():
    """هدف أصلًا بعيد (R/R≥العتبة) → الموسّع = الأصلي (لا تغيير)."""
    # هدف1 3.3 → R/R=1.0≥0.5 · الموسّع=max(3.3,3.15)=3.3
    post = [Bar(t_ms=1000, o=3.0, h=3.35, l=3.0, c=3.3, v=1)]
    assert round(_wide(3.0, 2.7, 3.3, post), 1) == 10.0          # (3.3-3)/3


def test_wide_t1_no_reach_no_stop_is_zero():
    """لم يبلغ الهدف الموسّع ولا الوقف → 0 (⏳)."""
    post = [Bar(t_ms=1000, o=3.0, h=3.10, l=2.9, c=3.0, v=1)]    # بين الوقف والموسّع
    assert _wide(3.0, 2.7, 3.06, post) == 0.0


def test_wide_t1_stop_first_within_bar():
    """داخل شمعة تلمس الموسّع والوقف معًا → الوقف أولًا (تحفّظ)."""
    post = [Bar(t_ms=1000, o=3.0, h=3.5, l=2.6, c=3.0, v=1)]     # 3.5≥3.15 و2.6≤2.7
    assert round(_wide(3.0, 2.7, 3.06, post), 1) == -10.0


def test_report_shows_wide_t1_measurement():
    """اعتماد 2: قسم توسيع هدف1 لـ«دون 0.5» يظهر بالحساب الصحيح وآمن HTML (§5)."""
    res = backtest.BacktestResult(start="x", end="y", days=1)
    res.trades = [{"result": "win", "max_gain_pct": 5, "t1_rr": 0.3,
                   "realized_pct": 0.4, "realized_wide_t1_pct": 2.0}] * 4
    res.funnel = backtest.new_funnel()
    rep = backtest.format_report(res)
    assert "توسيع هدف1 لشريحة «دون 0.5»" in rep
    assert "حالي +0.4% ← هدف أوسع +2.0%" in rep
    stripped = rep
    for tag in ("<b>", "</b>", "<i>", "</i>"):
        stripped = stripped.replace(tag, "")
    assert "<" not in stripped and ">" not in stripped


# ── AsOfClient: لا تسرّب مستقبل ───────────────────────────────────
class _Base:
    def bars_daily(self, t, s, e):
        return [Bar(t_ms=_tms(2026, 6, 24, 16, 0), o=2, h=2.1, l=1.9, c=2.0, v=1e6),
                Bar(t_ms=_tms(2026, 6, 26, 16, 0), o=2, h=3, l=2, c=2.9, v=2e6)]  # اليوم


def test_asof_client_daily_excludes_today():
    c = backtest.AsOfClient(_Base(), "2026-06-26", _tms(2026, 6, 26, 10, 0),
                            [], [], {})
    daily = c.bars_daily("X", "2026-01-01", "2026-06-26")
    dates = [backtest._bar_date(b) for b in daily]
    assert "2026-06-26" not in dates       # شمعة اليوم مستبعدة (لا تسرّب)
    assert "2026-06-24" in dates


def test_asof_client_daily_appends_partial_today_from_5min():
    """م5: اليومي يُلحق شمعة اليوم **الجزئية** المعاد بناؤها من شموع 5د المقصوصة
    (كالحي الذي يمرّرها لـ compute_readiness) — بلا تسرّب (كل قيمها ≤ asof)."""
    asof = _tms(2026, 6, 26, 10, 0)
    five = [Bar(t_ms=_tms(2026, 6, 26, 9, 30), o=2.0, h=2.3, l=1.95, c=2.2,
                v=1e5, n=50),
            Bar(t_ms=_tms(2026, 6, 26, 9, 35), o=2.2, h=2.6, l=2.15, c=2.5,
                v=2e5, n=70)]
    c = backtest.AsOfClient(_Base(), "2026-06-26", asof, five, five, {})
    daily = c.bars_daily("X", "2026-01-01", "2026-06-26")
    today = [b for b in daily if backtest._bar_date(b) == "2026-06-26"]
    assert today, "شمعة اليوم الجزئية مُلحقة"
    t = today[0]
    assert t.o == 2.0 and t.c == 2.5           # فتح أول 5د · إغلاق آخر 5د ≤ asof
    assert t.h == 2.6 and t.l == 1.95          # قمة/قاع تراكمي حتى asof
    assert t.v == 3e5                          # مجموع الحجم (لا شيء بعد asof)


def test_asof_client_intraday_sliced():
    full = [Bar(t_ms=_tms(2026, 6, 26, 9, 35), o=3, h=3.1, l=3, c=3.05, v=1e4),
            Bar(t_ms=_tms(2026, 6, 26, 11, 0), o=3.1, h=3.2, l=3.1, c=3.15, v=1e4)]
    asof = _tms(2026, 6, 26, 10, 0)
    pre = [b for b in full if b.t_ms <= asof]
    c = backtest.AsOfClient(_Base(), "2026-06-26", asof, pre, pre, {})
    assert len(c.bars_5min("X", "", "")) == 1      # فقط ما قبل asof


def test_asof_client_hourly_excludes_unfinished_window():
    """t_ms هو **بداية** نافذة الشمعة: شمعة الساعة الجارية (بدأت قبل asof
    وتنتهي بعده) مجلوبة تاريخيًّا **مكتملة** — تمريرها كاملة يسرّب حتى ~ساعة
    من المستقبل لإطار الساعة في الجاهزية. المتوقّع: المكتملة قبل asof تبقى،
    والجارية تُعاد **جزئيًّا** من شموع 5د المقصوصة (كما يراها البوت الحي)."""
    class _HourBase:
        def aggregates(self, t, mult, span, s, e, **kw):
            return [
                Bar(t_ms=_tms(2026, 6, 26, 9, 0), o=2.0, h=2.2, l=1.9, c=2.1, v=1e5),
                # الشمعة الجارية: قمّتها/إغلاقها «مستقبليان» بعد asof (10:35)
                Bar(t_ms=_tms(2026, 6, 26, 10, 0), o=2.1, h=9.9, l=2.0, c=9.5, v=9e5),
            ]
    asof = _tms(2026, 6, 26, 10, 35)
    five = [Bar(t_ms=_tms(2026, 6, 26, 10, 0), o=2.1, h=2.3, l=2.05, c=2.2, v=1e4),
            Bar(t_ms=_tms(2026, 6, 26, 10, 35), o=2.2, h=2.4, l=2.15, c=2.35, v=1e4)]
    c = backtest.AsOfClient(_HourBase(), "2026-06-26", asof, five, five, {})
    hourly = c.aggregates("X", 1, "hour", "2026-04-26", "2026-06-26")
    assert hourly[0].t_ms == _tms(2026, 6, 26, 9, 0) and hourly[0].h == 2.2
    cur = [b for b in hourly if b.t_ms == _tms(2026, 6, 26, 10, 0)]
    # لا تمرّ الشمعة المكتملة (h=9.9/c=9.5) — بل الجزئية المعاد بناؤها من 5د
    assert cur and cur[0].h == 2.4 and cur[0].c == 2.35 and cur[0].v == 2e4


# ── تشغيل كامل end-to-end بمحاكاة ─────────────────────────────────
def _daily_on_runner_scale(n=260, end_close=2.3):
    """سلسلة يومية صاعدة مُعاد تحجيمها لتنتهي قرب سعر الرنر (~$2.3) — كي تتّصل
    شمعة اليوم الجزئية المعاد بناؤها من 5د (م5) بلا قفزة سعرية مصطنعة تخدع
    الجاهزية. (الفيكسترة الأصلية على مقياس ~$13، أعلى بكثير من شموع الرنر.)"""
    base = uptrend_daily_bars(n)
    scale = end_close / base[-1].c
    return [Bar(t_ms=b.t_ms, o=b.o * scale, h=b.h * scale, l=b.l * scale,
                c=b.c * scale, v=b.v, n=b.n) for b in base]


class MockBase:
    """عميل أساس وهمي: يوم واحد فيه رنر واحد (RUNR) يحقّق هدفه."""

    def grouped_daily(self, date):
        if date == "2026-06-26":
            return [{"T": "RUNR", "o": 2.1, "h": 3.0, "l": 2.0, "c": 2.9, "v": 5e6},
                    {"T": "FLAT", "o": 5.0, "h": 5.1, "l": 4.9, "c": 5.0, "v": 1e6}]
        return [{"T": "RUNR", "c": 2.0}, {"T": "FLAT", "c": 5.0}]   # اليوم السابق

    def bars_5min(self, t, s, e):
        if t != "RUNR":
            return []
        # 9:35 يعبر +20% (2.0→2.5) ثم يصعد نحو الهدف
        return [
            Bar(t_ms=_tms(2026, 6, 26, 9, 35), o=2.4, h=2.55, l=2.35, c=2.5, v=3e5, n=80),
            Bar(t_ms=_tms(2026, 6, 26, 9, 40), o=2.5, h=2.7, l=2.5, c=2.65, v=3e5, n=80),
            Bar(t_ms=_tms(2026, 6, 26, 9, 45), o=2.65, h=3.2, l=2.6, c=3.1, v=4e5, n=90),
        ]

    def bars_1min(self, t, s, e):
        return self.bars_5min(t, s, e)

    def bars_daily(self, t, s, e):
        return _daily_on_runner_scale(260)

    def aggregates(self, t, mult, span, s, e, **kw):
        return _daily_on_runner_scale(260)

    def ticker_overview(self, t):
        return {"type": "CS", "primary_exchange": "XNAS",
                "weighted_shares_outstanding": 5e6}

    def float_endpoint(self, t):
        return 5e6

    def latest_news(self, t, gte, limit=5, published_lte_utc=None):
        return None


def test_run_backtest_end_to_end():
    cfg = Config(massive_api_key="x", trigger_change_pct=10.0)
    res = backtest.run_backtest(cfg, MockBase(), "2026-06-26", "2026-06-26")
    assert res.days == 1
    # RUNR عبر الحدّ وحُلِّل؛ النتيجة محسومة (نجاح متوقّع لأنه بلغ ~3.2)
    assert len(res.trades) >= 1
    runr = [t for t in res.trades if t["ticker"] == "RUNR"]
    assert runr and runr[0]["result"] in ("win", "loss", "timeout")
    report = backtest.format_report(res)
    assert "باكتيست" in report


# ── قمع الترشيح (تشخيص: أين يموت المرشّحون؟) ──────────────────────
def test_funnel_counts_no_5min_skip():
    """مرشّح بلا شموع 5د تاريخية يُعدّ في «فُقدت شموع» لا يختفي صامتًا."""

    class NoBars(MockBase):
        def bars_5min(self, t, s, e):
            return []                    # لا شموع تاريخية لأي رمز

    cfg = Config(massive_api_key="x", trigger_change_pct=10.0)
    res = backtest.run_backtest(cfg, NoBars(), "2026-06-26", "2026-06-26")
    assert res.funnel["considered"] >= 1
    assert res.funnel["no_5min"] >= 1
    assert res.funnel["alerts"] == 0
    # التقرير يبيّن القمع (يشرح ليش العدد قليل)
    assert "قمع الترشيح" in backtest.format_report(res)


def test_funnel_records_alert_path():
    cfg = Config(massive_api_key="x", trigger_change_pct=10.0)
    res = backtest.run_backtest(cfg, MockBase(), "2026-06-26", "2026-06-26")
    assert res.funnel["considered"] >= 1
    assert res.funnel["alerts"] == len(res.trades)


# ── المسح المتكرّر مثل الحي: المرفوض مبكّرًا يُعاد فحصه ──────────────
class RVolBuildBase(MockBase):
    """سهم يفشل RVol عند أول عبور (حجم ضئيل) ثم ينجح بعد تراكم الحجم.

    يثبت أن الباكتيست يعيد الفحص كل دورة مثل البوت الحي (لا فحص لمرة واحدة).
    """

    def grouped_daily(self, date):
        if date == "2026-06-26":
            return [{"T": "BUILD", "o": 2.1, "h": 3.0, "l": 2.0, "c": 2.6, "v": 5e6}]
        return [{"T": "BUILD", "c": 2.0}]

    def bars_5min(self, t, s, e):
        from runner_scanner.models import Bar
        return [
            # 9:35 — +25% لكن الحجم ضئيل → RVol < 5 → رفض (هذه الدورة)
            Bar(t_ms=_tms(2026, 6, 26, 9, 35), o=2.4, h=2.55, l=2.35, c=2.5,
                v=50_000, n=80),
            # 11:00 — تراكم حجم ضخم → RVol ≥ 5 → ينجح (دورة لاحقة)
            Bar(t_ms=_tms(2026, 6, 26, 11, 0), o=2.5, h=2.7, l=2.5, c=2.6,
                v=4_000_000, n=300),
        ]

    def bars_1min(self, t, s, e):
        return self.bars_5min(t, s, e)


def test_reevaluates_until_pass_like_live():
    cfg = Config(massive_api_key="x", trigger_change_pct=10.0)
    res = backtest.run_backtest(cfg, RVolBuildBase(), "2026-06-26", "2026-06-26")
    # الفحص لمرة واحدة (القديم) كان يرفضه؛ المسح المتكرّر ينبّه عند الشمعة الثانية
    assert len(res.trades) == 1
    assert res.trades[0]["entry"] == 2.6          # دخول عند 11:00 لا 9:35
    assert res.funnel["alerts"] == 1
    assert res.funnel["rejected"] == 0            # نجا، لم يُرفض نهائيًا


def test_1min_slice_extends_to_trigger_bar_close():
    """م3: قصّ شموع 1د يمتد حتى إغلاق شمعة الزناد 5د لا بدايتها — كي يُحسب
    VWAP الجلسة على لحظة القرار (إغلاق الزناد = الدخول) لا أقدم بأربع دقائق."""
    captured = {}

    class MinBase(MockBase):
        def bars_1min(self, t, s, e):
            if t != "RUNR":
                return []
            # خمس شموع 1د داخل نافذة الزناد (9:35–9:39) + شمعة لاحقة (9:40)
            return [Bar(t_ms=_tms(2026, 6, 26, 9, 35 + i), o=2.4, h=2.5,
                        l=2.35, c=2.45, v=6e4, n=20) for i in range(5)] + \
                   [Bar(t_ms=_tms(2026, 6, 26, 9, 40), o=2.5, h=2.7, l=2.5,
                        c=2.65, v=3e5, n=80)]

    orig = backtest.process_candidate

    def spy(cfg, client, *a, **k):
        captured.setdefault("t1", [b.t_ms for b in client.bars_1min("RUNR", "", "")])
        return orig(cfg, client, *a, **k)

    backtest.process_candidate = spy
    try:
        cfg = Config(massive_api_key="x", trigger_change_pct=10.0)
        backtest.run_backtest(cfg, MinBase(), "2026-06-26", "2026-06-26")
    finally:
        backtest.process_candidate = orig

    assert captured.get("t1"), "process_candidate استُدعي عند الزناد"
    # الزناد عند 9:35؛ القصّ يمتد حتى 9:39 (نهاية النافذة) لا يقف عند 9:35
    assert min(captured["t1"]) == _tms(2026, 6, 26, 9, 35)
    assert max(captured["t1"]) == _tms(2026, 6, 26, 9, 39)


def test_backtest_survives_fetch_failure():
    """فشل شبكي لسهم لا يكسر الباكتيست (best-effort) — يتخطّاه ويكمل."""

    class FlakyBase(MockBase):
        def bars_5min(self, t, s, e):
            raise RuntimeError("Read timed out")

    cfg = Config(massive_api_key="x", trigger_change_pct=10.0)
    res = backtest.run_backtest(cfg, FlakyBase(), "2026-06-26", "2026-06-26")
    assert res.days == 1                       # لم ينهَر
    assert res.funnel["error"] >= 1            # عُدّ الفشل، لم يُسقط الباكتيست


def test_day_candidates_pool_wider_than_live_top_n():
    """مجمّع الباكتيست = backtest_top_n (أوسع من top_n_runners الحي = 15)."""
    cfg = Config(massive_api_key="x", trigger_change_pct=10.0,
                 top_n_runners=15, backtest_top_n=45)
    prev = {f"S{i}": 2.0 for i in range(30)}
    grouped = [{"T": f"S{i}", "h": 2.0 * (1 + (0.20 + i * 0.01)),
                "c": 2.5} for i in range(30)]   # 30 سهمًا فوق +20%
    out = backtest._day_candidates(cfg, grouped, prev)
    assert len(out) == 30          # كلها (≤45)، لا تُقصّ على 15 الحي
    # لو كان السقف 15 الحي لظهر 15 فقط
    cfg2 = Config(massive_api_key="x", trigger_change_pct=10.0, backtest_top_n=10)
    assert len(backtest._day_candidates(cfg2, grouped, prev)) == 10


def test_day_candidates_no_lookahead_on_close_or_dayhigh():
    """م4: مجمّع اليوم لا يستبعد بسعر الإغلاق (مستقبلي) ولا بسقف قمة اليوم
    (مستقبلي) — البوّابتان (السعر/سقف التشوّه) لحظيّتان داخل التقييم كالحي.
    استبعادهما هنا على بيانات اليوم يحذف أكبر الرابحين بأثر رجعي."""
    cfg = Config(massive_api_key="x", trigger_change_pct=10.0,
                 max_change_pct=400.0, price_min=1.0, price_max=30.0,
                 backtest_top_n=50)
    prev_close = {"LATE": 2.0, "SPIKE": 2.0, "QUIET": 2.0}
    grouped = [
        # عبر الحدّ لحظةً ($2→$2.5) لكنه أغلق فوق سقف السعر ($45) — يجب أن يبقى
        {"T": "LATE", "h": 46.0, "c": 45.0},
        # قمة اليوم +500% (فوق سقف التشوّه) لكنه عبر الحدّ مبكّرًا — يجب أن يبقى
        {"T": "SPIKE", "h": 12.0, "c": 11.0},
        # قمته اليومية لم تبلغ الحدّ إطلاقًا (+5%) — يُستبعد (شرط لازم غير-تسرّب)
        {"T": "QUIET", "h": 2.1, "c": 2.05},
    ]
    tickers = [t for t, _ in backtest._day_candidates(cfg, grouped, prev_close)]
    assert "LATE" in tickers        # لا استبعاد بإغلاق مستقبلي
    assert "SPIKE" in tickers       # لا استبعاد بقمة يوم مستقبلية
    assert "QUIET" not in tickers   # لم يبلغ الحدّ يومًا → لا يمكن أن يكون رنرًا


def test_parallel_matches_serial():
    """الجلب المتوازي يعطي نفس نتيجة التسلسلي (آمن، بلا تسابق)."""
    snaps = {f"S{i}": 2.0 for i in range(8)}

    class ManyBase(MockBase):
        def grouped_daily(self, date):
            if date == "2026-06-26":
                return [{"T": k, "o": 2.1, "h": 3.0, "l": 2.0, "c": 2.9, "v": 5e6}
                        for k in snaps]
            return [{"T": k, "c": 2.0} for k in snaps]

    serial = backtest.run_backtest(
        Config(massive_api_key="x", trigger_change_pct=10.0, backtest_workers=1),
        ManyBase(), "2026-06-26", "2026-06-26")
    parallel = backtest.run_backtest(
        Config(massive_api_key="x", trigger_change_pct=10.0, backtest_workers=8),
        ManyBase(), "2026-06-26", "2026-06-26")
    assert serial.funnel["considered"] == parallel.funnel["considered"]
    assert serial.funnel["alerts"] == parallel.funnel["alerts"]
    assert len(serial.trades) == len(parallel.trades)
    assert {t["ticker"] for t in serial.trades} == \
           {t["ticker"] for t in parallel.trades}


def test_trade_records_indicator_flags():
    """كل صفقة تسجّل المؤشرات (لكشف أيها يتنبّأ بالنجاح)."""
    cfg = Config(massive_api_key="x", trigger_change_pct=10.0)
    res = backtest.run_backtest(cfg, MockBase(), "2026-06-26", "2026-06-26")
    assert res.trades, "متوقّع صفقة واحدة على الأقل"
    t = res.trades[0]
    for key in ("macd_bull", "golden_cross", "above_ma200", "above_vwap",
                "volume_rising", "divergence", "trend", "adx"):
        assert key in t, f"المؤشّر {key} غير مسجّل"


def test_profit_section_in_report():
    """قسم الربحية يعرض التوقّع/تحقيق الأهداف/كسر الوقف."""
    res = backtest.BacktestResult(start="x", end="y", days=1)
    base = {"session": "رسمي"}
    res.trades = [
        {**base, "result": "win", "realized_pct": 12, "target1_pct": 12,
         "target_hit": 3, "max_gain_pct": 30},
        {**base, "result": "win", "realized_pct": 8, "target1_pct": 8,
         "target_hit": 1, "max_gain_pct": 9},
        {**base, "result": "loss", "realized_pct": -6, "target1_pct": 10,
         "target_hit": 0, "max_gain_pct": 2},
    ]
    rep = backtest.format_report(res)
    assert "الربحية والأهداف" in rep
    assert "تحقيق الأهداف" in rep and "كسر الوقف" in rep
    assert "هدفها الأول (الأقرب) أقل من 10%" in rep


def test_min_target_profit_gate_rejects_small_reward():
    """بوّابة الحد الأدنى للربح ترفض صفقة هدفها الأول < العتبة (مع تعطيل بقية
    البوّابات لعزل البوّابة محل الاختبار)."""
    from runner_scanner import pipeline
    from runner_scanner.tests.fixtures import FakeClient, make_snapshot
    from runner_scanner.models import Session
    snap = make_snapshot("SMALL", last=10.0, prev=8.0, vol=2_000_000,
                         change_pct=25.0)
    client = FakeClient(float_shares=5_000_000)
    common = dict(massive_api_key="x", tech_readiness_min=0.0,
                  alert_score_min=0.0, rvol_min=0.0, momentum_min_floor=0.0,
                  parabolic_vwap_ext_pct=10_000.0,
                  parabolic_day_change_pct=10_000.0)
    # عتبة ربح ضخمة (999%) → أي هدف واقعي يُرفض بهذه البوّابة تحديدًا
    cand = pipeline.process_candidate(
        Config(min_target_profit_pct=999.0, **common), client, snap,
        session=Session.REGULAR)
    assert cand.is_rejected and "يستحق المخاطرة" in (cand.rejected_reason or "")
    # عتبة 0 (معطّلة) → لا ترفض بسبب الربح
    cand2 = pipeline.process_candidate(
        Config(min_target_profit_pct=0.0, **common), client, snap,
        session=Session.REGULAR)
    assert "يستحق المخاطرة" not in (cand2.rejected_reason or "")


def test_reward_gate_measures_top_target_not_first():
    """البوّابة تقيس **سقف** الأهداف (أبعد) لا الأقرب: هدف أول قريب لا يرفض
    الصفقة ما دام السقف مرتفعًا (الرنر يقمّ أبعد من مقاومته الأولى)."""
    from runner_scanner.pipeline import _targets_top_gain
    from runner_scanner.models import RiskPlan
    rp = RiskPlan(stop_price=9.0, stop_pct=10.0, entry_ref=10.0,
                  targets=[10.4, 11.0, 11.8], stop_basis="دعم 5د")
    # يقيس أبعد هدف (+18%) لا الأول (+4%)
    assert round(_targets_top_gain(rp, 10.0)) == 18
    # عتبة 10%: يمرّ (السقف 18 ≥ 10) رغم أن الهدف الأول +4% فقط
    assert _targets_top_gain(rp, 10.0) >= 10.0
    # سعر غير صالح / بلا أهداف → None (لا رفض)
    assert _targets_top_gain(rp, 0.0) is None
    assert _targets_top_gain(RiskPlan(stop_price=0, stop_pct=0, entry_ref=0,
                                      targets=[], stop_basis="x"), 10.0) is None


def test_pyxs_measurement_buckets_in_report():
    """قياس فرضيتَي PYXS: شرائح 5min RVol و R/R الهدف1 تظهر في التقرير."""
    res = backtest.BacktestResult(start="x", end="y", days=1)
    base = {"session": "رسمي", "max_gain_pct": 5}
    # منطفئ (5min RVol <2) يخسر · نشط (≥2) يفوز — عيّنة ≥3 لكل شريحة
    res.trades = (
        [{**base, "rvol_5min": 1.1, "t1_rr": 0.4, "result": "loss"}] * 3 +
        [{**base, "rvol_5min": 8.0, "t1_rr": 1.2, "result": "win"}] * 3)
    rep = backtest.format_report(res)
    assert "حسب 5min RVol" in rep and "منطفئ تحت 2x" in rep and "نشط ≥2x" in rep
    assert "حسب R/R الهدف1" in rep and "دون 0.5" in rep
    # HTML-آمن: لا < أو > شارد خارج الوسوم المقصودة (وإلا يُسقط تيليجرام التقرير)
    stripped = rep
    for tag in ("<b>", "</b>", "<i>", "</i>"):
        stripped = stripped.replace(tag, "")
    assert "<" not in stripped and ">" not in stripped


def test_report_discloses_simulation_limits_html_safe():
    """الإفصاح يذكر الطبقات الغائبة (محلّل/شورت/SEC/أبطال/توقّفات) وحبيبية 5د،
    وخالٍ من محارف < أو > الحرفية (وإلا يُسقط تيليجرام التقرير كاملًا — §5)."""
    res = backtest.BacktestResult(start="x", end="y", days=1)
    res.trades = [{"result": "win", "max_gain_pct": 10}] * 3
    rep = backtest.format_report(res)
    assert "تقدير تاريخي" in rep
    for token in ("محلّل", "SEC", "الأبطال", "LULD", "5د"):
        assert token in rep, token
    stripped = rep
    for tag in ("<b>", "</b>", "<i>", "</i>"):
        stripped = stripped.replace(tag, "")
    assert "<" not in stripped and ">" not in stripped


def test_trade_records_pyxs_measurement_fields():
    """كل صفقة تسجّل rvol_5min و t1_rr (قياس فرضيتَي PYXS)."""
    cfg = Config(massive_api_key="x", trigger_change_pct=10.0)
    res = backtest.run_backtest(cfg, MockBase(), "2026-06-26", "2026-06-26")
    assert res.trades, "متوقّع صفقة"
    t = res.trades[0]
    assert "rvol_5min" in t and "t1_rr" in t


def test_indicator_yes_no_section_in_report():
    """تقرير المؤشرات الثنائية يظهر عند توفّر عيّنة كافية في كلا الجانبين."""
    res = backtest.BacktestResult(start="x", end="y", days=1)
    # 6 «نعم» (5 فوز) + 6 «لا» (2 فوز) لمؤشّر MACD
    base = {"session": "رسمي", "readiness": 70, "score": 70}
    res.trades = (
        [{**base, "macd_bull": True, "result": "win", "max_gain_pct": 9}] * 5 +
        [{**base, "macd_bull": True, "result": "loss", "max_gain_pct": 1}] * 1 +
        [{**base, "macd_bull": False, "result": "win", "max_gain_pct": 9}] * 2 +
        [{**base, "macd_bull": False, "result": "loss", "max_gain_pct": 1}] * 4)
    rep = backtest.format_report(res)
    assert "المؤشرات الثنائية" in rep and "MACD صاعد" in rep


def test_stats_conservative_winrate_counts_timeouts():
    """النسبة المتحفّظة تعدّ ⏳ غير-فوز (أدنى من المحسومة)."""
    res = backtest.BacktestResult(start="x", end="y", days=1)
    res.trades = [{"result": "win", "max_gain_pct": 10}] * 8 + \
                 [{"result": "loss", "max_gain_pct": 1}] * 2 + \
                 [{"result": "timeout", "max_gain_pct": 3}] * 5
    s = res.stats()
    assert round(s["win_rate"]) == 80          # 8/(8+2) محسومة
    assert round(s["win_rate_conservative"]) == 53   # 8/15 شامل ⏳
    assert "المتحفّظ" in backtest.format_report(res)


def test_news_label_splits_positive_negative_none():
    from runner_scanner.models import Candidate, Catalyst, Session, SnapshotEntry
    from runner_scanner.catalyst import NEGATIVE_NEWS

    def _c(cat):
        x = Candidate(snapshot=SnapshotEntry("T", 3, 2, 2, 3, 2, 1, 2.5, 25),
                      session=Session.REGULAR)
        x.catalyst = cat
        return x
    assert backtest._news_label(_c(None)) == "بلا"
    assert backtest._news_label(_c(Catalyst(has_news=False))) == "بلا"
    pos = Catalyst(has_news=True); pos.category = "💊 موافقة/تجارب سريرية"
    assert backtest._news_label(_c(pos)) == "إيجابي"
    neg = Catalyst(has_news=True); neg.category = NEGATIVE_NEWS
    assert backtest._news_label(_c(neg)) == "سلبي"


def test_shadow_eval_records_rvol_rejects():
    """مرفوض RVol يُسجَّل في قياس الظل (نتيجة افتراضية + أقصى RVol)."""

    class ThinBase(MockBase):
        # حجم ضئيل دائمًا → RVol < 5 طوال اليوم → رفض RVol
        def grouped_daily(self, date):
            if date == "2026-06-26":
                return [{"T": "THIN", "o": 2.1, "h": 3.0, "l": 2.0, "c": 2.9, "v": 5e6}]
            return [{"T": "THIN", "c": 2.0}]

        def bars_5min(self, t, s, e):
            from runner_scanner.models import Bar
            return [Bar(t_ms=_tms(2026, 6, 26, 9, 35), o=2.4, h=2.6, l=2.3, c=2.5,
                        v=200, n=5),
                    Bar(t_ms=_tms(2026, 6, 26, 10, 0), o=2.5, h=2.8, l=2.5, c=2.7,
                        v=300, n=6)]

        def bars_1min(self, t, s, e):
            return self.bars_5min(t, s, e)

    cfg = Config(massive_api_key="x", trigger_change_pct=10.0,
                 backtest_shadow_rvol=True)
    res = backtest.run_backtest(cfg, ThinBase(), "2026-06-26", "2026-06-26")
    assert res.funnel["reject_reasons"].get("RVol", 0) >= 1
    assert len(res.funnel["shadow"]) >= 1
    assert res.funnel["shadow"][0]["result"] in ("win", "loss", "timeout")
    assert "قياس الظل" in backtest.format_report(res)


def test_shadow_risk_plan_uses_daily_resistances_like_live():
    """م7: قياس الظل يبني خطة المخاطر بنفس المقاومات اليومية كالخط الفعلي —
    وإلا أهدافه تختلف فيَختلّ حكم «العتبة مثبتة/تستحق الدراسة» المبنيّ عليها."""

    class ThinBase(MockBase):
        def grouped_daily(self, date):
            if date == "2026-06-26":
                return [{"T": "THIN", "o": 2.1, "h": 3.0, "l": 2.0, "c": 2.9,
                         "v": 5e6}]
            return [{"T": "THIN", "c": 2.0}]

        def bars_5min(self, t, s, e):
            return [Bar(t_ms=_tms(2026, 6, 26, 9, 35), o=2.4, h=2.6, l=2.3,
                        c=2.5, v=200, n=5),
                    Bar(t_ms=_tms(2026, 6, 26, 10, 0), o=2.5, h=2.8, l=2.5,
                        c=2.7, v=300, n=6)]

        def bars_1min(self, t, s, e):
            return self.bars_5min(t, s, e)

    captured = {}
    orig = backtest.build_risk_plan

    def spy(cfg, entry, closed5, daily_resistances=None):
        captured["dr"] = daily_resistances       # نداء الظل الوحيد لهذا المرجع
        return orig(cfg, entry, closed5, daily_resistances=daily_resistances)

    backtest.build_risk_plan = spy
    try:
        cfg = Config(massive_api_key="x", trigger_change_pct=10.0,
                     backtest_shadow_rvol=True)
        backtest.run_backtest(cfg, ThinBase(), "2026-06-26", "2026-06-26")
    finally:
        backtest.build_risk_plan = orig

    # قبل الإصلاح: الظل ينادي build_risk_plan بلا daily_resistances (None)
    assert "dr" in captured, "قياس الظل نادى build_risk_plan"
    assert captured["dr"] is not None and len(captured["dr"]) >= 1


def test_backtest_premarket_parity_with_live_guard():
    """مطابقة الحي: البريماركت معطّل → الباكتيست لا يقيّم/ينبّه في شموع البريماركت
    (كان يحاكي تنبيهات بريماركت لا ينتجها البوت المنشور — يقيس بوتًا آخر)."""

    class PreBase(MockBase):
        # الشموع كلها في البريماركت (8:00–8:10 ET) ويعبر الحدّ فيها
        def bars_5min(self, t, s, e):
            if t != "RUNR":
                return []
            return [Bar(t_ms=_tms(2026, 6, 26, 8, 0), o=2.4, h=2.55, l=2.35,
                        c=2.5, v=3e5, n=80),
                    Bar(t_ms=_tms(2026, 6, 26, 8, 5), o=2.5, h=2.7, l=2.5,
                        c=2.65, v=3e5, n=80),
                    Bar(t_ms=_tms(2026, 6, 26, 8, 10), o=2.65, h=3.2, l=2.6,
                        c=3.1, v=4e5, n=90)]

        def bars_1min(self, t, s, e):
            return self.bars_5min(t, s, e)

    # بوّابات متساهلة لعزل الحارس (لولاه لنجح المرشّح في البريماركت)
    common = dict(massive_api_key="x", trigger_change_pct=10.0,
                  tech_readiness_min=0.0, alert_score_min=0.0, rvol_min=0.0,
                  momentum_min_floor=0.0, parabolic_vwap_ext_pct=10_000.0,
                  parabolic_day_change_pct=10_000.0, min_target_profit_pct=0.0)
    # معطّل (الافتراضي، مثل الحي) → صفر صفقات بريماركت
    off = backtest.run_backtest(Config(**common), PreBase(),
                                "2026-06-26", "2026-06-26")
    assert all(t["session"] != "بريماركت" for t in off.trades)
    assert not off.trades          # كل الشموع بريماركت → لا تنبيه إطلاقًا
    # م1: رنر كل شموعه بريماركت يُصنَّف «premarket_only» لا «bad_snapshot»
    # (سنابشوته صالح؛ خرج فقط لأنه خارج ساعات التنبيه — لا يلوّث القمع)
    assert off.funnel["premarket_only"] == 1
    assert off.funnel["bad_snapshot"] == 0
    # مفعّل صراحةً → التنبيه يرجع (يثبت أن الحارس هو الفارق لا البوّابات)
    on = backtest.run_backtest(Config(premarket_alerts_enabled=True, **common),
                               PreBase(), "2026-06-26", "2026-06-26")
    assert on.trades and on.trades[0]["session"] == "بريماركت"


def test_shadow_verdict_is_data_driven():
    """حكم الظل يُحسب من الأرقام: شريحة 3–5x خاسرة → «مثبتة؛ لا تُخفَّض»،
    وعيّنة صغيرة → «غير كافية» (لا اقتراح خفض أزلي)."""
    res = backtest.BacktestResult(start="x", end="y", days=1)
    res.trades = [{"result": "win", "max_gain_pct": 10}] * 8 + \
                 [{"result": "loss", "max_gain_pct": 1}] * 2      # أساس 80%
    res.funnel = backtest.new_funnel()
    res.funnel["shadow"] = [{"max_rvol": 4.0, "result": "loss"}] * 10
    rep = backtest.format_report(res)
    assert "مثبتة؛ لا تُخفَّض" in rep
    # عيّنة ظل صغيرة (3 محسومة فقط) → لا حكم
    res.funnel["shadow"] = [{"max_rvol": 4.0, "result": "loss"}] * 3
    assert "غير كافية للحكم" in backtest.format_report(res)


def test_reject_bucket_classifies():
    assert backtest._reject_bucket("RVol 3.0x < 5x") == "RVol"
    assert backtest._reject_bucket("فلوت 90,000,000 > 40,000,000") == "فلوت"
    assert backtest._reject_bucket("شيء غريب") == "أخرى"
    # م4: السعر والجاهزية/الدرجة مفصولة (كانت مدموجة) — بنصوص البوّابات الفعلية
    assert backtest._reject_bucket("سعر 0.80 < 1.0 (سنتات)") == "سعر تحت الحد"
    assert backtest._reject_bucket(
        "سعر 45.00 > 30.0 (فوق نطاق الأسهم)") == "سعر فوق الحد"
    assert backtest._reject_bucket(
        "جاهزية فنية 45 < 60 (غير جاهز فنيًا)") == "جاهزية"
    assert backtest._reject_bucket("درجة 55 < عتبة التنبيه 60") == "درجة"
    assert backtest._reject_bucket(
        "تحت VWAP (شريحة أضعف تاريخيًا 55%)") == "تحت VWAP"


# ── م1: الحفظ الدائم للتشغيلات (مصدر الدمج) ───────────────────────
def test_save_run_persists_json_and_report(tmp_path):
    """م1: التشغيل الكامل يُحفَظ JSON (بالأنواع الدقيقة) + نص التقرير على القرص."""
    import json
    import os
    cfg = Config(massive_api_key="x", trigger_change_pct=10.0,
                 backtest_save_dir=str(tmp_path))
    res = backtest.run_backtest(cfg, MockBase(), "2026-06-26", "2026-06-26")
    report = backtest.format_report(res)
    path = backtest.save_run(cfg, res, report)
    assert path and os.path.exists(path)
    data = json.load(open(path, encoding="utf-8"))
    assert data["start"] == "2026-06-26" and data["end"] == "2026-06-26"
    assert data["days"] == res.days and data["trades"] == res.trades
    assert data["funnel"] == res.funnel and "created_utc" in data
    # نسخة نص التقرير محفوظة بجانب JSON (نفس الجذع، لاحقة .txt)
    txt = path[:-5] + ".txt"
    assert os.path.exists(txt) and open(txt, encoding="utf-8").read() == report


def test_save_run_failure_is_best_effort(tmp_path):
    """م1: فشل الحفظ (مسار غير قابل للكتابة) يرجّع None بلا استثناء (§3)."""
    afile = tmp_path / "afile"
    afile.write_text("x")            # ملف مكان المجلد → makedirs يفشل
    cfg = Config(massive_api_key="x", backtest_save_dir=str(afile / "sub"))
    res = backtest.BacktestResult(start="2026-01-01", end="2026-01-31", days=1)
    res.funnel = backtest.new_funnel()
    assert backtest.save_run(cfg, res, "تقرير") is None


def test_save_dir_defaults_next_to_db(tmp_path):
    """م1: مجلد الحفظ الافتراضي = <مجلد قاعدة البيانات>/backtests (القرص الدائم)."""
    import os
    cfg = Config(massive_api_key="x",
                 db_path=str(tmp_path / "sub" / "runner.sqlite3"))
    assert backtest._save_dir(cfg) == os.path.join(
        str(tmp_path / "sub"), "backtests")


# ── م2: دمج التشغيلات المحفوظة ────────────────────────────────────
def _saved_run(cfg, start, end, trades, reject_reasons=None):
    """يحفظ تشغيلًا وهميًّا بأقل الحقول (لاختبار الدمج، لا التنسيق)."""
    res = backtest.BacktestResult(start=start, end=end, days=20)
    res.trades = trades
    res.funnel = backtest.new_funnel()
    res.funnel["considered"] = 100
    res.funnel["alerts"] = len(trades)
    if reject_reasons:
        res.funnel["reject_reasons"] = reject_reasons
    backtest.save_run(cfg, res, "تقرير")


def test_merge_saved_runs_aggregates(tmp_path):
    """م2: دمج تشغيلين يجمع الصفقات والأيام والقمع (reject_reasons/considered)."""
    cfg = Config(massive_api_key="x", backtest_save_dir=str(tmp_path))
    def t(r):
        return {"result": r, "max_gain_pct": 5, "session": "رسمي"}
    _saved_run(cfg, "2026-01-01", "2026-01-31",
               [t("win")] * 3 + [t("loss")], {"RVol": 10})
    _saved_run(cfg, "2026-02-01", "2026-02-28",
               [t("win")] * 2, {"RVol": 5, "سعر": 3})
    merged, notes = backtest.merge_saved_runs(cfg)
    assert merged is not None
    assert len(merged.trades) == 6 and merged.days == 40
    assert merged.start == "2026-01-01" and merged.end == "2026-02-28"
    assert merged.funnel["considered"] == 200
    assert merged.funnel["reject_reasons"]["RVol"] == 15
    assert merged.funnel["reject_reasons"]["سعر"] == 3
    assert any("دمج 2" in n for n in notes)


def test_merge_skips_corrupt_file(tmp_path):
    """م2: ملف JSON تالف يُتخطّى مع تحذير — لا يكسر الدمج (best-effort)."""
    cfg = Config(massive_api_key="x", backtest_save_dir=str(tmp_path))
    _saved_run(cfg, "2026-01-01", "2026-01-31",
               [{"result": "win", "max_gain_pct": 5}])
    (tmp_path / "bt_bad_bad.json").write_text("{ليس JSON", encoding="utf-8")
    merged, notes = backtest.merge_saved_runs(cfg)
    assert merged is not None and len(merged.trades) == 1
    assert any("تالف" in n for n in notes)


def test_merge_skips_overlapping_range(tmp_path):
    """م2: نطاق متداخل زمنيًا يُتخطّى (تجنّب عدّ الصفقات مرّتين)."""
    cfg = Config(massive_api_key="x", backtest_save_dir=str(tmp_path))
    _saved_run(cfg, "2026-01-01", "2026-01-31",
               [{"result": "win", "max_gain_pct": 5}])
    _saved_run(cfg, "2026-01-15", "2026-02-15",
               [{"result": "loss", "max_gain_pct": 1}] * 9)
    merged, notes = backtest.merge_saved_runs(cfg)
    assert len(merged.trades) == 1               # المتداخل مُستبعَد
    assert any("المتداخل" in n for n in notes)


def test_merge_skips_structurally_malformed_file(tmp_path):
    """م2/§3: ملف صالح-JSON لكن مشوّه البنية (funnel قائمة · days نصّي · reject_reasons
    قائمة) يُتخطّى مع تحذير — لا يُسقط الدمج كلّه (انجراف مخطّط §7 على القرص الدائم)."""
    import json
    cfg = Config(massive_api_key="x", backtest_save_dir=str(tmp_path))
    _saved_run(cfg, "2026-01-01", "2026-01-31",
               [{"result": "win", "max_gain_pct": 5}])            # ملف سليم
    # funnel قائمة بدل dict (نسخة save_run قديمة مثلًا)
    (tmp_path / "bt_2026-02-01_2026-02-28.json").write_text(json.dumps(
        {"start": "2026-02-01", "end": "2026-02-28", "days": 20,
         "trades": [{"result": "win"}], "funnel": [1, 2]}), encoding="utf-8")
    # days نصّي غير رقمي
    (tmp_path / "bt_2026-03-01_2026-03-31.json").write_text(json.dumps(
        {"start": "2026-03-01", "end": "2026-03-31", "days": "abc",
         "trades": [], "funnel": {}}), encoding="utf-8")
    # funnel dict لكن reject_reasons قائمة (تلف داخلي في حقل تشخيصي): البنية
    # العليا سليمة فيُدمَج، و_merge_funnel الدفاعي يُسقط القيمة الفرعية المشوّهة.
    (tmp_path / "bt_2026-04-01_2026-04-30.json").write_text(json.dumps(
        {"start": "2026-04-01", "end": "2026-04-30", "days": 21,
         "trades": [{"result": "loss"}], "funnel": {"reject_reasons": [1]}}),
        encoding="utf-8")
    merged, notes = backtest.merge_saved_runs(cfg)     # يجب ألا يرفع استثناءً
    # المشوّهان بنيويًّا (funnel قائمة · days نصّي) يُتخطّيان؛ السليم + المتساهَل يُدمجان
    assert merged is not None and len(merged.trades) == 2
    assert sum("تالف" in n for n in notes) >= 2


def test_merge_notes_escape_html(tmp_path):
    """§5: اسم الملف/الاستثناء في ملاحظات الدمج مهرَّب HTML (وإلا < أو > يُسقط الرسالة)."""
    cfg = Config(massive_api_key="x", backtest_save_dir=str(tmp_path))
    (tmp_path / "bt_<x>_bad.json").write_text("{ليس JSON", encoding="utf-8")
    _, notes = backtest.merge_saved_runs(cfg)
    joined = "\n".join(notes)
    assert "&lt;x&gt;" in joined and "bt_<x>" not in joined


def test_merge_no_saved_runs_message(tmp_path):
    """م2: بلا تشغيلات محفوظة → None + رسالة عربية واضحة (بلا استثناء)."""
    cfg = Config(massive_api_key="x", backtest_save_dir=str(tmp_path))
    merged, notes = backtest.merge_saved_runs(cfg)
    assert merged is None
    assert "لا توجد تشغيلات" in backtest.format_merged_report(cfg)


def test_format_merged_report_end_to_end(tmp_path):
    """م2: تقرير الدمج يظهر رأس «دمج N» + جسم التقرير، وآمن HTML (§5)."""
    cfg = Config(massive_api_key="x", trigger_change_pct=10.0,
                 backtest_save_dir=str(tmp_path))
    res = backtest.run_backtest(cfg, MockBase(), "2026-06-26", "2026-06-26")
    backtest.save_run(cfg, res, backtest.format_report(res))
    rep = backtest.format_merged_report(cfg)
    assert "🧩 دمج 1" in rep and "باكتيست" in rep
    stripped = rep
    for tag in ("<b>", "</b>", "<i>", "</i>"):
        stripped = stripped.replace(tag, "")
    assert "<" not in stripped and ">" not in stripped


def test_report_realized_expectancy_by_rr_and_vwap():
    """م3: التقرير يعرض التوقّع المحقّق (realized_pct) لكل شريحة R/R وموقع VWAP —
    نسبة النجاح وحدها تخدع (هدف أقرب يُلمس أسهل). §5: بلا < أو > حرفية."""
    res = backtest.BacktestResult(start="x", end="y", days=1)

    def tr(rr, vwap, realized, result):
        return {"t1_rr": rr, "above_vwap": vwap, "realized_pct": realized,
                "result": result, "max_gain_pct": max(realized, 0.0)}
    res.trades = ([tr(0.3, True, 2.8, "win")] * 3 +       # دون 0.5 · فوق VWAP
                  [tr(1.5, False, -7.0, "loss")] * 3)     # ≥1 · تحت VWAP
    res.funnel = backtest.new_funnel()
    rep = backtest.format_report(res)
    assert "توقّع محقّق حسب R/R الهدف1" in rep
    assert "توقّع محقّق حسب موقع VWAP" in rep
    assert "دون 0.5: توقّع +2.8%/صفقة (3)" in rep
    assert "≥1: توقّع -7.0%/صفقة (3)" in rep
    assert "فوق VWAP: توقّع +2.8%/صفقة (3)" in rep
    assert "تحت VWAP: توقّع -7.0%/صفقة (3)" in rep
    stripped = rep
    for tag in ("<b>", "</b>", "<i>", "</i>"):
        stripped = stripped.replace(tag, "")
    assert "<" not in stripped and ">" not in stripped


def test_report_shows_trailing_exit_measurement():
    """م5: قسم بدائل الخروج يعرض «متعقّب» بجانب «جزئي»، وآمن HTML (§5)."""
    res = backtest.BacktestResult(start="x", end="y", days=1)
    res.trades = [{"result": "win", "max_gain_pct": 10, "realized_pct": 3.0,
                   "realized_partial_pct": 3.5, "realized_trail_pct": 8.0}] * 4
    res.funnel = backtest.new_funnel()
    rep = backtest.format_report(res)
    assert "قياس بدائل الخروج" in rep
    assert "متعقّب" in rep and "جزئي" in rep
    assert "← متعقّب +8.0%" in rep
    stripped = rep
    for tag in ("<b>", "</b>", "<i>", "</i>"):
        stripped = stripped.replace(tag, "")
    assert "<" not in stripped and ">" not in stripped


def test_backtest_merge_command_needs_no_api_key(tmp_path):
    """م2: «/backtest دمج» يقرأ المحفوظ بلا شبكة ولا مفتاح Massive (لا رسالة مفتاح)."""
    from runner_scanner.telegram_bot import TelegramAssistant
    sent = []

    class _FakeTelegram:
        def send(self, text):
            sent.append(text)
            return True

    class _FakeScanner:
        pass

    sc = _FakeScanner()
    sc.cfg = Config(massive_api_key="", backtest_save_dir=str(tmp_path))
    sc.telegram = _FakeTelegram()
    TelegramAssistant(sc)._handle_backtest("دمج")
    assert sent and "لا توجد تشغيلات" in sent[0]   # لا «يحتاج MASSIVE_API_KEY»
