"""Test: Search API 参数校验（/api/semantic_search）。"""
import pytest


@pytest.fixture
def client(temp_db):
    from backend.app import create_app
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


def test_semantic_search_invalid_paper_id_400(client):
    r = client.post("/api/semantic_search", json={"query": "memory", "paper_id": "abc"})
    assert r.status_code == 400


def test_semantic_search_valid_paper_id_not_400(client):
    # 合法 paper_id（库内无论文）-> 服务层返回 {"error": ...} 但 HTTP 不是 400/500
    r = client.post("/api/semantic_search", json={"query": "memory", "paper_id": "1"})
    assert r.status_code == 200
    body = r.get_json()
    assert body is not None
    assert "error" in body


def test_semantic_search_returns_json_object(client):
    # 修复回归：/api/semantic_search 必须返回 JSON 对象（非 JSON 字符串文本）
    r = client.post("/api/semantic_search", json={"query": "memory"})
    assert r.status_code == 200
    assert r.is_json
    body = r.get_json()
    assert isinstance(body, dict)
    assert "error" in body


def test_search_api_sources_tried_and_brief(client, monkeypatch):
    from backend.api import search as search_mod

    def fake_search(query, limit, year_from, year_to):
        return search_mod.SearchItems([
            {"title": "Paper", "doi": "10.1/x", "abstract": "abc", "tldr": "short"},
        ], ["semantic_scholar", "openalex"])

    monkeypatch.setitem(search_mod._DISPATCHERS, "semantic_scholar", fake_search)
    response = client.post("/api/search", json={
        "source": "semantic_scholar", "query": "x", "brief": True,
    })
    assert response.status_code == 200
    body = response.get_json()
    assert body["sources_tried"] == ["semantic_scholar", "openalex"]
    assert body["count"] == 1
    assert "abstract" not in body["items"][0]
    assert "tldr" not in body["items"][0]
