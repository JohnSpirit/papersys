"""公开版 MCP HTTP 传输约束：无 OAuth，且继续禁止本地文件入库工具。

- stdio 保留全部 19 个工具（见 test_mcp_stdio_sandbox 的端到端覆盖）
- HTTP 不注册 ingest_local_file / ingest_local_files：
  远程调用方不应能读写服务器本地路径，该限制不依赖任何登录机制
"""
import asyncio
import json
import socket
import threading
import time

EXPECTED_HTTP_TOOLS = {
    "search_papers", "semantic_search", "ingest_by_url", "get_task",
    "list_tasks", "get_paper", "get_paper_by_doi", "get_papers_by_dois",
    "get_papers", "search_library", "read_fulltext", "search_fulltext",
    "read_translation", "get_summary", "get_citations", "get_annotations",
    "add_annotation",
}


def _run(coro):
    return asyncio.run(coro)


def test_http_tool_set_excludes_local_file_ingest():
    from backend.mcp_server import _http_tools

    names = {tool.__name__ for tool in _http_tools()}
    assert names == EXPECTED_HTTP_TOOLS
    assert "ingest_local_file" not in names
    assert "ingest_local_files" not in names


def test_http_app_builds_without_oauth_secrets(temp_db, monkeypatch):
    from backend import config as cfg
    from backend.mcp_server import create_http_app

    for key in tuple(cfg.CONFIG["secrets"]):
        monkeypatch.setitem(cfg.CONFIG["secrets"], key, "")
    app = create_http_app(host="127.0.0.1")
    assert app is not None
    # Mount("/", ...) 的 path 归一化为 ""；MCP 端点位于 /mcp
    paths = {getattr(route, "path", None) for route in app.routes}
    assert paths & {"", "/"}


def test_http_real_client_cannot_ingest_local_files(temp_db):
    """真实 uvicorn + fastmcp Client（无认证）：工具列表无本地文件入库，
    调用被拒绝；业务工具（get_paper）正常可用。"""
    import uvicorn
    from fastmcp import Client
    from backend.db.conn import get_conn
    from backend.mcp_server import create_http_app

    conn = get_conn()
    try:
        paper_id = conn.execute(
            "INSERT INTO papers (dir_name, year, first_author, title)"
            " VALUES ('open_http_paper', 2026, 'test', 'HTTP Open Paper') RETURNING id"
        ).fetchone()[0]
    finally:
        conn.close()

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    config = uvicorn.Config(
        create_http_app(host="127.0.0.1"), host="127.0.0.1", port=port, log_level="error",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started

        async def exercise():
            async with Client(f"http://127.0.0.1:{port}/mcp", timeout=10) as client:
                tools = await client.list_tools()
                names = {tool.name for tool in tools}
                assert names == EXPECTED_HTTP_TOOLS
                assert "ingest_local_file" not in names
                assert "ingest_local_files" not in names

                result = await client.call_tool("get_paper", {"paper_id": paper_id})
                assert "HTTP Open Paper" in result.content[0].text

                for forbidden in ("ingest_local_file", "ingest_local_files"):
                    try:
                        denied = await client.call_tool(
                            forbidden, {"path": "/tmp/x.pdf", "paths": ["/tmp/x.pdf"]},
                            raise_on_error=False,
                        )
                    except Exception as exc:  # 协议层错误同样算拒绝
                        assert "not found" in str(exc).lower() or "unknown" in str(exc).lower()
                    else:
                        assert getattr(denied, "is_error", False), denied

        _run(exercise())
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def test_stdio_module_still_exposes_local_file_tools():
    """stdio 传输保留全部工具（含本地文件入库）。"""
    from backend import mcp_server

    assert callable(mcp_server.ingest_local_file)
    assert callable(mcp_server.ingest_local_files)
    stdio_only = {"ingest_local_file", "ingest_local_files"}
    assert stdio_only.isdisjoint({tool.__name__ for tool in mcp_server._http_tools()})


def test_stdio_main_initializes_empty_storage(tmp_path, monkeypatch):
    """stdio 在空数据目录启动时先建库，业务工具不会读到缺失的 papers 表。"""
    from backend import mcp_server
    from backend.db import conn as conn_mod

    db_path = tmp_path / "empty" / "papers.db"
    monkeypatch.setattr(conn_mod, "DB_PATH", db_path)
    called = []
    monkeypatch.setattr(mcp_server.mcp, "run", lambda transport: called.append(transport))
    mcp_server.main("stdio")
    assert called == ["stdio"]
    conn = conn_mod.get_conn()
    try:
        assert conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0] == 0
    finally:
        conn.close()


def test_mcp_tool_errors_stay_json(temp_db):
    """红线 11：工具错误以 {"error": ...} 返回，永不抛异常（无认证上下文）。"""
    from backend import mcp_server

    result = json.loads(mcp_server.get_paper(99999))
    assert "error" in result
    result = json.loads(mcp_server.get_citations(1, direction="sideways"))
    assert "error" in result


def test_doctor_recognizes_compose_mcp_command():
    """doctor 同时识别 paper mcp、模块入口和 Compose 的 backend.cli 入口。"""
    from backend.services.doctor import _is_mcp_command

    assert _is_mcp_command(["python", "-m", "backend.cli", "mcp", "--transport", "http"])
    assert _is_mcp_command(["/usr/local/bin/paper", "mcp"])
    assert _is_mcp_command(["python", "-m", "backend.mcp_server"])
    assert not _is_mcp_command(["gunicorn", "backend.app:app"])
