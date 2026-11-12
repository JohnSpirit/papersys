"""公开版（无 OAuth / 无用户体系）后端约束的回归测试。

覆盖：
  - 认证模块、认证 API 蓝图与 `paper auth` 命令已整体删除
  - 业务蓝图与 MCP 工具不依赖任何认证上下文即可导入/调用
  - 旧库（含认证专用表）业务读写兼容：认证表不被读取、写入或删除
  - 新任务固定 system/NULL 写入
  - 跨站写保护独立于登录机制：同源写成功、跨站写拒绝
"""
import importlib
import json
import sqlite3
from pathlib import Path

import pytest

from backend.db.conn import get_conn

AUTH_TABLES = ("users", "user_roles", "auth_sessions", "mcp_grants", "auth_audit")

# 3.11.2 旧版库遗留的认证专用表结构（公开版只验证「不读取/不写入/不删除」）。
_LEGACY_AUTH_DDL = """
CREATE TABLE users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    gitlab_sub TEXT NOT NULL UNIQUE,
    username TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
);
CREATE TABLE user_roles (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    PRIMARY KEY (user_id, role)
);
CREATE TABLE auth_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_hash TEXT NOT NULL UNIQUE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    revoked_at TEXT
);
CREATE TABLE mcp_grants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sub TEXT NOT NULL,
    upstream_token_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    revoked_at TEXT
);
CREATE TABLE auth_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    source TEXT NOT NULL,
    action TEXT NOT NULL,
    outcome TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


def test_auth_modules_are_gone():
    """认证模块文件已整体删除（断言本树文件系统内容，与导入行为无关）。"""
    backend_root = Path(__file__).resolve().parents[1]
    gone = [
        backend_root / "auth",
        backend_root / "api" / "auth.py",
        backend_root / "api" / "admin_users.py",
        backend_root / "db" / "auth_repo.py",
        backend_root / "external" / "gitlab_admin.py",
    ]
    for path in gone:
        assert not path.exists(), path


def test_paper_auth_command_removed():
    from backend.cli import build_parser
    with pytest.raises(SystemExit):
        build_parser().parse_args(["auth", "bootstrap-admin"])


def test_business_blueprints_import_clean():
    for name in (
        "backend.api.actions",
        "backend.api.citations",
        "backend.api.glossary_api",
        "backend.api.llm_usage",
        "backend.api.origin_guard",
        "backend.api.papers",
        "backend.api.preview",
        "backend.api.reader",
        "backend.api.search",
        "backend.api.settings",
        "backend.api.tasks",
        "backend.mcp_server",
    ):
        importlib.import_module(name)


def _insert_paper(dir_name: str) -> int:
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO papers (dir_name, year, first_author, title)"
            " VALUES (?, 2024, 'test', ?)",
            (dir_name, dir_name),
        )
        return cur.lastrowid
    finally:
        conn.close()


def test_mcp_tools_run_without_auth_context(temp_db):
    from backend import mcp_server

    pid = _insert_paper("open_mcp_anon")
    result = json.loads(mcp_server.add_annotation(pid, "quoted text", "a note"))
    assert result.get("id")

    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT created_by_user_id, source FROM annotations WHERE id = ?",
            (result["id"],),
        ).fetchone()
        assert row["created_by_user_id"] is None
        assert row["source"] == "agent"
    finally:
        conn.close()

    missing = json.loads(mcp_server.get_task(99999))
    assert "error" in missing
    listed = json.loads(mcp_server.list_tasks())
    assert listed["count"] >= 0 and "items" in listed


def test_new_task_rows_are_system_and_null(temp_db):
    from backend.db import tasks_repo

    task_id = tasks_repo.create_task("summary", {"paper_id": None})
    task = tasks_repo.get_task(task_id)
    assert task["execution_subject"] == "system"
    assert task["created_by_user_id"] is None
    assert task["required_permission"] is None


def test_legacy_db_with_auth_tables_keeps_working(temp_db, tmp_path, monkeypatch):
    """模拟 3.11.2 旧版库：认证表存在且有数据，业务读写全部不受影响。"""
    from backend.db import conn as conn_mod
    from backend.db import doc_revisions_repo, tasks_repo

    legacy_path = tmp_path / "legacy.db"
    schema_sql = (Path(conn_mod.__file__).parent / "schema.sql").read_text(encoding="utf-8")
    raw = sqlite3.connect(legacy_path)
    try:
        raw.executescript(schema_sql)
        raw.executescript(_LEGACY_AUTH_DDL)
        raw.execute(
            "INSERT INTO users (gitlab_sub, username, status) VALUES ('sub-1', 'legacy', 'active')"
        )
        raw.execute("INSERT INTO user_roles (user_id, role) VALUES (1, 'admin')")
        raw.execute(
            "INSERT INTO auth_sessions (session_hash, user_id, created_at, expires_at,"
            " last_seen_at) VALUES ('hash-1', 1, '2026-01-01', '2026-12-31', '2026-01-01')"
        )
        raw.execute(
            "INSERT INTO tasks (kind, payload_json, status, execution_subject,"
            " created_by_user_id, required_permission)"
            " VALUES ('pipeline', '{}', 'pending', 'user', 1, 'pipeline.run')"
        )
        raw.commit()
    finally:
        raw.close()
    monkeypatch.setattr(conn_mod, "DB_PATH", legacy_path)

    # 业务写：新任务固定 system/NULL
    new_id = tasks_repo.create_task("summary", {"paper_id": None})
    new_task = tasks_repo.get_task(new_id)
    assert new_task["execution_subject"] == "system"
    assert new_task["created_by_user_id"] is None

    # 业务读：历史 user 任务正常读取；按操作者过滤的查询参数仍可用
    legacy_task = tasks_repo.get_task(1)
    assert legacy_task["execution_subject"] == "user"
    assert legacy_task["created_by_user_id"] == 1
    assert [t["id"] for t in tasks_repo.list_tasks(created_by_user_id=1)] == [1]

    # 业务写：批注与修订史固定 NULL 操作者
    pid = _insert_paper("open_legacy_paper")
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO annotations (paper_id, type, payload_json, source, created_by_user_id)"
            " VALUES (?, 'comment', '{}', 'web', NULL)",
            (pid,),
        )
        annotation_id = cur.lastrowid
    finally:
        conn.close()
    revision_id = doc_revisions_repo.record_revision(pid, "en", "edited body", "web_edit")
    conn = get_conn()
    try:
        assert conn.execute(
            "SELECT created_by_user_id FROM annotations WHERE id = ?", (annotation_id,)
        ).fetchone()[0] is None
        assert conn.execute(
            "SELECT created_by_user_id FROM doc_revisions WHERE id = ?", (revision_id,)
        ).fetchone()[0] is None
    finally:
        conn.close()

    # 认证专用表原样保留：行数与内容不变（不读取业务语义、不删除、不回写）
    raw = sqlite3.connect(legacy_path)
    try:
        assert raw.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
        assert raw.execute("SELECT username, status FROM users").fetchone() == ("legacy", "active")
        assert raw.execute("SELECT COUNT(*) FROM user_roles").fetchone()[0] == 1
        assert raw.execute("SELECT COUNT(*) FROM auth_sessions").fetchone()[0] == 1
        assert raw.execute("SELECT COUNT(*) FROM mcp_grants").fetchone()[0] == 0
        assert raw.execute("SELECT COUNT(*) FROM auth_audit").fetchone()[0] == 0
    finally:
        raw.close()


# ── 跨站写保护（独立于登录机制）─────────────────────────────


@pytest.fixture
def guard_client():
    from flask import Flask
    from backend.api.origin_guard import init_origin_guard

    app = Flask(__name__)
    init_origin_guard(app)

    @app.get("/api/read")
    def read():
        return {"ok": True}

    @app.post("/api/write")
    def write():
        return {"ok": True}

    app.config["TESTING"] = True
    return app.test_client()


def test_same_origin_write_succeeds(guard_client):
    # 测试客户端默认 Host: localhost；同源浏览器写请求携带同 host 的 Origin
    r = guard_client.post("/api/write", headers={"Origin": "http://localhost"})
    assert r.status_code == 200
    # 无 Origin 的非浏览器客户端（curl / CLI / MCP）放行
    assert guard_client.post("/api/write").status_code == 200


def test_cors_whitelist_origin_succeeds(guard_client):
    r = guard_client.post("/api/write", headers={"Origin": "http://localhost:5173"})
    assert r.status_code == 200


def test_cross_site_write_rejected(guard_client):
    r = guard_client.post("/api/write", headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403
    assert r.get_json()["code"] == "cross_site_request"

    r = guard_client.post("/api/write", headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    assert r.get_json()["code"] == "origin_denied"


def test_reads_are_not_origin_checked(guard_client):
    r = guard_client.get("/api/read", headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 200


def test_origin_guard_independent_of_auth_flag(guard_client, monkeypatch):
    """跨站写保护不读取 CONFIG['auth']：即使残留 auth.enabled=true 也照常执行。"""
    from backend import config as cfg
    monkeypatch.setitem(cfg.CONFIG.setdefault("auth", {}), "enabled", True)
    r = guard_client.post("/api/write", headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    assert guard_client.post("/api/write", headers={"Origin": "http://localhost"}).status_code == 200
