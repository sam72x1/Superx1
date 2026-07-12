"""اختبارات المعايرة التلقائية — بمخزن وهمي (صفوف dict)."""

from __future__ import annotations

from runner_scanner.calibration import (
    format_proposals, propose_calibrations)
from runner_scanner.config import Config


def _row(result="win", rvol=12.0, readiness=85.0, score=85.0, had_news=1,
         reject_reason="", max_gain_pct=0.0, float_shares=5e6):
    return {"result": result, "rvol": rvol, "readiness": readiness,
            "score": score, "had_news": had_news,
            "reject_reason": reject_reason, "max_gain_pct": max_gain_pct,
            "float_shares": float_shares}


class _FakeStore:
    def __init__(self, alerts, missed=None):
        self._alerts = alerts
        self._missed = missed or []

    def fetch_resolved(self, only_alerts=False):
        return self._alerts

    def fetch_missed(self, pct):
        return self._missed


def test_no_data_no_proposals():
    props = propose_calibrations(_FakeStore([]), Config())
    assert props == []
    assert format_proposals(props) == ""


def test_raises_rvol_min_when_low_bucket_loses():
    # 8 منخفضة RVol كلها خسارة + 8 مرتفعة كلها نجاح → ارفع RVOL_MIN
    alerts = ([_row(result="loss", rvol=6.0) for _ in range(8)]
              + [_row(result="win", rvol=12.0) for _ in range(8)])
    props = propose_calibrations(_FakeStore(alerts), Config())
    rvol = [p for p in props if p.env == "RVOL_MIN"]
    assert rvol and rvol[0].proposed == 7   # 5 → 7


def test_raises_readiness_when_band_above_threshold_loses():
    # العتبة 60 وشريحة 60-70 كلها خسارة + 70+ نجاح → اقترح رفع العتبة إلى 70
    alerts = ([_row(result="loss", readiness=65.0) for _ in range(8)]
              + [_row(result="win", readiness=85.0) for _ in range(8)])
    cfg = Config(tech_readiness_min=60.0)
    props = propose_calibrations(_FakeStore(alerts), cfg)
    rd = [p for p in props if p.env == "TECH_READINESS_MIN"]
    assert rd and rd[0].current == 60.0 and rd[0].proposed == 70


def _missed(reject_reason, rvol=None, float_shares=None, max_gain_pct=50.0):
    """صف فرصة فائتة وهمي (بحقلَي rvol/float_shares اللذين يقرؤهما المقترح)."""
    return {"reject_reason": reject_reason, "max_gain_pct": max_gain_pct,
            "rvol": rvol, "float_shares": float_shares}


def test_raises_float_max_on_missed_opportunities():
    # فلوت الفائتين تحت السقف المقترح (60M) → يُعدّون ويظهر الاقتراح
    alerts = [_row(result="win", rvol=12.0) for _ in range(4)]
    missed = [_missed("فلوت كبير", float_shares=50e6) for _ in range(3)]
    props = propose_calibrations(_FakeStore(alerts, missed), Config())
    fl = [p for p in props if p.env == "FLOAT_MAX"]
    assert fl and fl[0].proposed > Config().float_max


def test_no_rvol_proposal_when_missed_below_proposed_threshold():
    """ت2 (إعادة إنتاج الحالة الحية 11 يوليو): 6 فائتين بـRVol أغلبهم دون
    العتبة المقترحة (4x) → القابل للالتقاط 1 فقط < 3 → لا اقتراح خفض.
    كان الكود القديم يعدّ الستّة ويقترح خطأً."""
    alerts = [_row(result="win", rvol=12.0) for _ in range(4)]
    missed = [_missed("RVol 0.7x < 5.0x", rvol=r)
              for r in (0.7, 0.7, 0.8, 1.6, 1.8, 4.6)]
    props = propose_calibrations(_FakeStore(alerts, missed), Config())
    assert not [p for p in props if p.env == "RVOL_MIN"]


