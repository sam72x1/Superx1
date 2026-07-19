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


def test_target_event_carries_ratcheted_stop():
    """طلب المستخدم: حدث تحقيق الهدف يحمل الوقف المُرقّى (تعادل بعد هدف1، الهدف
    السابق بعد كل هدف تالٍ) لتذكير المستخدم برفع وقفه."""
    st = _store()
    c = _cand("RT", 3.0, stop=2.7, t1=3.6)     # أهداف [3.6, 3.96, 4.32]
    st.log_candidate(c, T0)
    st.mark_alerted("RT", 80, T0)
    events = st.update_outcomes(                 # 4.0 يتجاوز هدف1 وهدف2
        {"RT": 4.0}, datetime(2026, 6, 26, 14, 10, tzinfo=timezone.utc))
    tgts = sorted([e for e in events if e["type"] == "target"],
                  key=lambda e: e["level"])
    # الهدف1: التعادل يُصاغ «سعر دخولك» بلا رقم (new_stop=None) لتجنّب اختلاف
    # first_price عن سعر دخول البطاقة؛ الهدف2: مستوى مطلق = الهدف1.
    assert tgts[0]["level"] == 1 and tgts[0]["new_stop"] is None
    assert tgts[1]["level"] == 2 and tgts[1]["new_stop"] == 3.6   # الهدف1
    st.close()


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


def test_same_pulse_target_and_stop_records_loss():
    """§8/BUG-08: نبضة تلمس الهدف والوقف معًا = خسارة (لا ربح). نصنع صفًّا قمّته
    تجاوزت الهدف وقاعه لمس الوقف قبل الحسم (تراكم عبر فجوة النبضة)، ثم نبضة
    واحدة تحسمه: يجب loss، مع إصدار حدثَي الهدف والوقف (لا نكبت رسالة).
    قبل الإصلاح كانت حلقة الأهداف تسبق فرع الوقف فتسجّل win وتنفخ نسبة النجاح."""
    st = _store()
    c = _cand("BOTH", 3.0, stop=2.7, t1=3.3)
    st.log_candidate(c, T0)
    st.mark_alerted("BOTH", 80, T0)
    st._conn.execute(
        "UPDATE tracking SET high_after=3.5, low_after=2.6 WHERE ticker='BOTH'")
    st._conn.commit()
    events = st.update_outcomes(
        {"BOTH": 3.0}, datetime(2026, 6, 26, 14, 10, tzinfo=timezone.utc))
    row = st.fetch_resolved(only_alerts=True)[0]
    assert row["result"] == "loss"                     # §8: تحفّظ
    assert row["hit_target"] == 1 and row["hit_stop"] == 1
    assert any(e["type"] == "target" for e in events)  # كلا الحدثين يُصدَران
    assert any(e["type"] == "stop" for e in events)
    st.close()


def test_target_then_stop_across_pulses_stays_win():
    """تمييز مهم: هدف لُمس في نبضة سابقة (result=win) ثم وقف لاحق يبقى win —
    القاعدة لنفس النبضة فقط، لا للخروج المشروع بعد بلوغ الهدف."""
    st = _store()
    c = _cand("SEQ", 3.0, stop=2.7, t1=3.3)
    st.log_candidate(c, T0)
    st.mark_alerted("SEQ", 80, T0)
    st.update_outcomes({"SEQ": 3.4},   # نبضة 1: الهدف → win
                       datetime(2026, 6, 26, 14, 5, tzinfo=timezone.utc))
    st.update_outcomes({"SEQ": 2.6},   # نبضة 2: الوقف لاحقًا
                       datetime(2026, 6, 26, 14, 10, tzinfo=timezone.utc))
    row = st.fetch_resolved(only_alerts=True)[0]
    assert row["result"] == "win"      # يبقى win (الهدف بُلغ أولًا فعلًا)
    st.close()


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


