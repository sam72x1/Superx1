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


def test_asof_client_intraday_sliced():
    full = [Bar(t_ms=_tms(2026, 6, 26, 9, 35), o=3, h=3.1, l=3, c=3.05, v=1e4),
            Bar(t_ms=_tms(2026, 6, 26, 11, 0), o=3.1, h=3.2, l=3.1, c=3.15, v=1e4)]
    asof = _tms(2026, 6, 26, 10, 0)
    pre = [b for b in full if b.t_ms <= asof]
    c = backtest.AsOfClient(_Base(), "2026-06-26", asof, pre, pre, {})
    assert len(c.bars_5min("X", "", "")) == 1      # فقط ما قبل asof


# ── تشغيل كامل end-to-end بمحاكاة ─────────────────────────────────
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
        return uptrend_daily_bars(260)

    def aggregates(self, t, mult, span, s, e, **kw):
        return uptrend_daily_bars(260)

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
    assert "هدفها الأول أقل من 10%" in rep


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


def test_reject_bucket_classifies():
    assert backtest._reject_bucket("RVol 3.0x < 5x") == "RVol"
    assert backtest._reject_bucket("فلوت 90,000,000 > 40,000,000") == "فلوت"
    assert backtest._reject_bucket("جاهزية فنية 45 < 60") == "جاهزية/درجة"
    assert backtest._reject_bucket("شيء غريب") == "أخرى"
