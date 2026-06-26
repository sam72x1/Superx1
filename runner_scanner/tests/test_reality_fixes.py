"""اختبارات تثبيت إصلاحات «المنطق يناقض واقع البيانات» (الدفعة 1)."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from runner_scanner import gates
from runner_scanner.config import Config
from runner_scanner.indicators import session_vwap
from runner_scanner.intraday_ta import compute_momentum
from runner_scanner.models import Bar, Candidate, Session
from runner_scanner.risk import build_risk_plan, resistance_targets
from runner_scanner.sessions import session_cumulative_volume
from runner_scanner.tests.fixtures import make_snapshot, rising_5min_bars

CFG = Config.from_env()
ET = ZoneInfo("America/New_York")


def _tms(h, mi):
    return int(datetime(2026, 6, 26, h, mi, tzinfo=ET).timestamp() * 1000)


# ── #1: بوّابة الحجم تتخطّى الجلسات الممتدة (تعتمد على RVol) ──────
def test_volume_gate_skips_premarket():
    c = Candidate(snapshot=make_snapshot(vol=80_000), session=Session.PREMARKET)
    assert gates.check_volume(CFG, c).passed is True
    c2 = Candidate(snapshot=make_snapshot(vol=80_000), session=Session.REGULAR)
    assert gates.check_volume(CFG, c2).passed is False   # الرسمي يُطبّق العتبة


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
