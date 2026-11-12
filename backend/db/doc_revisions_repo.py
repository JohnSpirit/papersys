"""doc_revisions 表的写入与查询（自建公示层修订史）。"""
import hashlib
import sqlite3
from pathlib import Path
from typing import Optional

from backend.config import CONFIG
from backend.db.conn import get_conn

def _snapshots_dir() -> Path:
    """惰性解析快照目录（每次调用从 CONFIG 读，便于测试隔离）。"""
    return Path(CONFIG["paths"]["data_dir"]) / "doc_snapshots"


def _ensure_snapshots_dir() -> Path:
    d = _snapshots_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def record_revision(
    paper_id: int,
    doc_type: str,
    content: str,
    source: str,
    conn: Optional[sqlite3.Connection] = None,
    created_by_user_id: Optional[int] = None,
) -> int:
    """记录一次文档修订：hash + 完整快照落盘。返回 revision id。

    快照文件名：doc_p{paper_id}_{doc_type}_{ts}.md（ts 精确到毫秒级，防同秒碰撞）。
    DB 插入失败时清理刚写的快照文件，避免孤儿文件堆积。
    传入 conn 时不自建连接/不关闭，由调用方事务包裹。
    created_by_user_id 仅为兼容历史库保留；公开版无用户体系，调用方不传，
    新修订固定记 NULL。
    """
    import time

    snap_dir = _ensure_snapshots_dir()
    ts = time.strftime("%Y%m%d_%H%M%S") + f"_{int(time.time() * 1000) % 1000:03d}"
    payload_path = snap_dir / f"doc_p{paper_id}_{doc_type}_{ts}.md"
    payload_path.write_text(content, encoding="utf-8")

    own = conn is None
    if own:
        conn = get_conn()
    try:
        cur = conn.execute(
            """INSERT INTO doc_revisions
               (paper_id, doc_type, content_hash, source, size_bytes, payload_path, created_by_user_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (paper_id, doc_type, _md5(content), source, len(content.encode("utf-8")),
             str(payload_path), created_by_user_id),
        )
        return cur.lastrowid
    except Exception:
        payload_path.unlink(missing_ok=True)
        raise
    finally:
        if own:
            conn.close()


def list_revisions(paper_id: int, doc_type: Optional[str] = None) -> list[dict]:
    conn = get_conn()
    try:
        if doc_type:
            rows = conn.execute(
                "SELECT * FROM doc_revisions WHERE paper_id = ? AND doc_type = ? ORDER BY id DESC",
                (paper_id, doc_type),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM doc_revisions WHERE paper_id = ? ORDER BY id DESC",
                (paper_id,),
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_revision(revision_id: int) -> Optional[dict]:
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM doc_revisions WHERE id = ?", (revision_id,)
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def read_revision_payload(revision_id: int) -> Optional[str]:
    rev = get_revision(revision_id)
    if not rev or not rev.get("payload_path"):
        return None
    p = Path(rev["payload_path"])
    if not p.exists():
        return None
    return p.read_text(encoding="utf-8", errors="replace")