def test_missed_block_shows_draw_and_stop_touch():
    """قسم الفرص الفائتة يعرض القاع ولمس مسافة الوقف بجانب القمة —
    القمة وحدها تخدع (سهم +40% قاعه -15% كان غالبًا سيُوقَف)."""
    st = _store()
    c = _cand("FOMO", 2.0, rejected=True, reason="جاهزية فنية 45 < 60")
    st.log_candidate(c, T0)
    # هبط -15% (أعمق من مسافة الوقف 7%) ثم قمّ +40%
    st.update_outcomes({"FOMO": 1.7},
                       datetime(2026, 6, 26, 14, 5, tzinfo=timezone.utc))
    st.update_outcomes({"FOMO": 2.8},
                       datetime(2026, 6, 26, 14, 10, tzinfo=timezone.utc))
    rep = build_dev_report(st, CFG)
    assert "وسيط القمة" in rep and "وسيط القاع" in rep
    assert "لمس مسافة الوقف" in rep and "1/1" in rep
    assert "+40% / -15%" in rep          # قمة/قاع معًا للسهم الفائت
    st.close()


def test_missed_ordering_stop_before_peak():
    """ب1: الوقف لُمس قبل القمة → «⛔ الوقف أولًا» + عدّ مؤكّد بالترتيب."""
    st = _store()
    c = _cand("STOP1ST", 2.0, rejected=True, reason="جاهزية فنية 45 < 60")
    st.log_candidate(c, T0)
    # هبط تحت مسافة الوقف (7%) أولًا، ثم قمّ +40% لاحقًا
    st.update_outcomes({"STOP1ST": 1.80},   # -10% (لمس مسافة الوقف)
                       datetime(2026, 6, 26, 14, 5, tzinfo=timezone.utc),
                       stop_dist_pct=CFG.stop_fixed_pct)
    st.update_outcomes({"STOP1ST": 2.80},   # +40% لاحقًا (القمة بعد الوقف)
                       datetime(2026, 6, 26, 14, 10, tzinfo=timezone.utc),
                       stop_dist_pct=CFG.stop_fixed_pct)
    rep = build_dev_report(st, CFG)
    assert "⛔ الوقف أولًا" in rep
    assert "سُتوقَف قبل القمة (مؤكّد بالترتيب الزمني): 1/1" in rep
    st.close()


def test_missed_ordering_peak_first_clean_run():
    """ب1: قمّ دون لمس الوقف → «✅ القمة أولًا» (فرصة حقيقية فاتت)."""
    st = _store()
    c = _cand("PEAK1ST", 2.0, rejected=True, reason="RVol 0.7x < 5.0x")
    st.log_candidate(c, T0)
    st.update_outcomes({"PEAK1ST": 2.80},   # +40% نظيف بلا لمس وقف
                       datetime(2026, 6, 26, 14, 5, tzinfo=timezone.utc),
                       stop_dist_pct=CFG.stop_fixed_pct)
    rep = build_dev_report(st, CFG)
    assert "✅ القمة أولًا" in rep
    assert "سُتوقَف قبل القمة (مؤكّد بالترتيب الزمني): 0/1" in rep
    st.close()


def test_missed_ordering_disabled_no_timestamps():
    """توافق: بلا stop_dist_pct (الافتراضي) لا طوابع ترتيب → لا وسم مؤكّد."""
    st = _store()
    c = _cand("OLD", 2.0, rejected=True, reason="جاهزية فنية 45 < 60")
    st.log_candidate(c, T0)
    st.update_outcomes({"OLD": 2.80},        # بلا stop_dist_pct
                       datetime(2026, 6, 26, 14, 5, tzinfo=timezone.utc))
    rep = build_dev_report(st, CFG)
    # peak_at يُسجَّل دائمًا، لكن stop_dist_at لا (العتبة 0) → القمة أولًا
    assert "مؤكّد بالترتيب الزمني" in rep     # يوجد ترتيب (القمة أولًا)
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


