"""任务与事件仓库：tasks / events 表的 CRUD。"""
import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

from backend.config import CONFIG
from backend.db.conn import get_conn

LOG_DIR = Path(CONFIG["paths"]["log_dir"])


# ── Tasks ──────────────────────────────────────────────────


def create_task(
    kind: str,
    payload: Optional[dict] = None,
    paper_id: Optional[int] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> int:
    """插入 pending 任务。传入 conn 时不自建连接/不关闭，由调用方事务包裹。

    公开版无用户体系：execution_subject 固定 'system'，created_by_user_id /
    required_permission 固定 NULL（三列仅为兼容历史库保留）。
    """
    own = conn is None
    if own:
        conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO tasks (kind, payload_json, paper_id, status)"
            " VALUES (?, ?, ?, 'pending')",
            (kind, json.dumps(payload or {}, ensure_ascii=False), paper_id),
        )
        task_id = cur.lastrowid
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_path = LOG_DIR / f"task_{task_id}.log"
        conn.execute("UPDATE tasks SET log_path = ? WHERE id = ?", (str(log_path), task_id))
        return task_id
    finally:
        if own:
            conn.close()


def create_or_reuse_active_task(
    kind: str,
    payload: Optional[dict] = None,
    paper_id: Optional[int] = None,
) -> tuple[int, bool]:
    """复用参数相同的活动任务；没有匹配任务时原子创建。

    公开版无用户体系：新任务固定 system 类别；历史 user 任务不再做
    角色/凭据检查，按 kind + paper_id + payload 匹配后可直接复用执行。
    """
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT id, payload_json FROM tasks"
            " WHERE kind = ? AND paper_id IS ? AND status IN ('pending', 'running')"
            " ORDER BY id DESC",
            (kind, paper_id),
        ).fetchall()
        expected = payload or {}
        for row in rows:
            try:
                existing = json.loads(row["payload_json"] or "{}")
            except json.JSONDecodeError:
                continue
            if existing == expected:
                conn.execute("COMMIT")
                return row["id"], True
        task_id = create_task(
            kind=kind, payload=payload, paper_id=paper_id, conn=conn,
        )
        conn.execute("COMMIT")
        return task_id, False
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def get_task(task_id: int) -> Optional[dict]:
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        try:
            d["payload"] = json.loads(d.get("payload_json") or "{}")
        except json.JSONDecodeError:
            d["payload"] = {}
        return d
    finally:
        conn.close()


def list_tasks(
    status: Optional[str] = None,
    kind: Optional[str] = None,
    paper_id: Optional[int] = None,
    limit: int = 100,
    offset: int = 0,
    created_by_user_id: Optional[int] = None,
) -> list[dict]:
    sql = "SELECT * FROM tasks WHERE 1=1"
    params: list = []
    if status:
        sql += " AND status = ?"; params.append(status)
    if kind:
        sql += " AND kind = ?"; params.append(kind)
    if paper_id:
        sql += " AND paper_id = ?"; params.append(paper_id)
    if created_by_user_id is not None:
        sql += " AND created_by_user_id = ?"; params.append(created_by_user_id)
    sql += " ORDER BY id DESC LIMIT ? OFFSET ?"; params += [limit, offset]
    conn = get_conn()
    try:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def count_tasks(
    status: Optional[str] = None,
    kind: Optional[str] = None,
    paper_id: Optional[int] = None,
    created_by_user_id: Optional[int] = None,
) -> int:
    sql = "SELECT COUNT(*) FROM tasks WHERE 1=1"
    params: list = []
    if status:
        sql += " AND status = ?"; params.append(status)
    if kind:
        sql += " AND kind = ?"; params.append(kind)
    if paper_id:
        sql += " AND paper_id = ?"; params.append(paper_id)
    if created_by_user_id is not None:
        sql += " AND created_by_user_id = ?"; params.append(created_by_user_id)
    conn = get_conn()
    try:
        return conn.execute(sql, params).fetchone()[0]
    finally:
        conn.close()


