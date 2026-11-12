"""业务表操作者字段的启动和诊断检查（无登录体系，字段仅为兼容历史库保留）。"""
from backend.db.conn import get_conn

REQUIRED_COLUMNS = {
    "papers": ("created_by_user_id",),
    "tasks": ("created_by_user_id", "execution_subject", "required_permission"),
    "annotations": ("created_by_user_id",),
    "doc_revisions": ("created_by_user_id",),
    "glossary": ("updated_by_user_id",),
}
AUTHZ_MIGRATION = "backend/db/migrations/20261027_authz.py"
TASK_NOT_NULL_MIGRATION = "backend/db/migrations/20261029_task_execution_subject_not_null.py"


def inspect_authz_schema(conn) -> dict:
    missing = []
    for table, required in REQUIRED_COLUMNS.items():
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        missing.extend(f"{table}.{column}" for column in required if column not in columns)
    nullable = []
    if not missing:
        for row in conn.execute("PRAGMA table_info(tasks)"):
            if row["name"] == "execution_subject" and not row["notnull"]:
                nullable.append("tasks.execution_subject")
    status = "missing" if missing else "not_null_mismatch" if nullable else "ok"
    return {"status": status, "missing_columns": missing, "nullable_columns": nullable}


def authz_schema_status() -> dict:
    conn = get_conn()
    try:
        return inspect_authz_schema(conn)
    finally:
        conn.close()


def require_authz_columns() -> None:
    state = authz_schema_status()
    if not state["missing_columns"]:
        return
    missing = ", ".join(state["missing_columns"])
    raise RuntimeError(
        f"authorization schema is missing required columns: {missing}. "
        f"Run `PYTHONPATH=. python3 {AUTHZ_MIGRATION}` before starting the application."
    )
