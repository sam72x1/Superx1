"""اختبارات تتبّع النتائج + أداة التطوير (dev_assistant)."""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone

from runner_scanner.config import Config
from runner_scanner.dev_assistant import build_dev_report, esc
from runner_scanner.models import (
    Candidate, Catalyst, FloatSource, MomentumResult, ReadinessResult,
    RiskPlan, Session, SnapshotEntry,
)
from runner_scanner.state import Store

CFG = Config.from_env()
T0 = datetime(2026, 6, 26, 14, 0, tzinfo=timezone.utc)


def _store():
    return Store(os.path.join(tempfile.mkdtemp(), "o.sqlite3"))


def _cand(ticker, price, *, news=True, rejected=False, reason=None,
          stop=None, t1=None, rvol=8.0):
    c = Candidate(
        snapshot=SnapshotEntry(ticker, price, price * 0.8, price * 0.8, price,
                               price * 0.78, 1_000_000, price * 0.95, 25.0),
        session=Session.REGULAR)
    c.momentum = MomentumResult(score=35, rvol=rvol, rvol_5min=22,
                                change_5min_pct=3, vwap_distance_pct=4,
                                above_vwap=True, volume_rising=True)
    c.readiness = ReadinessResult(
        classic_score=80, pillar_score=40, trend="صاعد", rsi=60,
        macd_bull=True, divergence="لا شيء", above_ma50=True,
        above_ma200=True, golden_cross=True)
    c.float_shares = 5_000_000
    c.float_source = FloatSource.FLOAT_ENDPOINT
    c.catalyst = Catalyst(has_news=news)
    if rejected:
        c.reject(reason)
    else:
        c.final_score = 80
        c.risk = RiskPlan(stop_price=stop, stop_pct=10, entry_ref=price,
                          targets=[t1, t1 * 1.1, t1 * 1.2], stop_basis="دعم 5د")
    return c


# ── تتبّع النتائج ─────────────────────────────────────────────────
def test_outcome_win_on_target_hit():
    st = _store()
    c = _cand("WIN", 3.0, stop=2.7, t1=3.6)
    st.log_candidate(c, T0)
    st.mark_alerted("WIN", 80, T0)
    events = st.update_outcomes(
        {"WIN": 3.7}, datetime(2026, 6, 26, 14, 10, tzinfo=timezone.utc))
    row = st.fetch_resolved(only_alerts=True)[0]
    assert row["result"] == "win" and row["hit_target"] == 1
    # حدث متابعة: تحقيق الهدف الأول
    assert any(e["type"] == "target" and e["level"] == 1 for e in events)


def test_outcome_loss_on_stop_hit():
    st = _store()
    c = _cand("LOSE", 5.0, stop=4.5, t1=6.0)
    st.log_candidate(c, T0)
    st.mark_alerted("LOSE", 75, T0)
    events = st.update_outcomes(
        {"LOSE": 4.4}, datetime(2026, 6, 26, 14, 10, tzinfo=timezone.utc))
    row = st.fetch_resolved(only_alerts=True)[0]
    assert row["result"] == "loss" and row["hit_stop"] == 1
    assert any(e["type"] == "stop" for e in events)


def test_outcome_timeout_when_window_passes():
    st = _store()
    c = _cand("FLAT", 10.0, stop=9.0, t1=12.0)
    st.log_candidate(c, T0)
    st.mark_alerted("FLAT", 70, T0)
    st.update_outcomes({"FLAT": 10.2},
                       datetime(2026, 6, 26, 16, 0, tzinfo=timezone.utc),
                       window_min=90)
    row = st.fetch_resolved(only_alerts=True)[0]
    assert row["result"] == "timeout"


def test_surge_event_on_new_leg():
    st = _store()
    c = _cand("SURGE", 3.0, stop=2.7, t1=10.0)   # هدف بعيد كي لا يُحسم
    st.log_candidate(c, T0)
    st.mark_alerted("SURGE", 80, T0)
    # قفزة +10% فوق سعر الدخول (≥ surge_leg 8%) → حدث قفزة قوية
    events = st.update_outcomes(
        {"SURGE": 3.3}, datetime(2026, 6, 26, 14, 5, tzinfo=timezone.utc),
        surge_leg_pct=8.0)
    assert any(e["type"] == "surge" for e in events)


# ── تنبيه الفرص الفائتة اللحظي (مرفوض صعد + سببه) ────────────────
def test_missed_event_for_rejected_runner():
    st = _store()
    c = _cand("MISS", 2.0, rejected=True, reason="جاهزية فنية 45 < 60")
    st.log_candidate(c, T0)
    # صعد +40% بعد الرفض (≥ عتبة 30%) → حدث «فرصة فائتة» + سببه
    events = st.update_outcomes(
        {"MISS": 2.8}, datetime(2026, 6, 26, 14, 10, tzinfo=timezone.utc),
        missed_rise_pct=30.0)
    missed = [e for e in events if e["type"] == "missed"]
    assert missed and missed[0]["ticker"] == "MISS"
    assert "جاهزية" in missed[0]["reason"]
    # لا يتكرّر في الدورة التالية
    again = st.update_outcomes(
        {"MISS": 2.9}, datetime(2026, 6, 26, 14, 12, tzinfo=timezone.utc),
        missed_rise_pct=30.0)
    assert not [e for e in again if e["type"] == "missed"]
    st.close()