def test_rejected_runner_not_closed_on_stop_keeps_missed_alive():
    """BUG-15: صفّ مرفوض (غير مُنبَّه) له وقف افتراضي — يُرفض عند بوّابة الحد
    الأدنى للربح **بعد** بناء خطة المخاطرة، فيُسجَّل بوقف. لو لمس الوقف مبكرًا
    ثم انطلق +50% لاحقًا، يجب أن يبقى مفتوحًا كي ينطلق تنبيه 👻 الفرصة الفائتة
    — لا أن يُغلق على الوقف (كان يموت 👻 قبل انطلاقه). نسجّل hit_stop للمعايرة."""
    st = _store()
    c = _cand("LATE", 2.0, rejected=True, reason="سقف ربح الأهداف 8% < 12%")
    st.log_candidate(c, T0)
    # وقف افتراضي على صفّ مرفوض (يحاكي الرفض عند بوّابة الحد الأدنى للربح)
    st._conn.execute("UPDATE tracking SET stop_price=1.8 WHERE ticker='LATE'")
    st._conn.commit()
    # نبضة 1: يهبط تحت الوقف الافتراضي (1.7 < 1.8) قبل أي صعود
    ev1 = st.update_outcomes(
        {"LATE": 1.7}, datetime(2026, 6, 26, 14, 5, tzinfo=timezone.utc),
        missed_rise_pct=30.0)
    assert not [e for e in ev1 if e["type"] == "missed"]   # لم يصعد بعد
    row = st.fetch_row("LATE")
    assert row["outcome"] == "open"        # لم يُغلق على الوقف (BUG-15)
    assert row["hit_stop"] == 1            # لكن hit_stop سُجِّل للمعايرة
    # نبضة 2: ينطلق +50% لاحقًا (3.0 من 2.0) — 👻 يجب أن ينطلق رغم لمس الوقف
    ev2 = st.update_outcomes(
        {"LATE": 3.0}, datetime(2026, 6, 26, 14, 10, tzinfo=timezone.utc),
        missed_rise_pct=30.0)
    missed = [e for e in ev2 if e["type"] == "missed"]
    assert missed and missed[0]["ticker"] == "LATE"
    st.close()


def test_alert_still_closes_on_stop():
    """توازن BUG-15: المُنبَّه عنه (لا المرفوض) يبقى يُغلق على الوقف كالمعتاد."""
    st = _store()
    c = _cand("ALRT", 5.0, stop=4.5, t1=6.0)
    st.log_candidate(c, T0)
    st.mark_alerted("ALRT", 75, T0)
    st.update_outcomes(
        {"ALRT": 4.4}, datetime(2026, 6, 26, 14, 10, tzinfo=timezone.utc))
    row = st.fetch_row("ALRT")
    assert row["outcome"] == "closed" and row["result"] == "loss"
    st.close()


def test_alert_entry_price_reanchors_from_card_not_first_sighting():
    """BUG-32: سهم يُرصد بسعر منخفض (25) ثم يُنبَّه عنه لاحقًا بسعر أعلى (29.25)
    ووقف البطاقة 27.2 **فوق** سعر أوّل رصد. قبل الإصلاح كان first_price=25 < الوقف
    فيُطلق hit_stop زائفًا فور أوّل رصد (PHOE 2026-07-07 حيًّا: hit_stop=1 رغم win).
    بعد الإصلاح تُقاس النتيجة من سعر دخول البطاقة 29.25 المُرسى، فسعرٌ عند 28
    (فوق الوقف) لا يلمس الوقف، والصفّ يحمل خطة البطاقة كما رآها المستخدم."""
    st = _store()
    c = _cand("PHOE", 25.0, rejected=True, reason="تحت VWAP")   # أوّل رصد 25، بلا خطة
    st.log_candidate(c, T0)
    st._conn.execute(     # محاكاة تراكم قمة/قاع قبل التنبيه (كالحي)
        "UPDATE tracking SET high_after=33.0, low_after=25.0 WHERE ticker='PHOE'")
    st._conn.commit()
    # تنبيه لاحق بسعر البطاقة 29.25 (وقف 27.2025 · أهداف 30/35/49.45)
    st.mark_alerted("PHOE", 80, T0, entry_price=29.25, stop_price=27.2025,
                    targets=[30.0, 35.0, 49.45])
    row = st.fetch_row("PHOE")
    assert row["entry_price"] == 29.25 and row["is_alert"] == 1
    assert row["stop_price"] == 27.2025            # وقف البطاقة لا وقفٌ قديم
    assert row["target1"] == 30.0
    assert row["high_after"] == 29.25 and row["low_after"] == 29.25  # أُرسيا للدخول
    # سعر عند 28 (فوق الوقف 27.2025) → لا hit_stop زائف، والقياس من 29.25
    st.update_outcomes({"PHOE": 28.0},
                       datetime(2026, 6, 26, 14, 10, tzinfo=timezone.utc))
    row = st.fetch_row("PHOE")
    assert row["hit_stop"] == 0                    # قبل الإصلاح كان 1 زائفًا
    assert row["result"] != "loss"
    st.close()


