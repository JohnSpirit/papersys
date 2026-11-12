import json
from contextlib import contextmanager

import pytest

from scripts import mineru_guard


@pytest.fixture
def guard_env(tmp_path, monkeypatch):
    monkeypatch.setitem(mineru_guard.CONFIG["mineru"], "mode", "local")
    monkeypatch.setitem(
        mineru_guard.CONFIG["mineru"], "local_container", "mineru-api-gpu"
    )
    monkeypatch.setitem(
        mineru_guard.CONFIG["mineru"], "local_api_base", "http://127.0.0.1:8001"
    )
    monkeypatch.setitem(mineru_guard.CONFIG["paths"], "cache_dir", str(tmp_path / "cache"))
    monkeypatch.setitem(mineru_guard.CONFIG["paths"], "log_dir", str(tmp_path / "logs"))
    monkeypatch.setattr(mineru_guard.time, "time", lambda: 10000.0)
    events = []
    monkeypatch.setattr(
        mineru_guard.tasks_repo,
        "emit_event",
        lambda task_id, msg, level="info", paper_id=None: events.append((level, msg)),
    )
    return tmp_path, events


def test_guard_healthy_does_not_restart(guard_env, monkeypatch):
    monkeypatch.setattr(mineru_guard, "probe_container_gpu", lambda container: (True, "GPU 0"))
    monkeypatch.setattr(
        mineru_guard,
        "_restart_container",
        lambda container: pytest.fail("healthy container must not restart"),
    )
    assert mineru_guard.run_guard() == "healthy"


def test_guard_restarts_stopped_container(guard_env, monkeypatch):
    _tmp_path, events = guard_env
    probes = iter([(False, "NVML failure"), (False, "NVML failure")])
    monkeypatch.setattr(mineru_guard, "probe_container_gpu", lambda container: next(probes))
    monkeypatch.setattr(mineru_guard, "probe_container_running", lambda container: (False, "false"))
    monkeypatch.setattr(
        mineru_guard,
        "probe_http",
        lambda api_base: pytest.fail("stopped container does not require HTTP health"),
    )
    restarts = []
    monkeypatch.setattr(
        mineru_guard,
        "_restart_container",
        lambda container: restarts.append(container) or (True, container),
    )
    monkeypatch.setattr(mineru_guard, "_wait_for_recovery", lambda *args: (True, "GPU 0"))
    assert mineru_guard.run_guard() == "healthy"
    assert restarts == ["mineru-api-gpu"]
    assert any(level == "warn" and "Restarting" in message for level, message in events)
    state = json.loads((guard_env[0] / "cache" / "mineru_guard.json").read_text())
    assert state["last_restart_attempt"] == 10000.0


def test_guard_running_container_with_unreadable_health_does_not_restart(guard_env, monkeypatch):
    monkeypatch.setattr(mineru_guard, "probe_container_gpu", lambda container: (False, "NVML"))
    monkeypatch.setattr(mineru_guard, "probe_container_running", lambda container: (True, "true"))
    monkeypatch.setattr(mineru_guard, "probe_http", lambda api_base: {"ok": False, "error": "refused"})
    monkeypatch.setattr(
        mineru_guard,
        "_restart_container",
        lambda container: pytest.fail("unreadable health must not restart"),
    )
    assert mineru_guard.run_guard() == "restart_failed_health"


@pytest.mark.parametrize("health", [
    {"ok": True, "queued_tasks": 1, "processing_tasks": 0},
    {"ok": True, "queued_tasks": 0, "processing_tasks": 1},
])
def test_guard_busy_queue_does_not_restart(guard_env, monkeypatch, health):
    monkeypatch.setattr(mineru_guard, "probe_container_gpu", lambda container: (False, "NVML"))
    monkeypatch.setattr(mineru_guard, "probe_container_running", lambda container: (True, "true"))
    monkeypatch.setattr(mineru_guard, "probe_http", lambda api_base: health)
    monkeypatch.setattr(
        mineru_guard,
        "_restart_container",
        lambda container: pytest.fail("busy queue must not restart"),
    )
    assert mineru_guard.run_guard() == "restart_deferred_busy"


def test_guard_lock_contention_does_not_restart(guard_env, monkeypatch):
    monkeypatch.setattr(mineru_guard, "probe_container_gpu", lambda container: (False, "NVML"))

    @contextmanager
    def unavailable_lock(**kwargs):
        yield False

    monkeypatch.setattr(mineru_guard, "mineru_file_lock", unavailable_lock)
    monkeypatch.setattr(
        mineru_guard,
        "_restart_container",
        lambda container: pytest.fail("lock contention must not restart"),
    )
    assert mineru_guard.run_guard() == "restart_deferred_lock"


def test_guard_failed_restart_uses_attempt_cooldown(guard_env, monkeypatch):
    monkeypatch.setattr(mineru_guard, "probe_container_gpu", lambda container: (False, "NVML"))
    monkeypatch.setattr(mineru_guard, "probe_container_running", lambda container: (False, "false"))
    restarts = []
    monkeypatch.setattr(
        mineru_guard,
        "_restart_container",
        lambda container: restarts.append(container) or (False, "restart failed"),
    )
    assert mineru_guard.run_guard() == "restart_failed"
    assert mineru_guard.run_guard() == "restart_cooldown"
    assert len(restarts) == 1


def test_guard_inspect_failure_does_not_restart(guard_env, monkeypatch):
    monkeypatch.setattr(mineru_guard, "probe_container_gpu", lambda container: (False, "NVML"))
    monkeypatch.setattr(mineru_guard, "probe_container_running", lambda container: (None, "missing"))
    monkeypatch.setattr(
        mineru_guard,
        "_restart_container",
        lambda container: pytest.fail("inspect failure must not restart"),
    )
    assert mineru_guard.run_guard() == "restart_failed_inspect"


def test_wait_for_recovery_requires_http_after_gpu(guard_env, monkeypatch):
    responses = iter([
        {"ok": False, "error": "starting"},
        {"ok": True, "status": "healthy"},
    ])
    monkeypatch.setattr(mineru_guard, "probe_container_gpu", lambda container: (True, "GPU 0"))
    monkeypatch.setattr(mineru_guard, "probe_http", lambda api_base: next(responses))
    monkeypatch.setattr(mineru_guard.time, "monotonic", lambda: 0)
    monkeypatch.setattr(mineru_guard.time, "sleep", lambda seconds: None)
    assert mineru_guard._wait_for_recovery("mineru-api-gpu", "http://127.0.0.1:8001") == (
        True,
        "GPU 0",
    )
