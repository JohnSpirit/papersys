import subprocess
from types import SimpleNamespace

from backend.services import mineru_health


def test_probe_container_gpu_success(monkeypatch):
    monkeypatch.setattr(
        mineru_health.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="GPU 0: RTX 4090\n", stderr=""),
    )
    assert mineru_health.probe_container_gpu("mineru-api-gpu") == (True, "GPU 0: RTX 4090")


def test_probe_container_gpu_failure(monkeypatch):
    monkeypatch.setattr(
        mineru_health.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=255,
            stdout="",
            stderr="Failed to initialize NVML: Unknown Error\n",
        ),
    )
    ok, detail = mineru_health.probe_container_gpu("mineru-api-gpu")
    assert not ok
    assert "NVML" in detail


def test_probe_container_gpu_timeout(monkeypatch):
    def raise_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="docker", timeout=10)

    monkeypatch.setattr(mineru_health.subprocess, "run", raise_timeout)
    ok, detail = mineru_health.probe_container_gpu("mineru-api-gpu")
    assert not ok
    assert "timed out" in detail


def test_probe_container_gpu_missing_docker(monkeypatch):
    monkeypatch.setattr(
        mineru_health.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError("docker")),
    )
    ok, detail = mineru_health.probe_container_gpu("mineru-api-gpu")
    assert not ok
    assert "docker" in detail


def test_probe_container_running(monkeypatch):
    monkeypatch.setattr(
        mineru_health.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="false\n", stderr=""),
    )
    assert mineru_health.probe_container_running("mineru-api-gpu") == (False, "false")


def test_probe_container_running_inspect_error(monkeypatch):
    monkeypatch.setattr(
        mineru_health.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout="", stderr="No such object"),
    )
    running, detail = mineru_health.probe_container_running("missing")
    assert running is None
    assert "No such object" in detail


def test_probe_http_success(monkeypatch):
    response = SimpleNamespace(
        status_code=200,
        json=lambda: {"status": "healthy", "processing_tasks": 0},
    )
    monkeypatch.setattr(mineru_health.requests, "get", lambda *args, **kwargs: response)
    result = mineru_health.probe_http("http://127.0.0.1:8001/")
    assert result["ok"]
    assert result["processing_tasks"] == 0


def test_probe_http_non_200(monkeypatch):
    response = SimpleNamespace(status_code=503, json=lambda: {"status": "unavailable"})
    monkeypatch.setattr(mineru_health.requests, "get", lambda *args, **kwargs: response)
    result = mineru_health.probe_http("http://127.0.0.1:8001")
    assert not result["ok"]
    assert result["error"] == "HTTP 503"


def test_probe_http_invalid_json(monkeypatch):
    def invalid_json():
        raise ValueError("not json")

    response = SimpleNamespace(status_code=200, json=invalid_json)
    monkeypatch.setattr(mineru_health.requests, "get", lambda *args, **kwargs: response)
    result = mineru_health.probe_http("http://127.0.0.1:8001")
    assert not result["ok"]
    assert result["error"] == "invalid JSON response"


def test_mineru_file_lock_shared_and_exclusive(tmp_path, monkeypatch):
    monkeypatch.setitem(mineru_health.CONFIG["paths"], "cache_dir", str(tmp_path))
    with mineru_health.mineru_file_lock(exclusive=False) as acquired:
        assert acquired
        with mineru_health.mineru_file_lock(exclusive=False, blocking=False) as shared:
            assert shared
        with mineru_health.mineru_file_lock(exclusive=True, blocking=False) as exclusive:
            assert not exclusive
    with mineru_health.mineru_file_lock(exclusive=True, blocking=False) as exclusive:
        assert exclusive


def test_doctor_mineru_reports_gpu_and_http(monkeypatch, capsys):
    from backend import cli

    monkeypatch.setitem(mineru_health.CONFIG["mineru"], "mode", "local")
    monkeypatch.setitem(mineru_health.CONFIG["mineru"], "local_container", "mineru-api-gpu")
    monkeypatch.setitem(
        mineru_health.CONFIG["mineru"], "local_api_base", "http://127.0.0.1:8001"
    )
    monkeypatch.setattr(
        mineru_health, "probe_container_gpu", lambda container: (True, "GPU 0: RTX 4090")
    )
    monkeypatch.setattr(
        mineru_health,
        "probe_http",
        lambda api_base: {"ok": True, "status": "healthy", "processing_tasks": 0},
    )
    cli._doctor_mineru()
    output = capsys.readouterr().out
    assert "GPU 0: RTX 4090" in output
    assert "healthy" in output
    assert "mineru-api-gpu" in output
