"""Migration：annotations.source 默认值 'outline' -> 'web'（旧库修正）。

3.0 起 schema.sql 的 annotations.source 默认值为 'web'，但 CREATE TABLE IF NOT EXISTS
不更新旧库已有表的定义，旧库仍停留在 2.0 时代的 'outline'。
SQLite 不支持修改列默认值，需重建表。
幂等：建表 SQL 不含 DEFAULT 'outline' 时直接返回。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from backend.db.conn import get_conn  # noqa: E402

# 列定义与 schema.sql 的 annotations 表保持一致
_CREATE_NEW = """CREATE TABLE annotations_new (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_id        INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    type            TEXT NOT NULL,
    anchor          TEXT,
    payload_json    TEXT,
    source          TEXT DEFAULT 'web',
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
)"""


def migrate() -> None:
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'annotations'"
        ).fetchone()
        if not row or "DEFAULT 'outline'" not in row["sql"]:
            print("annotations.source default already 'web' (or table missing); nothing to do")
            return
        conn.execute("BEGIN")
        conn.execute(_CREATE_NEW)
        conn.execute(
            "INSERT INTO annotations_new (id, paper_id, type, anchor, payload_json, source, created_at)"
            " SELECT id, paper_id, type, anchor, payload_json, source, created_at FROM annotations"
        )
        conn.execute("DROP TABLE annotations")
        conn.execute("ALTER TABLE annotations_new RENAME TO annotations")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_annotations_paper ON annotations(paper_id)"
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    print("annotations.source default migrated to 'web'")


if __name__ == "__main__":
    migrate()
