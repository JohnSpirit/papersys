"""Glossary 表 CRUD。"""
import sqlite3
from typing import Optional

from backend.db.conn import get_conn


def list_glossary() -> list[dict]:
    conn = get_conn()
    try:
        rows = conn.execute("SELECT * FROM glossary ORDER BY en COLLATE NOCASE").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def upsert_entry(en: str, zh: str, context: Optional[str] = None, notes: Optional[str] = None,
                 updated_by_user_id: Optional[int] = None) -> int:
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT id FROM glossary WHERE en = ? AND IFNULL(context,'') = IFNULL(?,'')",
            (en, context),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE glossary SET zh = ?, notes = ?, updated_by_user_id=?,"
                " updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (zh, notes, updated_by_user_id, row["id"]),
            )
            return row["id"]
        cur = conn.execute(
            "INSERT INTO glossary (en, context, zh, notes, updated_by_user_id)"
            " VALUES (?, ?, ?, ?, ?)",
            (en, context, zh, notes, updated_by_user_id),
        )
        return cur.lastrowid
    finally:
        conn.close()


def delete_entry(entry_id: int) -> None:
    conn = get_conn()
    try:
        conn.execute("DELETE FROM glossary WHERE id = ?", (entry_id,))
    finally:
        conn.close()


def render_for_prompt() -> str:
    """渲染为 system prompt 用文本：- en → zh（context / notes）。"""
    rows = list_glossary()
    if not rows:
        return ""
    parts = ["术语表（翻译时严格遵循）："]
    for r in rows:
        line = f"- {r['en']} → {r['zh']}"
        extras = []
        if r.get("context"):
            extras.append(r["context"])
        if r.get("notes"):
            extras.append(r["notes"])
        if extras:
            line += f"（{'; '.join(extras)}）"
        parts.append(line)
    return "\n".join(parts)