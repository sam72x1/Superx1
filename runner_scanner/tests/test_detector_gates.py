"""اختبارات الكشف والبوابات الصارمة."""

from __future__ import annotations

from runner_scanner.config import Config
from runner_scanner import detector, gates
from runner_scanner.models import Candidate, FloatSource, MomentumResult
from runner_scanner.tests.fixtures import make_snapshot

CFG = Config.from_env()


def _cand(**snap_kw) -> Candidate:
    return Candidate(snapshot=make_snapshot(**snap_kw))


def test_detector_picks_only_above_threshold():
    snaps = [
        make_snapshot("A", change_pct=25.0),
        make_snapshot("B", change_pct=8.0),    # تحت العتبة (10%)
        make_snapshot("C", change_pct=60.0),
    ]
    out = detector.detect_runners(CFG, snaps)
    assert [e.ticker for e in out] == ["C", "A"]   # مرتّب تنازليًا


def test_detector_ignores_invalid_entries():
    bad = make_snapshot("X", last=0.0, prev=0.0, change_pct=25.0)
    assert bad.is_valid is False
    assert detector.detect_runners(CFG, [bad]) == []


def test_price_gate_rejects_pennies_and_highflyers():
    assert gates.check_price(CFG, _cand(last=0.5)).passed is False
    assert gates.check_price(CFG, _cand(last=45.0)).passed is False
    assert gates.check_price(CFG, _cand(last=5.0)).passed is True


def test_volume_gate_off_by_default():
    # القرار: RVol هو مقياس السيولة الوحيد → بوّابة الحجم ملغاة افتراضيًا
    assert gates.check_volume(CFG, _cand(vol=50_000)).passed is True


def test_volume_gate_when_explicitly_enabled():
    cfg = Config(volume_gate_enabled=True)
    assert gates.check_volume(cfg, _cand(vol=50_000)).passed is False
    assert gates.check_volume(cfg, _cand(vol=600_000)).passed is True


def test_float_gate_unknown_passes_but_flagged():
    c = _cand()
    c.float_shares = None
    c.float_source = FloatSource.UNKNOWN
    res = gates.check_float(CFG, c)
    assert res.passed is True and "unknown" in res.reason


def test_float_gate_rejects_large_float():
    c = _cand()
    c.float_shares = 50_000_000
    c.float_source = FloatSource.FLOAT_ENDPOINT
    assert gates.check_float(CFG, c).passed is False


def test_parabolic_gate_rejects_exhausted_runner():
    c = _cand(last=5.0, prev=2.0, change_pct=150.0)
    assert gates.check_parabolic(CFG, c).passed is False


def test_parabolic_gate_vwap_extension():
    c = _cand(change_pct=30.0)
    c.momentum = MomentumResult(
        score=40, rvol=10, rvol_5min=20, change_5min_pct=2,
        vwap_distance_pct=55.0, above_vwap=True, volume_rising=True)
    assert gates.check_parabolic(CFG, c).passed is False


# ── بوّابة VWAP (قرار المستخدم بالبيانات: تنبيه فوق VWAP فقط) ───────
def _mom(above, reliable=True):
    return MomentumResult(
        score=40, rvol=10, rvol_5min=20, change_5min_pct=2,
        vwap_distance_pct=(3.0 if above else -3.0),
        above_vwap=above, volume_rising=True, vwap_reliable=reliable)


def test_vwap_gate_rejects_below_vwap():
    c = _cand(change_pct=25.0)
    c.momentum = _mom(above=False)
    res = gates.check_vwap(CFG, c)
    assert res.passed is False and "VWAP" in res.reason


def test_vwap_gate_passes_above_vwap():
    c = _cand(change_pct=25.0)
    c.momentum = _mom(above=True)
    assert gates.check_vwap(CFG, c).passed is True


def test_vwap_gate_skips_unreliable_vwap():
    # §4: VWAP غير موثوق (artifact صفري بالجلسات الممتدة) → لا رفض
    c = _cand(change_pct=25.0)
    c.momentum = _mom(above=False, reliable=False)
    assert gates.check_vwap(CFG, c).passed is True


def test_vwap_gate_no_momentum_passes():
    c = _cand(change_pct=25.0)          # momentum=None (قبل حساب الزخم)
    assert gates.check_vwap(CFG, c).passed is True


def test_vwap_gate_disabled_passes_below():
    cfg = Config(vwap_gate_enabled=False)
    c = _cand(change_pct=25.0)
    c.momentum = _mom(above=False)
    assert gates.check_vwap(cfg, c).passed is True


def test_detector_drops_reverse_split_distortion():
    snaps = [
        make_snapshot("REAL", change_pct=50.0),
        make_snapshot("SPLIT", change_pct=800.0),   # فوق السقف 400 → تشوّه
    ]
    out = detector.detect_runners(CFG, snaps)
    assert [e.ticker for e in out] == ["REAL"]


def test_detector_filters_derivatives():
    snaps = [
        make_snapshot("REAL", change_pct=30.0),
        make_snapshot("ABCDW", change_pct=40.0),     # وارنت
        make_snapshot("GGROU", change_pct=35.0),     # يونت
    ]
    out = detector.detect_runners(CFG, snaps)
    assert [e.ticker for e in out] == ["REAL"]


def test_looks_like_derivative():
    assert detector.looks_like_derivative("ABCDW") is True
    assert detector.looks_like_derivative("ABC.WS") is True
    assert detector.looks_like_derivative("ABC.U") is True
    assert detector.looks_like_derivative("AAPL") is False
    assert detector.looks_like_derivative("BRK.A") is False


def test_listing_gate_rejects_non_common_and_otc():
    c = _cand()
    c.ticker_type = "WARRANT"
    assert gates.check_listing(CFG, c).passed is False
    c2 = _cand()
    c2.ticker_type = "CS"
    c2.primary_exchange = "OTC"
    assert gates.check_listing(CFG, c2).passed is False


def test_listing_gate_passes_common_and_unknown():
    c = _cand()
    c.ticker_type = "CS"
    c.primary_exchange = "XNAS"
    assert gates.check_listing(CFG, c).passed is True
    # مجهول النوع/البورصة → يعدّي (فائدة الشك)
    assert gates.check_listing(CFG, _cand()).passed is True


def test_rvol_gate_uses_session_rvol():
    weak = _cand()
    weak.momentum = MomentumResult(
        score=10, rvol=2.0, rvol_5min=1, change_5min_pct=1,
        vwap_distance_pct=1, above_vwap=True, volume_rising=False)
    assert gates.check_rvol(CFG, weak).passed is False
    weak.momentum.rvol = 8.0
    assert gates.check_rvol(CFG, weak).passed is True
