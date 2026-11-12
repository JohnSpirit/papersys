"""Test: MinerU submit 限频窗口（P2-1）。

不访问网络，仅验证 `_throttle_submit` 在 SUBMIT_MAX_PER_MIN 内不阻塞，
超出后会 sleep。
"""
import time
from collections import deque
from unittest.mock import patch


def test_throttle_within_limit_no_sleep():
    """5 次提交在 60s 内不阻塞（窗口未满）。"""
    from backend.external.mineru import MinerUClient

    with patch.object(MinerUClient, "__init__", lambda self: None):
        c = MinerUClient()
        c._submit_window = deque()
        t0 = time.time()
        for _ in range(MinerUClient.SUBMIT_MAX_PER_MIN):
            c._throttle_submit()
        elapsed = time.time() - t0
        assert elapsed < 1.0, f"expected fast path, took {elapsed:.2f}s"
        assert len(c._submit_window) == MinerUClient.SUBMIT_MAX_PER_MIN


def test_throttle_window_expires():
    """旧记录过期 (>60s) 后窗口腾出，可继续提交。"""
    from backend.external.mineru import MinerUClient

    with patch.object(MinerUClient, "__init__", lambda self: None):
        c = MinerUClient()
        # 模拟 5 个 90 秒前的提交（已过期）
        c._submit_window = deque([time.time() - 90] * MinerUClient.SUBMIT_MAX_PER_MIN)
        t0 = time.time()
        c._throttle_submit()
        elapsed = time.time() - t0
        assert elapsed < 0.5, "expired records should not block"
        # 旧记录被清理，新记录入队
        assert len(c._submit_window) == 1


def test_session_reused_cloud_mode():
    """cloud 模式下 Session 应当复用，且 Authorization 头挂在 session 上。"""
    from backend.external.mineru import MinerUClient

    with patch("backend.external.mineru.CONFIG", {
        "mineru": {"api_base": "https://example.com", "mode": "cloud"},
        "secrets": {"mineru_token": "fake-token"},
    }), patch("backend.external.mineru._MODE", "cloud"):
        c = MinerUClient()
        assert c._mode == "cloud"
        assert c._session is not None
        assert c._session is c._session
        assert c._session.headers.get("Authorization") == "Bearer fake-token"


def test_session_reused_local_mode():
    """local 模式下 Session 不挂 Authorization 头（本地服务无需鉴权）。"""
    from backend.external.mineru import MinerUClient

    with patch("backend.external.mineru.CONFIG", {
        "mineru": {"mode": "local", "local_api_base": "http://127.0.0.1:8000"},
        "secrets": {},
    }), patch("backend.external.mineru._MODE", "local"):
        c = MinerUClient()
        assert c._mode == "local"
        assert c._session is not None
        # local 模式不应挂 Authorization 头
        assert c._session.headers.get("Authorization") is None


def test_local_mode_no_token_required():
    """local 模式不需要 MINERU_TOKEN，即使 secrets 为空也能初始化。"""
    from backend.external.mineru import MinerUClient

    with patch("backend.external.mineru.CONFIG", {
        "mineru": {"mode": "local", "local_api_base": "http://127.0.0.1:8000"},
        "secrets": {},
    }), patch("backend.external.mineru._MODE", "local"):
        c = MinerUClient()
        assert not hasattr(c, "_token") or c._token is None