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


def test_raises_float_max_on_missed_opportunities():
    alerts = [_row(result="win", rvol=12.0) for _ in range(4)]
    missed = [{"reject_reason": "فلوت كبير", "max_gain_pct": 50.0,
               "ticker": f"M{i}"} for i in range(3)]
    props = propose_calibrations(_FakeStore(alerts, missed), Config())
    fl = [p for p in props if p.env == "FLOAT_MAX"]
    assert fl and fl[0].proposed > Config().float_max


def test_format_proposals_renders_numbers():
    alerts = ([_row(result="loss", rvol=6.0) for _ in range(8)]
              + [_row(result="win", rvol=12.0) for _ in range(8)])
    text = format_proposals(propose_calibrations(_FakeStore(alerts), Config()))
    assert "RVOL_MIN" in text and "→" in text