def test_missed_disabled_by_default_threshold():
    st = _store()
    c = _cand("QUIET", 2.0, rejected=True, reason="فلوت كبير")
    st.log_candidate(c, T0)
    # بلا تمرير missed_rise_pct (الافتراضي ضخم = معطّل) → لا حدث
    events = st.update_outcomes(
        {"QUIET": 3.0}, datetime(2026, 6, 26, 14, 10, tzinfo=timezone.utc))
    assert not [e for e in events if e["type"] == "missed"]
    st.close()


def test_surge_event_carries_volume_participation():
    st = _store()
    c = _cand("PART", 3.0, stop=2.7, t1=10.0)   # هدف بعيد كي يبقى مفتوحًا
    st.log_candidate(c, T0)                       # first_volume = 1,000,000
    st.mark_alerted("PART", 80, T0)
    # قفزة +10% مع حجم تضاعف ×2 → مشاركة قوية
    events = st.update_outcomes(
        {"PART": 3.3}, datetime(2026, 6, 26, 14, 5, tzinfo=timezone.utc),
        surge_leg_pct=8.0, volume_map={"PART": 2_000_000})
    surge = [e for e in events if e["type"] == "surge"]
    assert surge and surge[0]["participation"] == "قوية ⬆️"
    st.close()


def test_followup_missed_message():
    from runner_scanner.alerts import build_followup
    msg = build_followup(CFG, {"ticker": "MISS", "type": "missed",
                               "price": 2.8, "gain_pct": 40.0,
                               "reason": "فلوت كبير"})
    assert "فرصة فائتة" in msg and "MISS" in msg and "فلوت كبير" in msg


def test_events_only_for_alerts_not_rejected():
    st = _store()
    c = _cand("REJ", 2.0, rejected=True, reason="RVol 3x < 5x")
    st.log_candidate(c, T0)   # لم يُنبَّه عنه
    events = st.update_outcomes(
        {"REJ": 3.0}, datetime(2026, 6, 26, 14, 10, tzinfo=timezone.utc))
    assert events == []       # لا أحداث للمرفوضين


def test_first_price_not_overwritten_on_relog():
    st = _store()
    c = _cand("X", 2.0, rejected=True, reason="RVol 3x < 5x")
    st.log_candidate(c, T0)
    # دورة لاحقة بسعر أعلى — first_price لازم يبقى 2.0
    c2 = _cand("X", 2.8, rejected=True, reason="RVol 3x < 5x")
    st.log_candidate(c2, T0)
    st.update_outcomes({"X": 2.8},
                       datetime(2026, 6, 26, 14, 30, tzinfo=timezone.utc))
    row = [r for r in st.fetch_resolved() if r["ticker"] == "X"]
    missed = st.fetch_missed(30.0)
    assert any(m["ticker"] == "X" for m in missed)   # صعد 40% من 2.0


def test_missed_opportunity_detected():
    st = _store()
    c = _cand("MISS", 2.0, rejected=True, reason="RVol 3.0x < 5x")
    st.log_candidate(c, T0)
    st.update_outcomes({"MISS": 2.8},
                       datetime(2026, 6, 26, 14, 10, tzinfo=timezone.utc))
    missed = st.fetch_missed(CFG.missed_rise_pct)
    assert len(missed) == 1 and missed[0]["ticker"] == "MISS"


# ── أداة التطوير ──────────────────────────────────────────────────
def test_dev_report_low_sample_shows_missed():
    st = _store()
    c = _cand("MISS", 2.0, rejected=True, reason="RVol 3.0x < 5x")
    st.log_candidate(c, T0)
    st.update_outcomes({"MISS": 2.8},
                       datetime(2026, 6, 26, 14, 10, tzinfo=timezone.utc))
    report = build_dev_report(st, CFG)
    assert "فرص فائتة" in report
    assert "MISS" in report


def test_dev_report_full_with_segments_and_suggestions():
    st = _store()
    prices = {}
    for i in range(12):
        tkr = f"W{i}"
        p = 3.0 + i * 0.1
        won = i % 3 != 0
        c = _cand(tkr, p, news=(i % 2 == 0), stop=p * 0.9, t1=p * 1.2)
        st.log_candidate(c, T0)
        st.mark_alerted(tkr, 80, T0)
        prices[tkr] = p * 1.25 if won else p * 0.88
    for i in range(4):
        tkr = f"M{i}"
        c = _cand(tkr, 2.0, rejected=True, reason="RVol 3.0x < 5x")
        st.log_candidate(c, T0)
        prices[tkr] = 3.0
    st.update_outcomes(prices,
                       datetime(2026, 6, 26, 14, 20, tzinfo=timezone.utc))
    report = build_dev_report(st, CFG)
    assert "النجاح الكلي" in report
    assert "حسب الجلسة" in report
    assert "اقتراحات ضبط" in report
    assert "RVOL_MIN" in report          # اقتراح خفض البوّابة


def test_esc_escapes_html():
    assert esc("<b>&") == "&lt;b&gt;&amp;"


def test_export_csvs_writes_files():
    import os
    from runner_scanner.dev_assistant import export_csvs
    st = _store()
    c = _cand("MISS", 2.0, rejected=True, reason="RVol 3x < 5x")
    st.log_candidate(c, T0)
    st.update_outcomes({"MISS": 2.8},
                       datetime(2026, 6, 26, 14, 10, tzinfo=timezone.utc))
    files = export_csvs(st, CFG, T0)
    assert any("missed" in os.path.basename(p) for p, _ in files)
    for path, _ in files:
        content = open(path, encoding="utf-8-sig").read()
        assert "ticker" in content and "MISS" in content
