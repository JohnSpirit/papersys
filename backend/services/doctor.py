"""doctor 使用的运行状态检查：任务队列、恢复记录和 MCP 进程。"""
import os
import time
from pathlib import Path

from backend.db import tasks_repo
from backend.db.conn import get_conn
from backend.version import get_version


def task_queue_stats() -> dict:
    conn = get_conn()
    try:
        counts = dict(conn.execute(
            "SELECT status, COUNT(*) FROM tasks GROUP BY status"
        ).fetchall())
        oldest = conn.execute(
            "SELECT id, kind, started_at, created_at FROM tasks"
            " WHERE status = 'running'"
            " ORDER BY COALESCE(started_at, created_at) ASC LIMIT 1"
        ).fetchone()
        return {
            "counts": counts,
            "oldest_running": dict(oldest) if oldest else None,
            "last_recovery": tasks_repo.last_recovery_stats(),
        }
    finally:
        conn.close()


def _process_start_epoch(pid: int) -> float | None:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().split()
        ticks = os.sysconf("SC_CLK_TCK")
        boot = next(
            int(line.split()[1]) for line in Path("/proc/stat").read_text().splitlines()
            if line.startswith("btime ")
        )
        return boot + int(fields[21]) / ticks
    except (OSError, ValueError, IndexError, StopIteration):
        return None


def _is_mcp_command(args: list[str]) -> bool:
    return (
        (any(a == "paper" or a.endswith("/paper") for a in args) and "mcp" in args)
        or any(a.endswith("backend.mcp_server") for a in args)
        or (any(a.endswith("backend.cli") for a in args) and "mcp" in args)
    )


def mcp_processes() -> list[dict]:
    """列出当前 paper mcp 进程；用代码 mtime 标记可能未重启的旧进程。"""
    code_paths = [
        Path(__file__).parent.parent / "mcp_server.py",
        Path(__file__).parent.parent / "api" / "search.py",
        Path(__file__).parent.parent / "workers" / "runner.py",
    ]
    newest_code = max((p.stat().st_mtime for p in code_paths if p.exists()), default=0)
    found = []
    proc = Path("/proc")
    if not proc.exists():
        return found
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            args = (entry / "cmdline").read_bytes().split(b"\0")
            args = [a.decode(errors="replace") for a in args if a]
        except OSError:
            continue
        if not _is_mcp_command(args):
            continue
        pid = int(entry.name)
        epoch = _process_start_epoch(pid)
        found.append({
            "pid": pid,
            "started": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch)) if epoch else None,
            "command": " ".join(args),
            "stale_suspected": bool(epoch and epoch < newest_code),
            "installed_version": get_version(),
        })
    return sorted(found, key=lambda p: p["pid"])
