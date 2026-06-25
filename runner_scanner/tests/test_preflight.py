"""اختبار فحص الجاهزية (الجزء غير الشبكي)."""

from __future__ import annotations

from runner_scanner.config import Config
from runner_scanner import preflight


def test_check_env_fails_when_keys_missing():
    cfg = Config(massive_api_key="", telegram_bot_token="",
                 telegram_chat_id="")
    assert preflight.check_env(cfg) is False


def test_check_env_passes_when_keys_present():
    cfg = Config(massive_api_key="k", telegram_bot_token="t",
                 telegram_chat_id="c")
    assert preflight.check_env(cfg) is True
