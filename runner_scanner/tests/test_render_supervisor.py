from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from runner_scanner import render_supervisor as supervisor
from services.kronos_inference.config import DEFAULT_KRONOS_SOURCE_REVISION
from services.kronos_inference.runtime_layout import runtime_layout


class FakeProcess:
    def __init__(self, pid: int, events: list[str]) -> None:
        self.pid = pid
        self.returncode = None
        self.events = events

    def poll(self):
        return self.returncode

    def terminate(self):
        self.events.append(f"terminate:{self.pid}")
        self.returncode = 0

    def kill(self):
        self.events.append(f"kill:{self.pid}")
        self.returncode = -9

    def wait(self, timeout=None):
        self.events.append(f"wait:{self.pid}")
        return int(self.returncode or 0)


class StepEvent:
    def __init__(self, clock, stop_after: int) -> None:
        self.clock = clock
        self.stop_after = stop_after
        self.wait_calls = 0

    def is_set(self):
        return False

    def wait(self, timeout):
        self.clock.value += timeout
        self.wait_calls += 1
        return self.wait_calls >= self.stop_after


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def now(self):
        return self.value


def test_child_environments_force_loopback_offline_and_scrub_secrets(tmp_path):
    layout = runtime_layout(tmp_path)
    scanner, service, endpoint = supervisor.child_environments(
        {
            "PATH": "/bin",
            "MASSIVE_API_KEY": "massive-secret",
            "TELEGRAM_BOT_TOKEN": "telegram-secret",
            "ANTHROPIC_API_KEY": "anthropic-secret",
            "KRONOS_API_TOKEN": "obsolete-secret",
            "KRONOS_SERVICE_URL": "https://old.example",
            "KRONOS_SERVICE_TOKEN": "old-token",
            "KEEPALIVE_PORT": "10000",
            "KRONOS_LOCAL_PORT": "18080",
        },
        layout,
    )

    assert endpoint == "http://127.0.0.1:18080"
    assert scanner["KRONOS_SERVICE_URL"] == endpoint
    assert "KRONOS_SERVICE_TOKEN" not in scanner
    assert service["KRONOS_HOST"] == "127.0.0.1"
    assert service["HF_HUB_OFFLINE"] == "1"
    assert service["KRONOS_REPO_PATH"] == str(layout.kronos_repo_path)
    assert service["OMP_NUM_THREADS"] == "1"
    for secret_name in (
        "MASSIVE_API_KEY",
        "TELEGRAM_BOT_TOKEN",
        "ANTHROPIC_API_KEY",
        "KRONOS_API_TOKEN",
    ):
        assert secret_name not in service


def test_local_port_cannot_collide_with_keepalive(tmp_path):
    with pytest.raises(ValueError, match="يتعارض"):
        supervisor.child_environments(
            {"KEEPALIVE_PORT": "10000", "KRONOS_LOCAL_PORT": "10000"},
            runtime_layout(tmp_path),
        )


