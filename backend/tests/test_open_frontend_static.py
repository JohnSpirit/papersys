"""Flask 托管 frontend/dist、SPA 回退、/api 发现、跨站写保护与健康检查测试。

3.11.2-open 无登录体系：健康检查始终返回存储状态与版本；同源写放行，
跨站写拒绝；前端深层路由回退 index.html，/api 未知路径保持 JSON 404。
"""
import sqlite3
from pathlib import Path

import pytest

from backend.version import get_version


@pytest.fixture()
def app_client(tmp_path, monkeypatch):
    """隔离数据目录与 SQLite 的测试客户端；PAPER_FRONTEND_DIST 指向临时 dist。"""
    from backend import config as cfg
    from backend.db import conn as conn_mod

    monkeypatch.setitem(cfg.CONFIG["paths"], "data_dir", str(tmp_path / "data"))
    monkeypatch.setitem(cfg.CONFIG["paths"], "cache_dir", str(tmp_path / "cache"))
    monkeypatch.setitem(cfg.CONFIG["paths"], "log_dir", str(tmp_path / "logs"))
    monkeypatch.setitem(cfg.CONFIG["paths"], "library_root", str(tmp_path / "library"))
    monkeypatch.setitem(cfg.CONFIG["paths"], "inbox_dir", str(tmp_path / "inbox"))
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(conn_mod, "DB_PATH", db_path)
    schema = (Path(conn_mod.__file__).parent / "schema.sql").read_text(encoding="utf-8")
    raw = sqlite3.connect(db_path)
    try:
        raw.executescript(schema)
        raw.commit()
    finally:
        raw.close()

    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html><body>spa-index</body></html>", encoding="utf-8")
    (dist / "assets" / "app-abc123.js").write_text("console.log('app');", encoding="utf-8")
    monkeypatch.setenv("PAPER_FRONTEND_DIST", str(dist))

    from backend.workers.runner import scheduler
    monkeypatch.setattr(scheduler, "start", lambda: None)

    from backend.app import create_app
    app = create_app()
    app.config["TESTING"] = True
    return app, app.test_client()


def test_root_serves_index_html(app_client):
    app, client = app_client
    r = client.get("/")
    assert r.status_code == 200
    assert b"spa-index" in r.data


def test_static_asset_served_with_long_cache(app_client):
    app, client = app_client
    r = client.get("/assets/app-abc123.js")
    assert r.status_code == 200
    assert b"console.log" in r.data
    assert "max-age=31536000" in r.headers.get("Cache-Control", "")


def test_deep_route_falls_back_to_index_html(app_client):
    """前端深层路由（如 /papers/1）回退 index.html，由前端路由接管。"""
    app, client = app_client
    r = client.get("/papers/1")
    assert r.status_code == 200
    assert b"spa-index" in r.data


def test_unknown_api_path_stays_json_404(app_client):
    """/api 下的未知路径不回退到前端页面。"""
    app, client = app_client
    r = client.get("/api/definitely-not-an-endpoint")
    assert r.status_code == 404
    assert r.get_json()["error"]


def test_api_discovery_moved_to_api_root(app_client):
    """API 发现信息在 /api，且不再包含登录/用户管理端点。"""
    app, client = app_client
    r = client.get("/api")
    assert r.status_code == 200
    body = r.get_json()
    assert body["service"] == "paper-survey-system backend"
    assert "/api/health" in body["endpoints"]
    assert "/api/auth/login" not in body["endpoints"]
    assert not any("admin" in ep for ep in body["endpoints"])


def test_health_always_reports_storage_and_version(app_client):
    """无登录模式下健康检查始终返回存储状态，版本显示 3.11.2-open。"""
    app, client = app_client
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["version"] == get_version() == "3.11.2-open"
    assert body["storage"]["ok"] is True


