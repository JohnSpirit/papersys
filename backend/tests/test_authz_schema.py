"""业务表操作者字段的 schema 与迁移兼容测试（无登录体系）。

覆盖：
  - 20261027_authz 迁移为旧库补操作者列，但不再创建认证专用表
  - 新库 schema 不含认证专用表，保留业务操作者列
  - 新任务固定 system/NULL 写入；历史 user 任务可直接执行（无角色检查）
  - 启动检查 require_authz_columns 对缺列旧库 fail-fast
  - 20261029 execution_subject NOT NULL 迁移保持行/关联/索引
"""
import importlib.util
import sqlite3
from pathlib import Path

import pytest

from backend.db import tasks_repo
from backend.db.conn import get_conn
from backend.db.repo import PAPER_FIELDS

AUTH_TABLES = ("users", "user_roles", "auth_sessions", "mcp_grants", "auth_audit")


def _load_migration():
    path = Path(__file__).resolve().parents[1] / "db/migrations/20261027_authz.py"
    spec = importlib.util.spec_from_file_location("authz_migration_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _old_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE papers (id INTEGER PRIMARY KEY, dir_name TEXT UNIQUE, year INTEGER, first_author TEXT);
        CREATE TABLE annotations (id INTEGER PRIMARY KEY, paper_id INTEGER);
        CREATE TABLE doc_revisions (id INTEGER PRIMARY KEY, paper_id INTEGER);
        CREATE TABLE glossary (id INTEGER PRIMARY KEY, en TEXT, zh TEXT);
        CREATE TABLE tasks (
            id INTEGER PRIMARY KEY, kind TEXT, payload_json TEXT, status TEXT,
            paper_id INTEGER, log_path TEXT
        );
    """)
    return conn


def _table_exists(conn, name: str) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone())


def test_migration_adds_actor_columns_without_auth_tables():
    module = _load_migration()
    conn = _old_db()
    try:
        conn.execute("INSERT INTO papers VALUES (1, 'old2026_paper', 2026, 'old')")
        conn.execute("INSERT INTO annotations VALUES (1, 1)")
        conn.execute("INSERT INTO tasks VALUES (1, 'pipeline', '{}', 'pending', 1, NULL)")

        first = module.migrate(conn)
        task = conn.execute("SELECT * FROM tasks WHERE id = 1").fetchone()
        assert task["execution_subject"] == "system"
        assert task["created_by_user_id"] is None
        assert task["required_permission"] is None
        assert first["backfilled_tasks"] == 1
        assert conn.execute("SELECT created_by_user_id FROM papers").fetchone()[0] is None
        assert conn.execute("SELECT created_by_user_id FROM annotations").fetchone()[0] is None
        # 公开版迁移不再创建认证专用表
        for table in AUTH_TABLES:
            assert not _table_exists(conn, table), table

        second = module.migrate(conn)
        assert second["backfilled_tasks"] == 0
        assert second["added_columns"] == []
        for table in AUTH_TABLES:
            assert not _table_exists(conn, table), table
    finally:
        conn.close()


def test_new_schema_has_no_auth_tables_but_keeps_actor_columns(temp_db):
    conn = get_conn()
    try:
        for table in AUTH_TABLES:
            assert not _table_exists(conn, table), table
        for table, column in (
            ("papers", "created_by_user_id"),
            ("annotations", "created_by_user_id"),
            ("doc_revisions", "created_by_user_id"),
            ("glossary", "updated_by_user_id"),
            ("tasks", "execution_subject"),
            ("tasks", "created_by_user_id"),
            ("tasks", "required_permission"),
        ):
            assert column in {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    finally:
        conn.close()


def test_task_creation_is_system_and_null(temp_db):
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO papers (dir_name, year, first_author) VALUES ('task2026_paper', 2026, 'test')"
        )
    finally:
        conn.close()
    task_id = tasks_repo.create_task("summary", {"paper_id": None})
    task = tasks_repo.get_task(task_id)
    assert task["execution_subject"] == "system"
    assert task["created_by_user_id"] is None
    assert task["required_permission"] is None

    first, reused = tasks_repo.create_or_reuse_active_task("embed", {"paper_id": 1}, 1)
    assert not reused
    same_id, reused = tasks_repo.create_or_reuse_active_task("embed", {"paper_id": 1}, 1)
    assert reused and same_id == first
    assert tasks_repo.get_task(first)["execution_subject"] == "system"


def test_legacy_user_task_executes_without_role_check(temp_db):
    """历史库中的 user 任务（含操作者与权限列）不再做角色检查，可直接执行。"""
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO tasks (kind, payload_json, status, execution_subject,"
            " created_by_user_id, required_permission)"
            " VALUES ('noop', '{}', 'pending', 'user', 7, 'pipeline.run')"
        )
        task_id = cur.lastrowid
    finally:
        conn.close()

    from backend.workers.runner import WORKER_REGISTRY, scheduler
    WORKER_REGISTRY["noop"] = lambda ctx, payload: None
    try:
        scheduler._execute(tasks_repo.get_task(task_id))
    finally:
        WORKER_REGISTRY.pop("noop", None)
    task = tasks_repo.get_task(task_id)
    assert task["status"] == "ok"
    assert task["execution_subject"] == "user"  # 历史行保持原样，不被改写


def test_paper_actor_field_reaches_list_and_detail(temp_db):
    assert "created_by_user_id" in PAPER_FIELDS
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO papers (dir_name, year, first_author) VALUES ('old2026_paper', 2026, 'old')"
        )
        conn.execute(
            "INSERT INTO papers (dir_name, year, first_author, created_by_user_id)"
            " VALUES ('new2026_paper', 2026, 'new', 42)"
        )
    finally:
        conn.close()
    from backend.workers.runner import scheduler
    original_start = scheduler.start
    scheduler.start = lambda: None
    try:
        from backend.app import create_app
        client = create_app().test_client()
        listing = client.get("/api/papers").get_json()["items"]
        by_name = {paper["dir_name"]: paper for paper in listing}
        assert by_name["old2026_paper"]["created_by_user_id"] is None
        assert by_name["new2026_paper"]["created_by_user_id"] == 42
        new_id = by_name["new2026_paper"]["id"]
        assert client.get(f"/api/papers/{new_id}").get_json()["created_by_user_id"] == 42
    finally:
        scheduler.start = original_start


def test_authz_schema_distinguishes_missing_nullable_and_complete(temp_db):
    from backend.db.authz_schema import inspect_authz_schema

    conn = _old_db()
    try:
        state = inspect_authz_schema(conn)
        assert state["status"] == "missing"
        assert "tasks.execution_subject" in state["missing_columns"]
        _load_migration().migrate(conn)
        assert inspect_authz_schema(conn)["status"] == "ok"
    finally:
        conn.close()
    conn = get_conn()
    try:
        assert inspect_authz_schema(conn)["status"] == "ok"
    finally:
        conn.close()

    conn = _old_db()
    try:
        conn.execute("ALTER TABLE papers ADD COLUMN created_by_user_id INTEGER")
        conn.execute("ALTER TABLE annotations ADD COLUMN created_by_user_id INTEGER")
        conn.execute("ALTER TABLE doc_revisions ADD COLUMN created_by_user_id INTEGER")
        conn.execute("ALTER TABLE glossary ADD COLUMN updated_by_user_id INTEGER")
        conn.execute("ALTER TABLE tasks ADD COLUMN created_by_user_id INTEGER")
        conn.execute("ALTER TABLE tasks ADD COLUMN execution_subject TEXT DEFAULT 'system'")
        conn.execute("ALTER TABLE tasks ADD COLUMN required_permission TEXT")
        assert inspect_authz_schema(conn)["status"] == "not_null_mismatch"
    finally:
        conn.close()


def test_require_authz_columns_rejects_legacy_schema(temp_db, tmp_path, monkeypatch):
    from backend.db import conn as conn_mod
    from backend.db.authz_schema import require_authz_columns

    legacy_path = tmp_path / "legacy.db"
    raw = sqlite3.connect(legacy_path)
    raw.execute("CREATE TABLE tasks (id INTEGER PRIMARY KEY, kind TEXT)")
    raw.close()
    monkeypatch.setattr(conn_mod, "DB_PATH", legacy_path)
    with pytest.raises(RuntimeError, match="20261027_authz.py") as exc:
        require_authz_columns()
    assert "papers.created_by_user_id" in str(exc.value)
    assert "tasks.execution_subject" in str(exc.value)


def test_doctor_reports_authz_schema(temp_db, monkeypatch, capsys):
    from backend import cli

    monkeypatch.setattr(cli, "_doctor_retrieval", lambda: None)
    monkeypatch.setattr(cli, "_doctor_mineru", lambda: None)
    monkeypatch.setattr(cli, "_health", lambda: False)
    cli.cmd_doctor(None)
    assert "authz schema: ✅ complete" in capsys.readouterr().out


def test_task_not_null_migration_preserves_rows_relations_and_indexes():
    path = Path(__file__).resolve().parents[1] / "db/migrations/20261029_task_execution_subject_not_null.py"
    spec = importlib.util.spec_from_file_location("task_not_null_migration_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript("""
        CREATE TABLE papers (id INTEGER PRIMARY KEY);
        CREATE TABLE tasks (
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
            execution_subject TEXT DEFAULT 'system',
            required_permission TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            started_at TIMESTAMP,
            finished_at TIMESTAMP
        );
        CREATE INDEX idx_tasks_status ON tasks(status);
        CREATE INDEX idx_tasks_kind ON tasks(kind);
        CREATE TABLE events (
            id INTEGER PRIMARY KEY,
            task_id INTEGER REFERENCES tasks(id) ON DELETE CASCADE
        );
        CREATE TABLE llm_calls (
            id INTEGER PRIMARY KEY,
            task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL
        );
        INSERT INTO papers VALUES (1);
        INSERT INTO tasks (id, kind, paper_id, execution_subject)
        VALUES (42, 'translate', 1, NULL);
        INSERT INTO events VALUES (7, 42);
        INSERT INTO llm_calls VALUES (9, 42);
    """)
    try:
        first = module.migrate(conn)
        assert first == {"rebuilt": True, "backfilled_tasks": 1}
        task = conn.execute("SELECT id, kind, paper_id, execution_subject FROM tasks").fetchone()
        assert dict(task) == {
            "id": 42, "kind": "translate", "paper_id": 1, "execution_subject": "system",
        }
        assert conn.execute("SELECT task_id FROM events WHERE id=7").fetchone()[0] == 42
        assert conn.execute("SELECT task_id FROM llm_calls WHERE id=9").fetchone()[0] == 42
        subject = next(
            row for row in conn.execute("PRAGMA table_info(tasks)")
            if row["name"] == "execution_subject"
        )
        assert subject["notnull"] == 1
        assert subject["dflt_value"] == "'system'"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        indexes = {row["name"] for row in conn.execute("PRAGMA index_list(tasks)")}
        assert {"idx_tasks_status", "idx_tasks_kind"} <= indexes
        assert module.migrate(conn) == {"rebuilt": False, "backfilled_tasks": 0}
        conn.execute("DELETE FROM tasks WHERE id=42")
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
        assert conn.execute("SELECT task_id FROM llm_calls WHERE id=9").fetchone()[0] is None
    finally:
        conn.close()


def test_task_not_null_migration_handles_empty_and_current_tables(temp_db):
    path = Path(__file__).resolve().parents[1] / "db/migrations/20261029_task_execution_subject_not_null.py"
    spec = importlib.util.spec_from_file_location("empty_task_not_null_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.migrate() == {"rebuilt": False, "backfilled_tasks": 0}

    conn = _old_db()
    try:
        _load_migration().migrate(conn)
        assert module.migrate(conn) == {"rebuilt": False, "backfilled_tasks": 0}
        assert module.migrate(conn) == {"rebuilt": False, "backfilled_tasks": 0}
    finally:
        conn.close()