def test_rvol_proposal_when_enough_catchable_missed():
    """ت2: 3 فائتين RVol فوق العتبة المقترحة (4x) → يظهر الاقتراح، والنص
    يذكر العتبة المقترحة والعدد الحقيقي القابل للالتقاط."""
    alerts = [_row(result="win", rvol=12.0) for _ in range(4)]
    missed = [_missed("RVol 4.5x < 5.0x", rvol=r) for r in (4.2, 4.5, 4.9)]
    props = propose_calibrations(_FakeStore(alerts, missed), Config())
    rv = [p for p in props if p.env == "RVOL_MIN"]
    assert rv and rv[0].proposed == 4 and "4x" in rv[0].reason and "3" in rv[0].reason


def test_float_proposal_ignores_missed_above_proposed_cap():
    """ت2: فائتون فلوتهم فوق السقف المقترح (60M) لا يُعدّون؛ من هم تحته
    ≥3 → يظهر. (مثال حي: ILLR فلوته 177M لا يبرّر رفع السقف إلى 60M.)"""
    alerts = [_row(result="win", rvol=12.0) for _ in range(4)]
    missed = ([_missed("فلوت كبير", float_shares=177e6) for _ in range(4)]
              + [_missed("فلوت كبير", float_shares=50e6) for _ in range(3)])
    props = propose_calibrations(_FakeStore(alerts, missed), Config())
    fl = [p for p in props if p.env == "FLOAT_MAX"]
    assert fl and "3" in fl[0].reason   # الثلاثة القابلون فقط
    # ولو كل الفائتين فوق السقف المقترح → لا اقتراح
    over = [_missed("فلوت كبير", float_shares=177e6) for _ in range(5)]
    props2 = propose_calibrations(_FakeStore(alerts, over), Config())
    assert not [p for p in props2 if p.env == "FLOAT_MAX"]


def test_unknown_rvol_or_float_not_counted_as_catchable():
    """ت2: قيمة مجهولة (None) لا تُعدّ قابلة للالتقاط — لا نبني على مجهول."""
    alerts = [_row(result="win", rvol=12.0) for _ in range(4)]
    rv_none = [_missed("RVol", rvol=None) for _ in range(5)]
    assert not [p for p in propose_calibrations(_FakeStore(alerts, rv_none),
                                                Config()) if p.env == "RVOL_MIN"]
    fl_none = [_missed("فلوت", float_shares=None) for _ in range(5)]
    assert not [p for p in propose_calibrations(_FakeStore(alerts, fl_none),
                                                Config()) if p.env == "FLOAT_MAX"]


def test_lowers_catalyst_bonus_when_news_underperforms():
    # «بلا محفّز» يتفوّق على «بمحفّز» بفارق واضح → اقترح خفض وزن الخبر
    # (8 بخبر نصفها خسارة = 50% مقابل 8 بلا خبر كلها نجاح = 100%).
    alerts = ([_row(result="win", had_news=1) for _ in range(4)]
              + [_row(result="loss", had_news=1) for _ in range(4)]
              + [_row(result="win", had_news=0) for _ in range(8)])
    cfg = Config(catalyst_score_bonus=8.0)
    props = propose_calibrations(_FakeStore(alerts), cfg)
    cat = [p for p in props if p.env == "CATALYST_SCORE_BONUS"]
    assert cat and cat[0].current == 8.0 and cat[0].proposed == 4


def test_raises_catalyst_bonus_when_news_outperforms():
    # «بمحفّز» يتفوّق بوضوح → اقترح رفع وزن الخبر (الاتجاه المقابل)
    alerts = ([_row(result="win", had_news=1) for _ in range(8)]
              + [_row(result="loss", had_news=0) for _ in range(4)]
              + [_row(result="win", had_news=0) for _ in range(4)])
    cfg = Config(catalyst_score_bonus=8.0)
    props = propose_calibrations(_FakeStore(alerts), cfg)
    cat = [p for p in props if p.env == "CATALYST_SCORE_BONUS"]
    assert cat and cat[0].proposed == 12


def test_no_catalyst_proposal_when_news_neutral():
    # فارق ضئيل بين الفئتين (< 10 نقاط) → لا اقتراح في أي اتجاه
    # بمحفّز: 19/20 = 95% · بلا محفّز: 18/20 = 90% → الفارق 5 نقاط فقط.
    alerts = ([_row(result="win", had_news=1) for _ in range(19)]
              + [_row(result="loss", had_news=1) for _ in range(1)]
              + [_row(result="win", had_news=0) for _ in range(18)]
              + [_row(result="loss", had_news=0) for _ in range(2)])
    props = propose_calibrations(_FakeStore(alerts), Config(catalyst_score_bonus=8.0))
    assert not [p for p in props if p.env == "CATALYST_SCORE_BONUS"]


