"""Test: RateLimitedClient 磁盘缓存 key 必须涵盖 POST 请求体（3.1.0b4 修复回归）。

缺陷：key 只含 URL params 时，Sciverse 这类"固定 URL + json body"的 POST
不同查询撞同一 key，首个查询的 24h 缓存被后续所有查询命中。
"""
import json

import pytest
import requests

from backend.external import base as base_mod
from backend.external.base import RateLimitedClient


class _CountingSession:
    """记录真实网络调用次数；每次调用返回包 echo 请求体的 200 响应。"""

    def __init__(self):
        self.calls: list[dict] = []

    def request(self, method, url, timeout=None, **kw):
        self.calls.append({"method": method, "url": url, **kw})
        resp = requests.Response()
        resp.status_code = 200
        resp._content = json.dumps({"echo": kw.get("json")}).encode()
        resp.headers["Content-Type"] = "application/json"
        resp.encoding = "utf-8"
        return resp


class _Client(RateLimitedClient):
    service_name = "test_cache_key"
    rps = 1000.0


def _make_client(tmp_path, monkeypatch) -> tuple[_Client, _CountingSession]:
    monkeypatch.setattr(base_mod, "CACHE_DIR", tmp_path)
    c = _Client()
    session = _CountingSession()
    c._session = session
    return c, session


def test_post_different_bodies_do_not_share_cache(tmp_path, monkeypatch):
    c, session = _make_client(tmp_path, monkeypatch)
    r1 = c.post("https://api.example/search", json={"query": "alpha"})
    r2 = c.post("https://api.example/search", json={"query": "beta"})
    assert r1.json()["echo"] == {"query": "alpha"}
    # 修复前：第二个请求命中第一个的缓存，返回 alpha 的结果
    assert r2.json()["echo"] == {"query": "beta"}
    assert len(session.calls) == 2


def test_post_same_body_hits_cache(tmp_path, monkeypatch):
    c, session = _make_client(tmp_path, monkeypatch)
    c.post("https://api.example/search", json={"query": "alpha"})
    r2 = c.post("https://api.example/search", json={"query": "alpha"})
    assert getattr(r2, "from_cache", False) is True
    assert len(session.calls) == 1


def test_get_cache_still_works(tmp_path, monkeypatch):
    """GET + params 的缓存行为不受影响（同参数命中，不同参数不命中）。"""
    c, session = _make_client(tmp_path, monkeypatch)
    c.get("https://api.example/works", params={"id": "1"})
    r2 = c.get("https://api.example/works", params={"id": "1"})
    assert getattr(r2, "from_cache", False) is True
    r3 = c.get("https://api.example/works", params={"id": "2"})
    assert getattr(r3, "from_cache", False) is not True
    assert len(session.calls) == 2


# ── 429/5xx 指数退避重试（3.1.0b5）───────────────────────


def _status_resp(status_code: int) -> requests.Response:
    r = requests.Response()
    r.status_code = status_code
    return r


class _ScriptedSession:
    """按脚本依次返回响应（脚本耗尽后重复最后一项），记录调用次数。"""

    def __init__(self, statuses: list[int]):
        self.statuses = statuses
        self.calls = 0

    def request(self, method, url, timeout=None, **kw):
        i = min(self.calls, len(self.statuses) - 1)
        self.calls += 1
        return _status_resp(self.statuses[i])


def _make_retry_client(tmp_path, monkeypatch, statuses, url):
    monkeypatch.setattr(base_mod, "CACHE_DIR", tmp_path)
    c = _Client()
    c._session = _ScriptedSession(statuses)
    sleeps: list[float] = []
    monkeypatch.setattr(base_mod.time, "sleep", lambda s: sleeps.append(s))
    return c, sleeps, url


def test_retry_429_then_success(tmp_path, monkeypatch):
    """429 两次后 200：共 3 次请求，退避 2s/4s，返回成功响应。"""
    c, sleeps, url = _make_retry_client(
        tmp_path, monkeypatch, [429, 429, 200], "https://api.example/r1"
    )
    resp = c.get(url)
    assert resp.status_code == 200
    assert c._session.calls == 3
    # 只取 >=1s 的退避值（_rate_limit 的 0.001s 限频间隔也在同一条 sleep 上）
    backoffs = [s for s in sleeps if s >= 1.0]
    assert backoffs == [2.0, 4.0]