def test_validate_artifact_requires_exact_pins(tmp_path):
    layout = runtime_layout(tmp_path)
    (layout.kronos_repo_path / "model").mkdir(parents=True)
    (layout.kronos_repo_path / "model" / "kronos.py").write_text("", encoding="utf-8")
    layout.huggingface_home.mkdir(parents=True)
    layout.manifest_path.write_text(
        json.dumps(
            {
                "schema": 1,
                "kronos_revision": DEFAULT_KRONOS_SOURCE_REVISION,
                "model": "wrong",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="المراجعات المثبتة"):
        supervisor.validate_artifact(layout)


def test_shutdown_keeps_kronos_alive_until_scanner_stops():
    events: list[str] = []
    scanner = FakeProcess(11, events)
    kronos = FakeProcess(22, events)
    supervisor.shutdown_children(scanner, kronos, scanner_timeout=5.0)
    assert events == ["terminate:11", "wait:11", "terminate:22", "wait:22"]


def test_terminate_escalates_only_after_timeout():
    events: list[str] = []

    class SlowProcess(FakeProcess):
        def wait(self, timeout=None):
            events.append(f"wait:{self.pid}:{timeout}")
            if self.returncode == 0:
                raise subprocess.TimeoutExpired("child", timeout)
            return int(self.returncode or 0)

        def terminate(self):
            events.append(f"terminate:{self.pid}")
            self.returncode = 0

        def kill(self):
            events.append(f"kill:{self.pid}")
            self.returncode = -9

    process = SlowProcess(33, events)
    supervisor.terminate_process(process, "slow", 2.0)
    assert events == ["terminate:33", "wait:33:2.0", "kill:33", "wait:33:5"]


def test_invalid_shadow_artifact_still_starts_scanner(monkeypatch, tmp_path):
    layout = runtime_layout(tmp_path)
    monkeypatch.setattr(supervisor, "runtime_layout", lambda: layout)
    spawned: list[tuple[tuple[str, ...], dict[str, str]]] = []

    def factory(command, _cwd, environment):
        spawned.append((tuple(command), dict(environment)))
        process = FakeProcess(44, [])
        process.returncode = 0
        return process

    result = supervisor.run(
        {"KRONOS_SHADOW_ENABLED": "true", "KEEPALIVE_PORT": "10000"},
        process_factory=factory,
    )

    assert result == 1
    assert len(spawned) == 1
    assert spawned[0][0][-1] == "runner_scanner.main"
    assert spawned[0][1]["KRONOS_SHADOW_ENABLED"] == "false"


def test_kronos_spawn_error_does_not_block_scanner(monkeypatch, tmp_path):
    monkeypatch.setattr(supervisor, "runtime_layout", lambda: runtime_layout(tmp_path))
    monkeypatch.setattr(supervisor, "validate_artifact", lambda _layout: None)
    spawned: list[str] = []

    def factory(command, _cwd, _environment):
        module = command[-1]
        spawned.append(module)
        if module == "services.kronos_inference":
            raise OSError("simulated ENOMEM")
        process = FakeProcess(55, [])
        process.returncode = 0
        return process

    result = supervisor.run(
        {"KRONOS_SHADOW_ENABLED": "true", "KEEPALIVE_PORT": "10000"},
        process_factory=factory,
        readiness_probe=lambda *_args: pytest.fail("readiness must not block scanner"),
    )

    assert result == 1
    assert spawned == ["services.kronos_inference", "runner_scanner.main"]


def test_health_parser_exposes_inference_age(monkeypatch):
    monkeypatch.setattr(
        supervisor,
        "_get_json",
        lambda *_args: {
            "status": "ok",
            "kronos_revision": DEFAULT_KRONOS_SOURCE_REVISION,
            "forecast_in_progress": True,
            "forecast_age_seconds": 151.25,
        },
    )
    health = supervisor._health("http://127.0.0.1:18080", 1.0)
    assert health == supervisor.KronosHealth(True, 151.25)


def test_hung_inference_restarts_kronos_without_restarting_scanner(
    monkeypatch, tmp_path
):
    clock = FakeClock()
    stop = StepEvent(clock, stop_after=20)
    monkeypatch.setattr(supervisor, "monotonic", clock.now)
    monkeypatch.setattr(supervisor, "runtime_layout", lambda: runtime_layout(tmp_path))
    monkeypatch.setattr(supervisor, "validate_artifact", lambda _layout: None)
    events: list[str] = []
    service_processes: list[FakeProcess] = []
    scanner_processes: list[FakeProcess] = []

    def factory(command, _cwd, _environment):
        if command[-1] == "services.kronos_inference":
            process = FakeProcess(100 + len(service_processes), events)
            service_processes.append(process)
            return process
        process = FakeProcess(200, events)
        scanner_processes.append(process)
        return process

    health_calls = 0

    def health_probe(_endpoint, _timeout):
        nonlocal health_calls
        health_calls += 1
        if health_calls == 1:
            return supervisor.KronosHealth(True, 31.0)
        return supervisor.KronosHealth(False, None)

    result = supervisor.run(
        {
            "KRONOS_SHADOW_ENABLED": "true",
            "KEEPALIVE_PORT": "10000",
            "KRONOS_HEALTH_INTERVAL_SEC": "2",
            "KRONOS_HANG_TIMEOUT_SEC": "30",
            "KRONOS_RESTART_DELAY_SEC": "1",
            "KRONOS_SCANNER_SHUTDOWN_TIMEOUT_SEC": "2",
        },
        process_factory=factory,
        readiness_probe=lambda *_args: True,
        health_probe=health_probe,
        stop_event=stop,
    )

    assert result == 0
    assert len(scanner_processes) == 1
    assert len(service_processes) >= 2
    assert "terminate:100" in events