def test_cross_site_write_rejected(app_client):
    """Sec-Fetch-Site: cross-site 的写请求一律 403，不进入业务视图。"""
    app, client = app_client
    r = client.put("/api/papers/1/doc/en",
                   json={"text": "x"},
                   headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403
    assert r.get_json()["code"] == "cross_site_request"


def test_foreign_origin_write_rejected(app_client):
    """Origin 既非同源也不在 CORS 允许列表的写请求 403。"""
    app, client = app_client
    r = client.put("/api/papers/1/doc/en",
                   json={"text": "x"},
                   headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    assert r.get_json()["code"] == "origin_denied"


def test_same_origin_write_passes_gate(app_client):
    """同源写请求通过写保护（到达业务视图；论文不存在故 404 而非 403）。"""
    app, client = app_client
    r = client.put("/api/papers/1/doc/en",
                   json={"text": "x"},
                   headers={"Origin": "http://localhost"})
    assert r.status_code == 404


def test_cors_allowlisted_origin_write_passes_gate(app_client):
    """CORS 允许列表中的来源（前端 dev server 场景）通过写保护。"""
    app, client = app_client
    r = client.put("/api/papers/1/doc/en",
                   json={"text": "x"},
                   headers={"Origin": "http://localhost:5173"})
    assert r.status_code == 404


def test_non_browser_write_without_origin_passes_gate(app_client):
    """无 Origin 的非浏览器客户端（curl/MCP/服务间调用）放行。"""
    app, client = app_client
    r = client.put("/api/papers/1/doc/en", json={"text": "x"})
    assert r.status_code == 404


def test_proxy_fix_applied(app_client):
    """ProxyFix 包装 wsgi_app（反向代理部署时正确解析 scheme/host）。"""
    from werkzeug.middleware.proxy_fix import ProxyFix
    app, _ = app_client
    assert isinstance(app.wsgi_app, ProxyFix)


def test_missing_dist_returns_json_hint(app_client, tmp_path, monkeypatch):
    """dist 未构建时 / 返回 JSON 提示而不是 500。"""
    app, _ = app_client
    empty = tmp_path / "empty-dist"
    empty.mkdir()
    monkeypatch.setenv("PAPER_FRONTEND_DIST", str(empty))
    from backend.app import create_app
    fresh = create_app()
    fresh.config["TESTING"] = True
    r = fresh.test_client().get("/")
    assert r.status_code == 404
    assert "frontend dist not built" in r.get_json()["hint"]


def test_startup_operator_column_check_passes_on_current_schema(app_client):
    """新库（schema.sql 自带业务操作者列）通过 create_app 启动检查。"""
    from backend.db.authz_schema import authz_schema_status
    state = authz_schema_status()
    assert state["status"] == "ok"
    assert state["missing_columns"] == []


def test_startup_rejects_legacy_db_missing_operator_columns(tmp_path, monkeypatch):
    """旧库缺业务操作者列时 create_app 在启动阶段抛 RuntimeError（fail-fast，
    不等首次业务操作才失败）；init_db 不会为已存在的旧表补列。"""
    from backend import config as cfg
    from backend.db import conn as conn_mod

    monkeypatch.setitem(cfg.CONFIG["paths"], "data_dir", str(tmp_path / "data"))
    monkeypatch.setitem(cfg.CONFIG["paths"], "cache_dir", str(tmp_path / "cache"))
    monkeypatch.setitem(cfg.CONFIG["paths"], "log_dir", str(tmp_path / "logs"))
    monkeypatch.setitem(cfg.CONFIG["paths"], "library_root", str(tmp_path / "library"))
    monkeypatch.setitem(cfg.CONFIG["paths"], "inbox_dir", str(tmp_path / "inbox"))
    legacy_db = tmp_path / "legacy.db"
    schema = (Path(conn_mod.__file__).parent / "schema.sql").read_text(encoding="utf-8")
    raw = sqlite3.connect(legacy_db)
    try:
        # 用完整 schema 建库再删操作者列，模拟「旧业务列齐全、只缺操作者列」的历史库
        raw.executescript(schema)
        raw.execute("ALTER TABLE papers DROP COLUMN created_by_user_id")
        raw.execute("ALTER TABLE tasks DROP COLUMN execution_subject")
        raw.commit()
    finally:
        raw.close()
    monkeypatch.setattr(conn_mod, "DB_PATH", legacy_db)

    from backend.app import create_app
    with pytest.raises(RuntimeError, match="authorization schema is missing required columns") as exc:
        create_app()
    assert "papers.created_by_user_id" in str(exc.value)
    assert "tasks.execution_subject" in str(exc.value)