def test_format_proposals_renders_numbers():
    alerts = ([_row(result="loss", rvol=6.0) for _ in range(8)]
              + [_row(result="win", rvol=12.0) for _ in range(8)])
    text = format_proposals(propose_calibrations(_FakeStore(alerts), Config()))
    assert "RVOL_MIN" in text and "→" in text


def test_top_action_picks_single_priority():
    """/improve يلخّص أهم إجراء واحد جاهز من اقتراحات المعايرة."""
    from runner_scanner.dev_assistant import top_action
    alerts = ([_row(result="loss", rvol=6.0) for _ in range(8)]
              + [_row(result="win", rvol=12.0) for _ in range(8)])
    txt = top_action(_FakeStore(alerts), Config())
    assert "أهم إجراء" in txt and "RVOL_MIN" in txt
    assert "<" not in txt.replace("<b>", "").replace("</b>", "").replace(
        "<i>", "").replace("</i>", "")   # HTML-آمن


def test_top_action_empty_when_no_data():
    from runner_scanner.dev_assistant import top_action
    txt = top_action(_FakeStore([]), Config())
    assert "لا إجراء عاجل" in txt


def test_rvol_raise_ignores_stocks_proposal_wont_excise():
    """BUG-09: شريحة رفع RVOL_MIN تُحسب بما سيستأصله المقترَح (rvol<7) لا [5,8).
    خاسرون عند 7.5x يبقون فوق العتبة المقترحة (7) فلا يبرّرون رفعها."""
    alerts = ([_row(result="loss", rvol=7.5) for _ in range(8)]
              + [_row(result="win", rvol=12.0) for _ in range(8)])
    props = propose_calibrations(_FakeStore(alerts), Config())
    assert not [p for p in props if p.env == "RVOL_MIN"]


def test_rvol_raise_counts_only_excised_slice():
    """BUG-09 (ضبط موجب): خاسرون داخل الشريحة المستأصَلة (6.5<7) يُنتجون الاقتراح.
    ملاحظة صدق: يمرّ على الكود قبل الإصلاح أيضًا (6.5 داخل الشريحتين القديمة<8
    والجديدة<7)؛ التضييق نفسه يثبّته التوأم test_rvol_raise_ignores_… (خاسر 7.5
    داخل القديمة خارج الجديدة). هذا يضمن ألّا يختفي الاقتراح المشروع فحسب."""
    alerts = ([_row(result="loss", rvol=6.5) for _ in range(8)]
              + [_row(result="win", rvol=12.0) for _ in range(8)])
    rv = [p for p in propose_calibrations(_FakeStore(alerts), Config())
          if p.env == "RVOL_MIN"]
    assert rv and rv[0].proposed == 7


def test_score_raise_ignores_stocks_proposal_wont_excise():
    """BUG-09: شريحة رفع ALERT_SCORE_MIN [60, proposed=65) لا [60,70).
    خاسرون عند درجة 67 يبقون فوق 65 فلا يبرّرون الرفع."""
    alerts = ([_row(result="loss", score=67.0) for _ in range(8)]
              + [_row(result="win", score=85.0) for _ in range(8)])
    props = propose_calibrations(_FakeStore(alerts), Config(alert_score_min=60.0))
    assert not [p for p in props if p.env == "ALERT_SCORE_MIN"]


def test_score_raise_counts_only_excised_slice():
    """BUG-09 (ضبط موجب): خاسرون داخل الشريحة المستأصَلة (62<65) يُنتجون الاقتراح=65.
    ملاحظة صدق: يمرّ قبل الإصلاح أيضًا (62 داخل [60,70) و[60,65))؛ التضييق نفسه
    يثبّته التوأم test_score_raise_ignores_… (خاسر 67 داخل القديمة خارج الجديدة)."""
    alerts = ([_row(result="loss", score=62.0) for _ in range(8)]
              + [_row(result="win", score=85.0) for _ in range(8)])
    sc = [p for p in propose_calibrations(_FakeStore(alerts),
                                          Config(alert_score_min=60.0))
          if p.env == "ALERT_SCORE_MIN"]
    assert sc and sc[0].proposed == 65
