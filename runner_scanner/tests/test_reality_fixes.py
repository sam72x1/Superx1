"""اختبارات تثبيت إصلاحات «المنطق يناقض واقع البيانات» (الدفعة 1)."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import os
import tempfile

from runner_scanner import gates
from runner_scanner.classic_ta import compute_readiness
from runner_scanner.config import Config
from runner_scanner.indicators import detect_divergence, rsi_series, session_vwap
from runner_scanner.intraday_ta import compute_momentum
from runner_scanner.models import Bar, Candidate, Session
from runner_scanner.risk import build_risk_plan, resistance_targets
from runner_scanner.sessions import session_cumulative_volume
from runner_scanner.state import Store, trade_date_str
from runner_scanner.tests.fixtures import (
    make_snapshot, rising_5min_bars, uptrend_daily_bars)

CFG = Config.from_env()
ET = ZoneInfo("America/New_York")


def _tms(h, mi):
    return int(datetime(2026, 6, 26, h, mi, tzinfo=ET).timestamp() * 1000)


# ── #1: بوّابة الحجم — ملغاة افتراضيًا (RVol وحده)؛ ولو فُعّلت تتخطّى الممتدة
def test_volume_gate_off_by_default_relies_on_rvol():
    c = Candidate(snapshot=make_snapshot(vol=80_000), session=Session.REGULAR)
    assert gates.check_volume(CFG, c).passed is True     # ملغاة → RVol وحده


def test_volume_gate_when_enabled_skips_premarket_only():
    cfg = Config(volume_gate_enabled=True)
    pre = Candidate(snapshot=make_snapshot(vol=80_000), session=Session.PREMARKET)
    assert gates.check_volume(cfg, pre).passed is True   # ممتدة → RVol الجلسي
    reg = Candidate(snapshot=make_snapshot(vol=80_000), session=Session.REGULAR)
    assert gates.check_volume(cfg, reg).passed is False  # الرسمي يُطبّق العتبة


# ── #17: VWAP يتطلّب ≥2 شمعة مساهِمة (شمعة واحدة ليست قياسًا) ─────
def test_session_vwap_requires_min_bars():
    one = [Bar(t_ms=1, o=1, h=1, l=1, c=1, v=1000)]
    assert session_vwap(one) is None
    assert session_vwap(one * 2) is not None


# ── #2/#19: VWAP غير موثوق في البريماركت بلا شموع دقيقة → لا قياس ─
def test_vwap_unreliable_in_premarket_without_minute_bars():
    m = compute_momentum(CFG, make_snapshot(), Session.PREMARKET,
                         rising_5min_bars(), bars_1min=None)
    assert m.vwap_reliable is False
    assert m.above_vwap is False and m.vwap_distance_pct == 0.0


# ── #3/#10: الحجم التراكمي الجلسي من الشموع لا من snap.day_volume ─
def test_session_cumulative_volume_filters_to_session():
    pre = Bar(t_ms=_tms(7, 0), o=2, h=2.1, l=2, c=2.05, v=50_000)
    reg = Bar(t_ms=_tms(10, 0), o=2, h=2.1, l=2, c=2.05, v=900_000)
    total = session_cumulative_volume(CFG, Session.PREMARKET, [pre, reg])
    assert total == 50_000   # فقط شمعة البريماركت


# ── #20: زخم 5د من شمعة مكتملة لا الجارية (التي c≈o) ─────────────
def test_change_5min_from_completed_bar():
    completed = Bar(t_ms=1, o=2.0, h=2.2, l=2.0, c=2.2, v=100_000)  # +10%
    forming = Bar(t_ms=2, o=2.2, h=2.2, l=2.2, c=2.2, v=10_000)     # c==o → 0
    m = compute_momentum(CFG, make_snapshot(), Session.REGULAR,
                         [completed, forming], bars_1min=None)
    assert m.change_5min_pct > 0   # من المكتملة (+10%) لا الجارية (0)


# ── #21: حُرّاس entry<=0 في الوقف/الأهداف ────────────────────────
def test_risk_entry_guard():
    assert resistance_targets(0.0, []) == []
    rp = build_risk_plan(CFG, 0.0, [])
    assert rp.targets == [] and rp.stop_basis == "سعر غير صالح"


# ── #14: قمة شمعة رقيقة (طبعة واحدة) لا تُعدّ مقاومة-هدف ──────────
def test_thin_bar_high_excluded_from_targets():
    # شمعة رقيقة (n=1) بقمة عالية + شموع سليمة أدنى
    thin = Bar(t_ms=5, o=3.0, h=3.9, l=3.0, c=3.1, v=500, n=1)
    normal = [Bar(t_ms=i, o=3.0, h=3.2, l=2.9, c=3.1, v=80_000, n=50)
              for i in range(4)]
    bars = normal + [thin]
    tg = resistance_targets(3.05, bars, count=3, max_pct=80.0, min_bar_trades=3)
    assert 3.9 not in tg   # قمة الشمعة الرقيقة (3.9) مستبعدة من المقاومات
    # وللمقارنة: بلا فلتر السيولة تدخل 3.9
    tg_raw = resistance_targets(3.05, bars, count=3, max_pct=80.0, min_bar_trades=0)
    assert 3.9 in tg_raw


# ── منهجية المستخدم: أهداف موسومة + متوسطات ٢٠/٥٠ + قمم تأرجح ─────
def test_targets_labeled_with_kinds():
    """كل هدف يحمل نوعه (منهجية المستخدم): مقاومة/متوسط/قمة تأرجح، مع الحفاظ
    على الترتيب التصاعدي بالسعر (شرط تتبّع النتائج والباكتيست)."""
    bars = [Bar(t_ms=i, o=3.0, h=3.2, l=2.9, c=3.1, v=80_000, n=50)
            for i in range(4)]
    # المستويات فوق 3.05: 3.2 (قمة اليوم) · 3.3 (متوسط) · 3.5 (قمة تأرجح)
    labeled = resistance_targets(
        3.05, bars, count=3, min_bar_trades=3,
        ma_levels={"متوسط ٢٠": 3.3}, daily_peaks=[3.5],
        return_labeled=True)
    prices = [p for p, _ in labeled]
    kinds = [k for _, k in labeled]
    assert prices == sorted(prices)              # تصاعدي بالسعر
    assert "متوسط ٢٠" in kinds                   # المتوسط ظهر كهدف (مصدر جديد)
    assert "قمة تأرجح" in kinds                  # قمة الموجة السابقة ظهرت
    # النوع الافتراضي بلا وسم = list[float] كما قبل (توافق خلفي)
    plain = resistance_targets(3.05, bars, extra=[3.8], count=3, min_bar_trades=3)
    assert all(isinstance(t, float) for t in plain)


def test_recent_hour_high_target():
    """منهجية المستخدم (صورة 4س/5د): «أعلى سعر في آخر N ساعة» يظهر كهدف موسوم.
    قمة اليوم (قديمة) أعلى من قمة النافذة المتدحرجة الأخيرة → مستويان متمايزان."""
    # 50 شمعة 5د: أول شمعتين (خارج آخر 48) قمتهما 4.5 = قمة اليوم؛ آخر 48 قمتها 3.6
    bars = [Bar(t_ms=0, o=3.0, h=4.5, l=3.0, c=3.1, v=90_000, n=50),
            Bar(t_ms=1, o=3.0, h=4.4, l=3.0, c=3.1, v=90_000, n=50)]
    for i in range(2, 50):
        h = 3.6 if i == 25 else 3.4
        bars.append(Bar(t_ms=i, o=3.0, h=h, l=2.9, c=3.1, v=80_000, n=50))
    rp = build_risk_plan(CFG, 3.05, bars)     # target_recent_high_hours=4 → آخر 48
    kinds = " ".join(rp.target_kinds)
    assert "قمة 4س" in kinds                   # قمة النافذة المتدحرجة (3.6) ظهرت هدفًا
    assert 3.6 in rp.targets
    # تعطيلها: 0 ساعة → لا هدف «قمة Nس»
    off = replace_cfg(CFG, target_recent_high_hours=0.0)
    assert "قمة" not in " ".join(build_risk_plan(off, 3.05, bars).target_kinds)


def replace_cfg(cfg, **kw):
    from dataclasses import replace
    return replace(cfg, **kw)


def test_ma_below_entry_not_a_target():
    """متوسط تحت الدخول ليس هدفًا صاعدًا (الرنر فوق متوسطاته غالبًا)."""
    bars = [Bar(t_ms=i, o=3.0, h=3.2, l=2.9, c=3.1, v=80_000, n=50)
            for i in range(4)]
    labeled = resistance_targets(3.05, bars, count=3, min_bar_trades=3,
                                 ma_levels={"متوسط ٥٠": 2.5}, return_labeled=True)
    assert all(k != "متوسط ٥٠" for _, k in labeled)   # 2.5 < 3.05 → مُستبعد


# ── #12: RSI لا يُحشى بـ50 → لا دايفرجنس وهمي على تاريخ قصير ──────
def test_rsi_series_none_in_warmup():
    s = rsi_series([10, 11, 10.5], period=14)   # كله warmup
    assert all(v is None for v in s)
    # دايفرجنس يتجاهل نقاط None بأمان (لا استثناء، لا إشارة وهمية)
    closes = [10, 9, 11, 8, 12, 7, 13, 6, 14, 5, 15]
    assert detect_divergence(closes, rsi_series(closes, 14)) == "لا شيء"


# ── #4: تاريخ قصير → جاهزية غير مؤكَّدة (تحت العتبة) ──────────────
def test_short_history_readiness_low_confidence():
    r = compute_readiness(CFG, uptrend_daily_bars(30))   # < min_history_bars
    assert r.classic_score < CFG.tech_readiness_min
    assert any("تاريخ قصير" in n for n in r.notes)


# ── #5: first_price يُعاد تأسيسه عند الانتقال بريماركت→رسمي ───────
def test_first_price_rebaselines_premarket_to_regular():
    st = Store(os.path.join(tempfile.mkdtemp(), "reb.sqlite3"))
    from datetime import datetime, timezone
    t0 = datetime(2026, 6, 26, 14, 0, tzinfo=timezone.utc)
    day = trade_date_str(t0)
    pre = Candidate(snapshot=make_snapshot(ticker="REB", last=2.0, change_pct=25.0),
                    session=Session.PREMARKET)
    st.log_candidate(pre, t0)
    assert st.fetch_row("REB", day)["first_price"] == 2.0
    reg = Candidate(snapshot=make_snapshot(ticker="REB", last=2.5, change_pct=30.0),
                    session=Session.REGULAR)
    st.log_candidate(reg, t0)
    assert st.fetch_row("REB", day)["first_price"] == 2.5   # أُعيد تأسيسه
    st.close()


def test_first_price_kept_when_already_alerted():
    st = Store(os.path.join(tempfile.mkdtemp(), "reb2.sqlite3"))
    from datetime import datetime, timezone
    t0 = datetime(2026, 6, 26, 14, 0, tzinfo=timezone.utc)
    day = trade_date_str(t0)
    pre = Candidate(snapshot=make_snapshot(ticker="ALR", last=2.0, change_pct=25.0),
                    session=Session.PREMARKET)
    st.log_candidate(pre, t0)
    st.mark_alerted("ALR", 80, t0)   # نُبِّه في البريماركت
    reg = Candidate(snapshot=make_snapshot(ticker="ALR", last=2.5, change_pct=30.0),
                    session=Session.REGULAR)
    st.log_candidate(reg, t0)
    assert st.fetch_row("ALR", day)["first_price"] == 2.0   # يُحفظ سعر التنبيه
    st.close()
