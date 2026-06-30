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


def test_raises_float_max_on_missed_opportunities():
    alerts = [_row(result="win", rvol=12.0) for _ in range(4)]
    missed = [{"reject_reason": "فلوت كبير", "max_gain_pct": 50.0,
               "ticker": f"M{i}"} for i in range(3)]
    props = propose_calibrations(_FakeStore(alerts, missed), Config())
    fl = [p for p in props if p.env == "FLOAT_MAX"]
    assert fl and fl[0].proposed > Config().float_max


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
