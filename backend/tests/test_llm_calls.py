"""Test: llm_calls 调用明细落库（usage 解析 / 失败记录 / call_meta）+ /api/llm_usage 聚合。"""
import pytest

from backend.config import CONFIG
from backend.db.conn import get_conn


class _FakeResp:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class _FakeSession:
    def __init__(self, data=None, exc=None):
        self._data = data
        self._exc = exc

    def post(self, *a, **k):
        if self._exc:
            raise self._exc
        return _FakeResp(self._data)


def _client_with(monkeypatch, session) -> "object":
    monkeypatch.setitem(CONFIG["secrets"], "llm_api_key", "test-key")
    from backend.external.llm import LLMClient
    c = LLMClient()
    c._session = session
    return c


def _read_rows() -> list:
    conn = get_conn()
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM llm_calls ORDER BY id").fetchall()]
    finally:
        conn.close()


def test_chat_logs_usage_and_call_meta(temp_db, monkeypatch):
    from backend.external.llm import LLMClient

    conn = get_conn()
    try:
        pid = conn.execute(
            "INSERT INTO papers (dir_name, year, first_author) VALUES ('llmcall_t', 2023, 't')"
        ).lastrowid
        tid = conn.execute(
            "INSERT INTO tasks (kind, status, paper_id) VALUES ('summary', 'running', ?)", (pid,)
        ).lastrowid
    finally:
        conn.close()

    data = {
        "choices": [{"message": {"content": "  中文结果  "}}],
        "usage": {"prompt_tokens": 111, "completion_tokens": 22, "total_tokens": 133,
                  "prompt_cache_hit_tokens": 100},
    }
    client = _client_with(monkeypatch, _FakeSession(data=data))
    client.call_meta = {"kind": "summary", "paper_id": pid, "task_id": tid}

    out = client.chat("SYS", "USER")

    assert out == "中文结果"
    rows = _read_rows()
    assert len(rows) == 1
    r = rows[0]
    assert r["kind"] == "summary" and r["paper_id"] == pid and r["task_id"] == tid
    assert r["model"] == CONFIG["llm"]["model"]
    assert r["prompt_tokens"] == 111 and r["completion_tokens"] == 22
    assert r["cached_tokens"] == 100
    assert r["prompt_chars"] == len("SYS") + len("USER")
    assert r["completion_chars"] == len("  中文结果  ")
    assert r["ok"] == 1 and r["error"] is None
    assert r["latency_ms"] is not None


def test_chat_failure_logged_and_reraised(temp_db, monkeypatch):
    from backend.external.llm import LLMClient

    client = _client_with(monkeypatch, _FakeSession(exc=RuntimeError("401 unauthorized")))

    with pytest.raises(RuntimeError):
        client.chat("S", "U")

    rows = _read_rows()
    assert len(rows) == 1
    assert rows[0]["ok"] == 0
    assert "401" in rows[0]["error"]
    assert rows[0]["prompt_tokens"] is None


def test_chat_without_usage_and_meta(temp_db, monkeypatch):
    """提供方不回 usage / 调用方未注入 call_meta：行仍落，相应字段为 NULL。"""
    from backend.external.llm import LLMClient

    client = _client_with(monkeypatch, _FakeSession(data={"choices": [{"message": {"content": "x"}}]}))
    client.chat("S", "U")

    r = _read_rows()[0]
    assert r["kind"] is None and r["paper_id"] is None
    assert r["prompt_tokens"] is None and r["completion_tokens"] is None
    assert r["ok"] == 1


def test_llm_usage_api_aggregates(temp_db, monkeypatch):
    monkeypatch.setitem(CONFIG["llm"], "price_input_per_mtok", 2.0)
    monkeypatch.setitem(CONFIG["llm"], "price_cached_input_per_mtok", 0.04)
    monkeypatch.setitem(CONFIG["llm"], "price_output_per_mtok", 8.0)
    conn = get_conn()
    try:
        conn.execute(
            """INSERT INTO llm_calls (kind, model, prompt_tokens, completion_tokens,
                      cached_tokens, prompt_chars, ok)
               VALUES ('summary', 'm1', 100, 50, 40, 400, 1),
                      ('translate', 'm1', 200, 80, 0, 800, 1),
                      ('translate', 'm1', NULL, NULL, NULL, 100, 0)""")
    finally:
        conn.close()

    from backend.app import create_app
    c = create_app().test_client()
    r = c.get("/api/llm_usage")
    assert r.status_code == 200
    d = r.get_json()

    assert d["totals"]["calls"] == 3
    assert d["totals"]["prompt_tokens"] == 300
    assert d["totals"]["completion_tokens"] == 130
    assert d["totals"]["cached_tokens"] == 40
    assert d["totals"]["errors"] == 1
    # 成本：input (300-40)*2/1e6 + cached 40*0.04/1e6 + output 130*8/1e6
    assert d["totals"]["cost_total"] == pytest.approx(0.0015616, abs=1e-6)
    assert d["prices"] == {"input_per_mtok": 2.0,
                           "cached_input_per_mtok": 0.04, "output_per_mtok": 8.0}
    kinds = {k["kind"]: k for k in d["by_kind"]}
    assert kinds["summary"]["calls"] == 1 and kinds["translate"]["calls"] == 2
    assert kinds["translate"]["errors"] == 1
    assert d["by_model"][0]["model"] == "m1"
