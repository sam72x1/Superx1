"""اختبارات رادار التخفيف (SEC) — بجلسة HTTP وهمية (بلا شبكة)."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from runner_scanner.config import Config
from runner_scanner.models import DilutionResult, Session
from runner_scanner.pipeline import process_candidate
from runner_scanner.sec_radar import SecRadar
from runner_scanner.tests.fixtures import FakeClient, make_snapshot

ET = ZoneInfo("America/New_York")
ET_NOW = datetime(2026, 6, 26, 10, 30, tzinfo=ET)
TODAY = date(2026, 6, 26)

_CIK_MAP = {"0": {"cik_str": 11111, "ticker": "DILUT", "title": "Dilut Inc"}}
_CIK10 = "0000011111"


class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class _FakeSession:
    def __init__(self, submissions, cik_map=None):
        self.headers = {}
        self._cik = cik_map if cik_map is not None else _CIK_MAP
        self._subs = submissions

    def get(self, url, timeout=12):
        if "company_tickers" in url:
            return _Resp(200, self._cik)
        if _CIK10 in url:
            return _Resp(200, self._subs)
        return _Resp(404, None)


def _radar(forms, dates, enabled=True):
    cfg = Config(dilution_radar_enabled=enabled)
    subs = {"filings": {"recent": {"form": forms, "filingDate": dates}}}
    return SecRadar(cfg, session=_FakeSession(subs))


def test_active_offering_high_risk():
    r = _radar(["424B5", "8-K"], ["2026-06-10", "2026-06-01"])
    res = r.check("DILUT", today=TODAY)
    assert res is not None and res.risk == "مرتفع"
    assert res.is_active is True
    assert res.latest_form == "424B5"


def test_shelf_only_medium_risk():
    r = _radar(["S-3", "10-Q"], ["2026-03-01", "2026-05-01"])
    res = r.check("DILUT", today=TODAY)
    assert res is not None and res.risk == "متوسط"
    assert res.is_active is True


def test_no_dilution_forms_returns_clean():
    r = _radar(["10-Q", "8-K"], ["2026-06-10", "2026-06-01"])
    res = r.check("DILUT", today=TODAY)
    assert res is not None and res.risk == "لا"
    assert res.is_active is False


def test_old_filings_outside_window_ignored():
    # 424B5 قديم جدًا (خارج نافذة 45 يوم) → لا يُعدّ طرحًا فعّالًا
    r = _radar(["424B5"], ["2026-01-01"])
    res = r.check("DILUT", today=TODAY)
    assert res is not None and res.risk == "لا"


def test_unknown_ticker_returns_none():
    r = _radar(["424B5"], ["2026-06-10"])
    assert r.check("NOPE", today=TODAY) is None


def test_disabled_returns_none():
    r = _radar(["424B5"], ["2026-06-10"], enabled=False)
    assert r.check("DILUT", today=TODAY) is None


class _FakeRadar:
    def __init__(self, result):
        self._r = result

    def check(self, ticker, today=None):
        return self._r


def test_pipeline_active_dilution_penalizes():
    cfg = Config(dilution_penalty=90.0)   # خصم كبير يُسقط تحت العتبة
    radar = _FakeRadar(DilutionResult(risk="مرتفع", latest_form="424B5",
                                      note="طرح فعّال"))
    cand = process_candidate(
        cfg, FakeClient(), make_snapshot(change_pct=25.0),
        halts=None, session=Session.REGULAR, et_now=ET_NOW, sec_radar=radar)
    assert cand.is_rejected is True
    assert "تخفيف" in (cand.rejected_reason or "")


def test_pipeline_no_dilution_keeps_alert():
    cfg = Config()
    radar = _FakeRadar(DilutionResult(risk="لا"))
    cand = process_candidate(
        cfg, FakeClient(), make_snapshot(change_pct=25.0),
        halts=None, session=Session.REGULAR, et_now=ET_NOW, sec_radar=radar)
    assert cand.is_rejected is False
    assert cand.dilution is not None and cand.dilution.is_active is False
