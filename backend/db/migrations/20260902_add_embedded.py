"""Migration：papers 表加 status_embedded 列（P4 RAG 状态列，第 6 状态列）。

幂等：pragma 检查后 ALTER。新库由 schema.sql 自动包含。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from backend.db.conn import get_conn  # noqa: E402


def migrate() -> None:
    conn = get_conn()
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(papers)").fetchall()}
        if "status_embedded" not in cols:
            conn.execute("ALTER TABLE papers ADD COLUMN status_embedded TEXT DEFAULT 'missing'")
            print("added papers.status_embedded")
        else:
            print("status_embedded exists, skip")
        # embeddings 表由 schema.sql 创建（init_db 幂等执行）
        conn.execute(
            """CREATE TABLE IF NOT EXISTS embeddings (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                paper_id        INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
                chunk_id        TEXT NOT NULL,
                chunk_index     INTEGER NOT NULL,
                text            TEXT NOT NULL,
                vector          BLOB NOT NULL,
                dim             INTEGER NOT NULL,
                model           TEXT,
                created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (paper_id, chunk_index)
            )"""
        )
        conn.commit()
    finally:
        conn.close()
    print("v3 embed migration done")


if __name__ == "__main__":
    migrate()