def update_task(
    task_id: int,
    *,
    status: Optional[str] = None,
    progress: Optional[float] = None,
    error: Optional[str] = None,
    paper_id: Optional[int] = None,
    started: bool = False,
    finished: bool = False,
    increment_retries: bool = False,
) -> None:
    sets, params = [], []
    if status is not None:
        sets.append("status = ?"); params.append(status)
    if progress is not None:
        sets.append("progress = ?"); params.append(progress)
    if error is not None:
        sets.append("error = ?"); params.append(error)
    if paper_id is not None:
        sets.append("paper_id = ?"); params.append(paper_id)
    if started:
        sets.append("started_at = CURRENT_TIMESTAMP")
    if finished:
        sets.append("finished_at = CURRENT_TIMESTAMP")
    if increment_retries:
        sets.append("retries = retries + 1")
    if not sets:
        return
    conn = get_conn()
    try:
        conn.execute(
            f"UPDATE tasks SET {', '.join(sets)} WHERE id = ?",
            params + [task_id],
        )
    finally:
        conn.close()


REQUEUE_KINDS = {"pipeline", "parse", "enrich", "embed", "summary"}
_DEFAULT_MAX_TASK_RETRIES = 3


def _max_task_retries() -> int:
    try:
        return max(0, int(CONFIG.get("workers", {}).get(
            "max_task_retries", _DEFAULT_MAX_TASK_RETRIES)))
    except (TypeError, ValueError):
        return _DEFAULT_MAX_TASK_RETRIES


def requeue_task(task_id: int, reason: str, *, level: str = "info") -> None:
    """把任务退回 pending，清空执行时间并累计重试次数。"""
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE tasks SET status='pending', progress=0, error=NULL,"
            " started_at=NULL, finished_at=NULL, retries=retries+1 WHERE id = ?",
            (task_id,),
        )
    finally:
        conn.close()
    emit_event(task_id, reason, level=level)


def recover_orphans(max_retries: Optional[int] = None) -> dict:
    """启动时恢复上次进程残留的 running 任务。

    可重跑的 pipeline/parse/enrich/embed/summary 任务退回 pending；
    其他任务或超过重试上限的任务标 error。返回重新入队与失败计数。
    """
    limit = _max_task_retries() if max_retries is None else max(0, int(max_retries))
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id, kind, retries FROM tasks WHERE status='running' ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    stats = {"requeued": 0, "failed": 0}
    for row in rows:
        if row["kind"] in REQUEUE_KINDS and (row["retries"] or 0) < limit:
            requeue_task(row["id"], "requeued after restart")
            stats["requeued"] += 1
            continue
        conn = get_conn()
        try:
            conn.execute(
                "UPDATE tasks SET status='error',"
                " error='orphaned: worker killed before completion',"
                " finished_at=CURRENT_TIMESTAMP WHERE id = ?",
                (row["id"],),
            )
        finally:
            conn.close()
        emit_event(row["id"], "orphaned: marked error on startup", level="error")
        stats["failed"] += 1
    return stats


def last_recovery_stats() -> dict:
    """读取最近一次启动恢复的计数；没有恢复记录时返回零。"""
    messages = ("requeued after restart", "orphaned: marked error on startup")
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT ts FROM events WHERE msg IN (?, ?) ORDER BY id DESC LIMIT 1",
            messages,
        ).fetchone()
        if not row:
            return {"requeued": 0, "failed": 0, "ts": None}
        counts = dict(conn.execute(
            "SELECT msg, COUNT(*) FROM events WHERE ts = ? AND msg IN (?, ?) GROUP BY msg",
            (row["ts"], *messages),
        ).fetchall())
        return {
            "requeued": counts.get(messages[0], 0),
            "failed": counts.get(messages[1], 0),
            "ts": row["ts"],
        }
    finally:
        conn.close()