def test_rejected_row_ignores_stop_above_basis_but_keeps_valid_stop():
    """BUG-32 (قصّة FBRX): صفّ مرفوض ورث وقفًا **فوق** سعر أوّل رصده (مجمَّد من
    دورة سابقة بسعر أعلى عبر COALESCE) — يجب ألّا يُطلق hit_stop زائفًا. وفي
    المقابل صفّ مرفوض بوقف **تحت** أساسه (BUG-15) يظلّ يسجّل hit_stop للمعايرة."""
    st = _store()
    # FBRX: أوّل رصد 24.95، وقف موروث 25.947 (فوق الأساس = مجمَّد من سعر أعلى)
    bad = _cand("FBRX", 24.95, rejected=True, reason="حركة متقدّمة 46%")
    st.log_candidate(bad, T0)
    st._conn.execute("UPDATE tracking SET stop_price=25.947 WHERE ticker='FBRX'")
    st._conn.commit()
    st.update_outcomes({"FBRX": 24.95},
                       datetime(2026, 6, 26, 14, 10, tzinfo=timezone.utc))
    assert st.fetch_row("FBRX")["hit_stop"] == 0     # وقف فوق الأساس → يُتجاهَل
    # BUG-15: وقف تحت الأساس (1.8 < 2.0) لا يزال يُسجَّل hit_stop للمعايرة
    ok = _cand("LATE2", 2.0, rejected=True, reason="سقف ربح 8% < 12%")
    st.log_candidate(ok, T0)
    st._conn.execute("UPDATE tracking SET stop_price=1.8 WHERE ticker='LATE2'")
    st._conn.commit()
    st.update_outcomes({"LATE2": 1.7},
                       datetime(2026, 6, 26, 14, 10, tzinfo=timezone.utc))
    assert st.fetch_row("LATE2")["hit_stop"] == 1
    st.close()


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


def _resolved_alert(st, ticker, day_dt, *, win):
    """ينشئ تنبيهًا محسومًا (فوز/خسارة) في يوم محدّد — لاختبار مقارنة قبل/بعد."""
    from datetime import timedelta
    price, stop, t1 = 3.0, 2.7, 3.6      # هدف1 +20% · وقف -10%
    c = _cand(ticker, price, stop=stop, t1=t1)
    st.log_candidate(c, day_dt)
    st.mark_alerted(ticker, 80, day_dt)
    final = t1 + 0.1 if win else stop - 0.1
    st.update_outcomes({ticker: final}, day_dt + timedelta(minutes=10))


def test_dev_report_week_over_week_compare():
    """طلب المستخدم: التقرير يعرض مقارنة «قبل/بعد» من النتائج الحيّة الفعلية
    (أسبوع مقابل أسبوع) لقياس أثر تغييرات الفرز — لا محاكاة."""
    st = _store()
    now = datetime(2026, 7, 5, 14, 0, tzinfo=timezone.utc)
    cur_day = datetime(2026, 7, 1, 14, 0, tzinfo=timezone.utc)    # ضمن آخر 7
    prev_day = datetime(2026, 6, 24, 14, 0, tzinfo=timezone.utc)  # الأسبوع السابق
    # الأسبوع الحالي: 2 فوز / 1 خسارة → 67%
    _resolved_alert(st, "CUR_W1", cur_day, win=True)
    _resolved_alert(st, "CUR_W2", cur_day, win=True)
    _resolved_alert(st, "CUR_L1", cur_day, win=False)
    # الأسبوع السابق: 1 فوز / 1 خسارة → 50%
    _resolved_alert(st, "PRV_W1", prev_day, win=True)
    _resolved_alert(st, "PRV_L1", prev_day, win=False)
    rep = build_dev_report(st, CFG, now)
    assert "قبل/بعد" in rep
    assert "الصفقات المحسومة: 3 مقابل 2" in rep
    assert "نسبة الفوز: 67% مقابل 50%" in rep
    assert "لمس الوقف: 1/3 مقابل 1/2" in rep
    assert "عيّنة صغيرة" in rep            # 5 نتائج < dev_min_sample → تحذير صدق
    st.close()


