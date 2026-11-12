"""增加业务表操作者字段（created_by_user_id 等）并回填 tasks.execution_subject。

3.11.2-open 起不再创建 users / user_roles / auth_sessions / mcp_grants /
auth_audit 认证专用表（无登录体系）；本迁移只补齐业务表保留的操作者列。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from backend.db.conn import get_conn  # noqa: E402


ACTOR_COLUMNS = {
    "papers": ("created_by_user_id", "INTEGER"),
    "annotations": ("created_by_user_id", "INTEGER"),
    "doc_revisions": ("created_by_user_id", "INTEGER"),
    "glossary": ("updated_by_user_id", "INTEGER"),
}

TASK_COLUMNS = (
    ("created_by_user_id", "INTEGER"),
    ("execution_subject", "TEXT NOT NULL DEFAULT 'system'"),
    ("required_permission", "TEXT"),
)


def migrate(conn=None) -> dict:
    own = conn is None
    if own:
        conn = get_conn()
    try:
        added = []
        for table, (column, definition) in ACTOR_COLUMNS.items():
            columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            if column not in columns:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
                added.append(f"{table}.{column}")
        task_columns = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
        backfilled = conn.execute(
            "SELECT COUNT(*) FROM tasks"
            + (" WHERE execution_subject IS NULL" if "execution_subject" in task_columns else "")
        ).fetchone()[0]
        for column, definition in TASK_COLUMNS:
            if column not in task_columns:
                conn.execute(f"ALTER TABLE tasks ADD COLUMN {column} {definition}")
                added.append(f"tasks.{column}")
        conn.execute("UPDATE tasks SET execution_subject='system' WHERE execution_subject IS NULL")
        conn.commit()
        return {"added_columns": added, "backfilled_tasks": backfilled}
    finally:
        if own:
            conn.close()


if __name__ == "__main__":
    print(f"authz migration done: {migrate()}")
