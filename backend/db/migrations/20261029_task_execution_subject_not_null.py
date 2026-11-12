"""重建 tasks 表，统一 execution_subject 的 NOT NULL 约束。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from backend.db.conn import get_conn  # noqa: E402

TASK_COLUMNS = (
    "id", "kind", "payload_json", "status", "progress", "error", "retries",
    "paper_id", "log_path", "created_by_user_id", "execution_subject",
    "required_permission", "created_at", "started_at", "finished_at",
)

TASK_DDL = """
CREATE TABLE tasks_not_null (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    payload_json TEXT,
    status TEXT DEFAULT 'pending',
    progress REAL DEFAULT 0.0,
    error TEXT,
    retries INTEGER DEFAULT 0,
    paper_id INTEGER REFERENCES papers(id) ON DELETE SET NULL,
    log_path TEXT,
    created_by_user_id INTEGER,
    execution_subject TEXT NOT NULL DEFAULT 'system',
    required_permission TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMP,
    finished_at TIMESTAMP
)
"""


def migrate(conn=None) -> dict:
    own = conn is None
    if own:
        conn = get_conn()
    foreign_keys = bool(conn.execute("PRAGMA foreign_keys").fetchone()[0])
    if conn.in_transaction:
        conn.commit()
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        columns = {row["name"]: row for row in conn.execute("PRAGMA table_info(tasks)")}
        subject = columns.get("execution_subject")
        if subject is None:
            raise RuntimeError("tasks.execution_subject is missing; run 20261027_authz.py first")
        if subject["notnull"]:
            return {"rebuilt": False, "backfilled_tasks": 0}
        conn.execute("BEGIN IMMEDIATE")
        backfilled = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE execution_subject IS NULL"
        ).fetchone()[0]
        conn.execute("UPDATE tasks SET execution_subject='system' WHERE execution_subject IS NULL")
        conn.execute(TASK_DDL)
        names = ", ".join(TASK_COLUMNS)
        conn.execute(
            f"INSERT INTO tasks_not_null ({names}) SELECT {names} FROM tasks ORDER BY id"
        )
        conn.execute("DROP TABLE tasks")
        conn.execute("ALTER TABLE tasks_not_null RENAME TO tasks")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_kind ON tasks(kind)")
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(f"task table rebuild failed foreign key checks: {violations}")
        conn.commit()
        return {"rebuilt": True, "backfilled_tasks": backfilled}
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        if foreign_keys:
            conn.execute("PRAGMA foreign_keys=ON")
        if own:
            conn.close()


if __name__ == "__main__":
    print(f"task execution_subject migration done: {migrate()}")
