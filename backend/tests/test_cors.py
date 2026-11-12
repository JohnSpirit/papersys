"""Test: CORS 来源白名单（3.1.0b5 收紧回归）。

此前 CORS(app) 全开放（flask-cors 默认 origins='*'）+ 全端点无鉴权，任意网页
可在用户浏览器内跨站读写 API。现在只允许 config.toml [server].cors_origins
列出的来源。
"""
import pytest


@pytest.fixture
def client(temp_db):
    from backend.app import create_app
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


def test_allowed_origin_gets_cors_headers(client):
    r = client.get("/api/health", headers={"Origin": "http://localhost:5173"})
    assert r.status_code == 200
    assert r.headers.get("Access-Control-Allow-Origin") == "http://localhost:5173"


def test_disallowed_origin_gets_no_cors_headers(client):
    r = client.get("/api/health", headers={"Origin": "http://evil.example"})
    assert r.status_code == 200
    # 不在白名单的来源拿不到 CORS 响应头，浏览器会拦截跨站读取
    assert "Access-Control-Allow-Origin" not in r.headers


def test_preflight_from_disallowed_origin_rejected(client):
    r = client.options(
        "/api/papers",
        headers={
            "Origin": "http://evil.example",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    # 非 simple 跨站写（如 POST JSON）需要预检通过；被拒后浏览器不发实际请求
    assert "Access-Control-Allow-Origin" not in r.headers