def test_dev_report_distribution_honesty_tail_warning():
    """صدق التوزيع (مقتبس من أداة الباكتيست): متوسط ≫ وسيط → تحذير أن الحافة
    يحملها ذيل قِلّة، والوسيط يظهر في الشرائح."""
    st = _store()
    prices = {}
    # 11 فائزًا صغيرًا (قمة +8%) + فائز واحد ضخم (+300%) = متوسط منفوخ، وسيط صغير
    for i in range(11):
        tkr = f"S{i}"
        c = _cand(tkr, 3.0, stop=2.7, t1=3.24)     # هدف1 +8%
        st.log_candidate(c, T0)
        st.mark_alerted(tkr, 80, T0)
        prices[tkr] = 3.24
    big = _cand("BIG", 3.0, stop=2.7, t1=3.24)
    st.log_candidate(big, T0)
    st.mark_alerted("BIG", 80, T0)
    prices["BIG"] = 12.0                            # +300% ذيل
    st.update_outcomes(prices,
                       datetime(2026, 6, 26, 14, 20, tzinfo=timezone.utc))
    rep = build_dev_report(st, CFG)
    assert "وسيط" in rep                            # الوسيط يُعرض
    assert "صدق التوزيع" in rep and "المتوسط يخدع" in rep
    st.close()


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


def test_dev_bucket_labels_match_bounds():
    """BUG-14 (فحص وحدة): _bucket يستبعد القيم دون الحدّ لا يُسمّيها بالشريحة."""
    from runner_scanner.dev_assistant import _bucket
    rvol_edges = [(5, 8, "5-8x"), (8, 15, "8-15x"), (15, 1e9, "15x أو أكثر")]
    assert _bucket(2.0, rvol_edges) is None      # دون 5 → مستبعَد لا «5-8x»
    assert _bucket(6.0, rvol_edges) == "5-8x"
    score_edges = [(60, 70, "60-70"), (70, 80, "70-80")]
    assert _bucket(40.0, score_edges) is None    # دون 60 → مستبعَد لا «60-70»
    assert _bucket(65.0, score_edges) == "60-70"


def test_dev_report_excludes_subthreshold_rows_from_labeled_buckets():
    """BUG-14 (تحقّق عدائي — يُشغّل build_dev_report فعلًا، لا _bucket معزولًا):
    4 صفقات محسومة بـRVol=2 ودرجة=40 (دون حدّي «5-8x»/«60-70») يجب ألّا تظهر
    تحت تينك التسميتين في التقرير. قبل إصلاح BUG-14 (حدّ الشريحة 0) كانت تُنسب
    خطأً فيهيمن أسهم دون البوّابة على شريحة يعاير المستخدم عليها. هذا الاختبار
    يفشل على الكود قبل الإصلاح (بخلاف فحص الوحدة الذي يمرّر الحدود الصحيحة بنفسه)."""
    st = _store()
    prices = {}
    for i in range(12):   # ≥ dev_min_sample كي تُرسَم شرائح RVol/الدرجة أصلًا
        tkr = f"SUB{i}"
        st.log_candidate(_cand(tkr, 3.0, stop=2.7, t1=3.3), T0)
        st.mark_alerted(tkr, 80, T0)
        prices[tkr] = 3.4     # فوز (بلغ الهدف)
        st._conn.execute(     # دون حدّي الشريحتين: RVol=2 (<5) ودرجة=40 (<60)
            "UPDATE tracking SET rvol=2.0, score=40 WHERE ticker=?", (tkr,))
    st._conn.commit()
    st.update_outcomes(prices,
                       datetime(2026, 6, 26, 14, 20, tzinfo=timezone.utc))
    rep = build_dev_report(st, CFG)
    assert "5-8x" not in rep      # RVol=2 مستبعَد لا يُنسب لـ«5-8x»
    assert "60-70" not in rep     # درجة=40 مستبعَدة لا تُنسب لـ«60-70»
    st.close()
