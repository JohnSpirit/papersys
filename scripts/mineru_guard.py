"""定时检查 MinerU 容器 GPU，并在安全条件下重启容器。"""
import json
import subprocess
import sys
import time
from pathlib import Path

from backend.config import CONFIG
from backend.db import tasks_repo
from backend.services.mineru_health import (
    mineru_file_lock,
    probe_container_gpu,
    probe_container_running,
    probe_http,
)

_RESTART_COOLDOWN_SECONDS = 1800
_RESTART_TIMEOUT_SECONDS = 60


def _state_path() -> Path:
    return Path(CONFIG["paths"]["cache_dir"]) / "mineru_guard.json"


def _log_path() -> Path:
    return Path(CONFIG["paths"]["log_dir"]) / "mineru_guard.log"


def _load_state() -> dict:
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(state: dict) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
    temporary.replace(path)


def _append_log(message: str) -> None:
    path = _log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    with path.open("a", encoding="utf-8") as log_file:
        log_file.write(f"[{timestamp}] {message}\n")


def _record(state: dict, status: str, message: str, level: str, *, force: bool = False) -> str:
    if state.get("last_status") == status and not force:
        return status
    _append_log(f"{status}: {message}")
    if force or state.get("last_status") != status:
        try:
            tasks_repo.emit_event(None, message, level=level, paper_id=None)
        except Exception as exc:  # noqa: BLE001
            _append_log(f"event failed: {exc}")
    state["last_status"] = status
    _save_state(state)
    return status


def _mark_healthy(state: dict, detail: str) -> str:
    previous = state.get("last_status")
    if previous in (None, "healthy"):
        state["last_status"] = "healthy"
        _save_state(state)
        return "healthy"
    return _record(state, "healthy", f"MinerU GPU recovered: {detail}", "info", force=True)


def _restart_container(container: str) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            ["docker", "restart", container],
            capture_output=True,
            text=True,
            timeout=_RESTART_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    output = "\n".join(
        part.strip() for part in (result.stdout, result.stderr) if part and part.strip()
    )
    if result.returncode != 0:
        return False, output or f"docker restart exited {result.returncode}"
    return True, output or container


def _wait_for_recovery(container: str, api_base: str) -> tuple[bool, str]:
    deadline = time.monotonic() + _RESTART_TIMEOUT_SECONDS
    detail = "recovery check did not run"
    while True:
        gpu_ok, gpu_detail = probe_container_gpu(container)
        http = probe_http(api_base) if gpu_ok else {"ok": False}
        if gpu_ok and http.get("ok"):
            return True, gpu_detail
        detail = f"gpu={gpu_detail}; http={http.get('error', http.get('status', 'unavailable'))}"
        if time.monotonic() >= deadline:
            return False, detail
        time.sleep(2)


def run_guard() -> str:
    cfg = CONFIG["mineru"]
    container = cfg.get("local_container", "")
    if cfg.get("mode", "cloud") != "local" or not container:
        return "disabled"
    api_base = cfg.get("local_api_base", "http://127.0.0.1:8000")
    state = _load_state()
    gpu_ok, detail = probe_container_gpu(container)
    if gpu_ok:
        return _mark_healthy(state, detail)

    with mineru_file_lock(exclusive=True, blocking=False) as acquired:
        if not acquired:
            return _record(
                state,
                "restart_deferred_lock",
                f"MinerU GPU unavailable ({detail}); local parse holds the lock",
                "warn",
            )
        gpu_ok, detail = probe_container_gpu(container)
        if gpu_ok:
            return _mark_healthy(state, detail)

        running, inspect_detail = probe_container_running(container)
        if running is None:
            return _record(
                state,
                "restart_failed_inspect",
                f"Cannot inspect {container}; restart skipped: {inspect_detail}",
                "error",
            )
        if running:
            http = probe_http(api_base)
            if not http.get("ok"):
                return _record(
                    state,
                    "restart_failed_health",
                    f"MinerU GPU unavailable but /health is unreadable; "
                    f"restart skipped: {http.get('error', 'unknown error')}",
                    "error",
                )
            try:
                queued = int(http.get("queued_tasks") or 0)
                processing = int(http.get("processing_tasks") or 0)
            except (TypeError, ValueError):
                return _record(
                    state,
                    "restart_failed_health",
                    "MinerU /health returned invalid queue counts; restart skipped",
                    "error",
                )
            if queued > 0 or processing > 0:
                return _record(
                    state,
                    "restart_deferred_busy",
                    f"MinerU has queued={queued} processing={processing}; restart skipped",
                    "warn",
                )

        now = time.time()
        try:
            last_attempt = float(state.get("last_restart_attempt") or 0)
        except (TypeError, ValueError):
            last_attempt = 0
        if now - last_attempt < _RESTART_COOLDOWN_SECONDS:
            return _record(
                state,
                "restart_cooldown",
                f"Restart skipped until {int(last_attempt + _RESTART_COOLDOWN_SECONDS)}",
                "warn",
            )

        state["last_restart_attempt"] = now
        _record(
            state,
            "restart_attempt",
            f"Restarting {container} after GPU failure: {detail}",
            "warn",
            force=True,
        )
        restarted, restart_detail = _restart_container(container)
        if not restarted:
            return _record(
                state,
                "restart_failed",
                f"docker restart failed for {container}: {restart_detail}",
                "error",
                force=True,
            )
        recovered, recovery_detail = _wait_for_recovery(container, api_base)
        if not recovered:
            return _record(
                state,
                "restart_failed",
                f"{container} did not recover GPU and HTTP health: {recovery_detail}",
                "error",
                force=True,
            )
        return _mark_healthy(state, recovery_detail)


def main() -> int:
    try:
        status = run_guard()
        print(status)
        return 0
    except Exception as exc:  # noqa: BLE001
        try:
            _record(_load_state(), "guard_error", f"MinerU guard failed: {exc}", "error", force=True)
        except Exception:  # noqa: BLE001,S110
            pass
        print(f"MinerU guard failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