def test_retry_exhausted_raises(tmp_path, monkeypatch):
    """持续 503：1 次原始 + 3 次重试（退避 2/4/8s）后 raise。"""
    c, sleeps, url = _make_retry_client(
        tmp_path, monkeypatch, [503], "https://api.example/r2"
    )
    with pytest.raises(base_mod.RetriesExhaustedError):
        c.get(url)
    assert c._session.calls == 4
    backoffs = [s for s in sleeps if s >= 1.0]
    assert backoffs == [2.0, 4.0, 8.0]


def test_no_retry_on_404(tmp_path, monkeypatch):
    """404 不可重试：单次请求直接返回响应，无退避。"""
    c, sleeps, url = _make_retry_client(
        tmp_path, monkeypatch, [404], "https://api.example/r3"
    )
    resp = c.get(url)
    assert resp.status_code == 404
    assert c._session.calls == 1
    assert sleeps == []


# ── 跨线程/跨进程限频（2026-10-17，MCP 多进程并发实测 429 后修复）────


def test_rate_limit_shared_across_instances(tmp_path, monkeypatch):
    """限频状态写磁盘文件（flock 互斥）：第二个实例的首次请求也补足间隔。"""
    monkeypatch.setattr(base_mod, "CACHE_DIR", tmp_path)
    sleeps: list[float] = []
    monkeypatch.setattr(base_mod.time, "sleep", lambda s: sleeps.append(s))

    class _SlowClient(RateLimitedClient):
        service_name = "rl_shared"
        rps = 1.0

    a = _SlowClient()
    b = _SlowClient()
    a._rate_limit()
    assert sleeps == []                     # 全局限速窗口内首次请求无等待
    b._rate_limit()                         # 与 a 相距约 0s -> 补足 ~1s 间隔
    assert len(sleeps) == 1 and sleeps[0] > 0.9
    assert (tmp_path / ".ratelimit" / "rl_shared.json").exists()


def test_retry_exhausted_429_hints_retry(tmp_path, monkeypatch):
    """429 重试耗尽的报错带「safe to retry later」，供调用方判断可重试。"""
    c, _sleeps, url = _make_retry_client(
        tmp_path, monkeypatch, [429], "https://api.example/r429"
    )
    with pytest.raises(base_mod.RetriesExhaustedError, match="safe to retry later"):
        c.get(url)


def test_retry_transport_error_then_success(tmp_path, monkeypatch):
    """传输层瞬断（ChunkedEncodingError）按 429/5xx 同样的退避重试，不直接报废。"""
    monkeypatch.setattr(base_mod, "CACHE_DIR", tmp_path)

    class _FlakySession:
        def __init__(self):
            self.calls = 0

        def request(self, method, url, timeout=None, **kw):
            self.calls += 1
            if self.calls <= 2:
                raise requests.exceptions.ChunkedEncodingError("Response ended prematurely")
            r = requests.Response()
            r.status_code = 200
            r._content = b"{}"
            r.headers["Content-Type"] = "application/json"
            r.encoding = "utf-8"
            return r

    c = _Client()
    c._session = _FlakySession()
    sleeps: list[float] = []
    monkeypatch.setattr(base_mod.time, "sleep", lambda s: sleeps.append(s))
    resp = c.get("https://api.example/flaky")
    assert resp.status_code == 200
    assert c._session.calls == 3
    backoffs = [s for s in sleeps if s >= 1.0]
    assert backoffs == [2.0, 4.0]


def test_retry_exhausted_redacts_query_api_key(tmp_path, monkeypatch):
    """重试耗尽的异常信息不得带出查询串中的 API key。"""
    c, _sleeps, url = _make_retry_client(
        tmp_path,
        monkeypatch,
        [429],
        "https://api.openalex.org/works?search=x&api_key=SECRET_TEST_KEY",
    )
    with pytest.raises(base_mod.RetriesExhaustedError) as excinfo:
        c.get(url)
    message = str(excinfo.value)
    assert "SECRET_TEST_KEY" not in message
    assert "api_key=***" in message


def test_retry_exhausted_redacts_transport_error_url(tmp_path, monkeypatch):
    """传输异常重试耗尽后，异常文本中的 URL 同样脱敏。"""
    monkeypatch.setattr(base_mod, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(base_mod.time, "sleep", lambda s: None)

    class _FailingSession:
        def request(self, method, url, timeout=None, **kw):
            raise requests.exceptions.ConnectionError(
                "connection failed for https://api.openalex.org/works?api_key=SECRET_TEST_KEY"
            )

    c = _Client()
    c._session = _FailingSession()
    with pytest.raises(base_mod.RetriesExhaustedError) as excinfo:
        c.get("https://api.openalex.org/works?api_key=SECRET_TEST_KEY")
    message = str(excinfo.value)
    assert "SECRET_TEST_KEY" not in message
    assert "api_key=***" in message
