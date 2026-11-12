"""MinerU 本地容器检查与解析/重启共享锁。"""
import fcntl
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import requests

from backend.config import CONFIG


def probe_container_gpu(container: str, timeout: int = 10) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            ["docker", "exec", container, "nvidia-smi", "-L"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    output = "\n".join(
        part.strip() for part in (result.stdout, result.stderr) if part and part.strip()
    )
    first_line = output.splitlines()[0] if output else "nvidia-smi returned no output"
    if result.returncode != 0:
        return False, first_line or f"nvidia-smi exited {result.returncode}"
    if not output:
        return False, first_line
    return True, first_line


def probe_container_running(container: str, timeout: int = 10) -> tuple[bool | None, str]:
    try:
        result = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Running}}", container],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return None, str(exc)
    output = (result.stdout or result.stderr or "").strip()
    if result.returncode != 0:
        return None, output.splitlines()[0] if output else f"docker inspect exited {result.returncode}"
    if output == "true":
        return True, output
    if output == "false":
        return False, output
    return None, output or "docker inspect returned no state"


def probe_http(api_base: str, timeout: int = 5) -> dict:
    url = f"{api_base.rstrip('/')}/health"
    try:
        response = requests.get(url, timeout=timeout)
    except requests.RequestException as exc:
        return {"ok": False, "error": str(exc), "status_code": None}
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if response.status_code != 200:
        return {
            "ok": False,
            "error": f"HTTP {response.status_code}",
            "status_code": response.status_code,
        }
    if not isinstance(payload, dict):
        return {
            "ok": False,
            "error": "invalid JSON response",
            "status_code": response.status_code,
        }
    result = dict(payload)
    result["status_code"] = response.status_code
    result["ok"] = payload.get("status") == "healthy"
    if not result["ok"]:
        result["error"] = f"unexpected health status: {payload.get('status')!r}"
    return result


@contextmanager
def mineru_file_lock(*, exclusive: bool, blocking: bool = True) -> Iterator[bool]:
    lock_path = Path(CONFIG["paths"]["cache_dir"]) / "mineru-local.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock_file:
        flags = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        if not blocking:
            flags |= fcntl.LOCK_NB
        acquired = False
        try:
            try:
                fcntl.flock(lock_file.fileno(), flags)
            except BlockingIOError:
                yield False
                return
            acquired = True
            yield True
        finally:
            if acquired:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
