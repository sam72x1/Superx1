"""اختبارات تحميل إعدادات التطوير المحلي من ملف .env."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from dotenv import load_dotenv

from runner_scanner import config as config_module
from runner_scanner.config import Config


def _redirect_dotenv(monkeypatch, dotenv_path: Path) -> list[bool]:
    """يوجّه تحميل Config إلى ملف مؤقت ويسجّل سياسة التجاوز المستخدمة."""
    overrides: list[bool] = []
    monkeypatch.delenv("PYTHON_DOTENV_DISABLED", raising=False)

    def _load_test_dotenv(*, override: bool) -> bool:
        overrides.append(override)
        return load_dotenv(dotenv_path=dotenv_path, override=override)

    monkeypatch.setattr(config_module, "load_dotenv", _load_test_dotenv)
    return overrides


def test_from_env_loads_value_from_dotenv(monkeypatch, tmp_path):
    """نسخ .env يكفي كي يقرأ Config قيم التشغيل المحلي منه."""
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text("MASSIVE_API_KEY=from-dotenv\n", encoding="utf-8")
    monkeypatch.delenv("MASSIVE_API_KEY", raising=False)
    overrides = _redirect_dotenv(monkeypatch, dotenv_path)

    try:
        cfg = Config.from_env()
        assert cfg.massive_api_key == "from-dotenv"
        assert overrides == [False]
    finally:
        # python-dotenv يعدّل os.environ مباشرة، فنمنع تسرّب القيمة لاختبار آخر.
        os.environ.pop("MASSIVE_API_KEY", None)


def test_from_env_keeps_existing_environment_value(monkeypatch, tmp_path):
    """قيمة الصدفة/Render تبقى مقدّمة على القيمة المكتوبة في .env."""
    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text("MASSIVE_API_KEY=from-dotenv\n", encoding="utf-8")
    monkeypatch.setenv("MASSIVE_API_KEY", "from-environment")
    overrides = _redirect_dotenv(monkeypatch, dotenv_path)

    cfg = Config.from_env()

    assert cfg.massive_api_key == "from-environment"
    assert overrides == [False]


def test_kronos_shadow_config_is_explicit_and_disabled_by_default(monkeypatch):
    cfg = Config.from_env()
    assert cfg.kronos_shadow_enabled is False
    assert cfg.kronos_service_url == ""
    assert cfg.kronos_horizons == (6, 12, 18)
    assert cfg.kronos_max_context_lag_sec == 360.0

    monkeypatch.setenv("KRONOS_SHADOW_ENABLED", "true")
    monkeypatch.setenv("KRONOS_SERVICE_URL", "https://kronos.example/")
    monkeypatch.setenv("KRONOS_HORIZONS", "12,6,-1,6.5,18")
    monkeypatch.setenv("KRONOS_QUEUE_SIZE", "7")
    monkeypatch.setenv("KRONOS_MAX_CONTEXT_LAG_SEC", "420")
    cfg = Config.from_env()

    assert cfg.kronos_shadow_enabled is True
    assert cfg.kronos_service_url == "https://kronos.example/"
    assert cfg.kronos_horizons == (6, 12, 18)
    assert cfg.kronos_queue_size == 7
    assert cfg.kronos_max_context_lag_sec == 420.0


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("KRONOS_OBSERVATION_GRACE_MIN", "-1"),
        ("KRONOS_OBSERVATION_GRACE_MIN", "5"),
        ("KRONOS_OBSERVATION_GRACE_MIN", "nan"),
        ("KRONOS_MAX_CONTEXT_LAG_SEC", "-1"),
        ("KRONOS_MAX_CONTEXT_LAG_SEC", "inf"),
    ],
)
def test_kronos_time_policy_rejects_unsafe_values(monkeypatch, name, value):
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError):
        Config.from_env()