def peek_pending(kind_filter: Optional[list[str]] = None) -> Optional[dict]:
    """只读预览下一个可领取任务；用于调度器先检查线程池容量。"""
    conn = get_conn()
    try:
        sql = "SELECT * FROM tasks WHERE status = 'pending'"
        params: list = []
        if kind_filter:
            placeholders = ", ".join("?" for _ in kind_filter)
            sql += f" AND kind IN ({placeholders})"
            params += kind_filter
        sql += (
            " AND (paper_id IS NULL OR paper_id NOT IN ("
            " SELECT paper_id FROM tasks"
            " WHERE status = 'running' AND paper_id IS NOT NULL))"
            " ORDER BY id ASC LIMIT 1"
        )
        row = conn.execute(sql, params).fetchone()
        if not row:
            return None
        d = dict(row)
        try:
            d["payload"] = json.loads(d.get("payload_json") or "{}")
        except json.JSONDecodeError:
            d["payload"] = {}
        return d
    finally:
        conn.close()


def claim_pending(kind_filter: Optional[list[str]] = None) -> Optional[dict]:
    """原子地领取一个 pending 任务，置为 running。

    使用 UPDATE ... RETURNING (SQLite 3.35+) 单语句原子操作，
    避免 BEGIN IMMEDIATE + SELECT + UPDATE 之间的竞争窗口。
    started_at 不在领取时写入；worker 真正开始执行时才更新。

    同一 paper 已有 running 任务时，该 paper 的其他任务不会被领取
    （留在 pending，等 running 任务结束后下一轮 poll 再领）——防止两个
    task 并发写同一篇论文（如两个 pipeline task 同时向同一个解析目录
    解包 full.md/images）。paper_id 为 NULL 的任务（ingest/feed）不受限。
    """
    conn = get_conn()
    try:
        sql = (
            "UPDATE tasks SET status = 'running'"
            " WHERE id = ("
            "   SELECT id FROM tasks WHERE status = 'pending'"
        )
        params: list = []
        if kind_filter:
            placeholders = ", ".join("?" for _ in kind_filter)
            sql += f" AND kind IN ({placeholders})"
            params += kind_filter
        sql += (
            "   AND (paper_id IS NULL OR paper_id NOT IN ("
            "       SELECT paper_id FROM tasks"
            "       WHERE status = 'running' AND paper_id IS NOT NULL"
            "   ))"
        )
        sql += " ORDER BY id ASC LIMIT 1"
        sql += ") RETURNING *"

        row = conn.execute(sql, params).fetchone()
        if not row:
            return None
        d = dict(row)
        try:
            d["payload"] = json.loads(d.get("payload_json") or "{}")
        except json.JSONDecodeError:
            d["payload"] = {}
        return d
    except sqlite3.OperationalError:
        return None
    finally:
        conn.close()


# ── Events ─────────────────────────────────────────────────


def emit_event(
    task_id: Optional[int],
    msg: str,
    level: str = "info",
    paper_id: Optional[int] = None,
) -> int:
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO events (task_id, paper_id, level, msg) VALUES (?, ?, ?, ?)",
            (task_id, paper_id, level, msg),
        )
        eid = cur.lastrowid
    finally:
        conn.close()

    if task_id:
        try:
            log_file = LOG_DIR / f"task_{task_id}.log"
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] [{level}] {msg}\n")
        except Exception:
            pass
    return eid


def list_events(
    since_id: int = 0,
    task_id: Optional[int] = None,
    paper_id: Optional[int] = None,
    limit: int = 500,
    owner_user_id: Optional[int] = None,
) -> list[dict]:
    if owner_user_id is None:
        sql = "SELECT events.* FROM events WHERE events.id > ?"
    else:
        sql = (
            "SELECT events.* FROM events JOIN tasks ON tasks.id=events.task_id"
            " WHERE events.id > ? AND tasks.created_by_user_id=?"
        )
    params: list[Any] = [since_id]
    if owner_user_id is not None:
        params.append(owner_user_id)
    if task_id:
        sql += " AND events.task_id = ?"; params.append(task_id)
    if paper_id:
        sql += " AND events.paper_id = ?"; params.append(paper_id)
    sql += " ORDER BY events.id ASC LIMIT ?"; params.append(limit)
    conn = get_conn()
    try:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def read_task_log(task_id: int, tail: int = 500) -> str:
    log_file = LOG_DIR / f"task_{task_id}.log"
    if not log_file.exists():
        return ""
    lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-tail:])
