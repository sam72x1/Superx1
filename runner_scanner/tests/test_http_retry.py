"""اختبار إعادة المحاولة في طبقة النقل (_get) — عابر يُعاد، دائم يُرفع فورًا."""

from __future__ import annotations

import pytest
import requests

from runner_scanner import massive_client
from runner_scanner.config import Config
from runner_scanner.massive_client import MassiveClient, MassiveError


class _Resp:
    def __init__(self, status, json_data=None, headers=None, text=""):
        self.status_code = status
        self._j = json_data
        self.headers = headers or {}
        self.text = text

    def json(self):
        if self._j is None:
            raise ValueError("no json")
        return self._j


class _Session:
    """جلسة وهمية: كل نداء يأخذ السلوك التالي (استثناء أو رد)."""

    def __init__(self, behaviors):
        self._b = behaviors
        self.calls = 0
        self.headers = {}

    def get(self, url, params=None, timeout=None):
        b = self._b[min(self.calls, len(self._b) - 1)]
        self.calls += 1
        if isinstance(b, Exception):
            raise b
        return b


def _client(behaviors, retries=3):
    cfg = Config(massive_api_key="x", http_max_retries=retries)
    return MassiveClient(cfg, session=_Session(behaviors))


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(massive_client.time, "sleep", lambda s: None)


def test_retries_on_timeout_then_succeeds():
    c = _client([requests.exceptions.ReadTimeout("t"),
                 requests.exceptions.ReadTimeout("t"),
                 _Resp(200, {"ok": 1})])
    assert c._get("/x") == {"ok": 1}
    assert c._http.calls == 3                  # حاول 3 مرات حتى نجح


def test_retries_on_429_then_succeeds():
    c = _client([_Resp(429, headers={"Retry-After": "0"}),
                 _Resp(200, {"ok": 1})])
    assert c._get("/x") == {"ok": 1}
    assert c._http.calls == 2


def test_retries_on_500_then_succeeds():
    c = _client([_Resp(503, text="busy"), _Resp(200, {"ok": 1})])
    assert c._get("/x") == {"ok": 1}


def test_401_raises_immediately_no_retry():
    c = _client([_Resp(401), _Resp(200, {"ok": 1})])
    with pytest.raises(MassiveError):
        c._get("/x")
    assert c._http.calls == 1                  # لا إعادة على خطأ دائم


def test_gives_up_after_max_retries():
    c = _client([requests.exceptions.ConnectionError("down")], retries=2)
    with pytest.raises(MassiveError):
        c._get("/x")
    assert c._http.calls == 3                  # محاولة أصلية + 2 إعادة
