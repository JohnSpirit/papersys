"""限频 + 磁盘缓存 基础 client。所有外部 API client 继承此基类。"""
import fcntl
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Optional

import requests

from backend.config import CONFIG
from backend.services.redaction import redact_secrets


CACHE_DIR = Path(CONFIG["paths"]["cache_dir"])
CACHE_TTL = CONFIG["external"]["cache_ttl_hours"] * 3600

# 429/5xx 指数退避重试（AGENTS 约定：min(60, 2**retry)，重试 3 次后 raise）。
# 4xx（404/422 等）不可重试，直接返回响应由调用方降级处理。
RETRYABLE_STATUS = {429, 500, 502, 503, 504}
MAX_RETRIES = 3


class RetriesExhaustedError(RuntimeError):
    """429/5xx 在重试 3 次后仍失败。调用方（worker 有 try/except，Flask 端点
    有全局处理器）按异常处理；此前单次请求直接返回非 200 响应，偶发限频
    会白白失败（Crossref 并发高峰实测：单篇重放即成功）。"""


class RateLimitedClient:
    """带令牌桶限频 + 磁盘缓存的 HTTP client。

    子类设置：
      service_name      — 缓存子目录名
      rps               — 每秒最大请求数
    """

    service_name: str = "generic"
    rps: float = 1.0

    def __init__(self) -> None:
        self._session = requests.Session()
        self._cache_dir = CACHE_DIR / self.service_name
        self._cache_dir.mkdir(parents=True, exist_ok=True)

    def _rate_limit(self) -> None:
        """令牌桶 sleep：跨线程且跨进程串行（2026-10-17 修订）。

        旧实现用实例级 _last_req：同一 MCP 进程内线程池并发调用、以及并存的
        多个 paper mcp 进程各自限速，叠加后突破 1 req/s 被 Semantic Scholar
        打 429（会话记录实测两次）。现在同一 service 共享磁盘状态文件，
        fcntl.flock 互斥：持锁期间完成「读上次请求时刻 -> sleep 补足间隔 ->
        写入新时刻」，后来线程/进程排队，等效全局令牌桶。CACHE_DIR 在调用时
        读模块属性（测试 monkeypatch base.CACHE_DIR 生效）。
        """
        gap = 1.0 / max(self.rps, 0.1)
        rl_dir = CACHE_DIR / ".ratelimit"
        rl_dir.mkdir(parents=True, exist_ok=True)
        lock_path = rl_dir / f"{self.service_name}.lock"
        state_path = rl_dir / f"{self.service_name}.json"
        with open(lock_path, "a+b") as lf:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
            try:
                last = 0.0
                try:
                    last = float(json.loads(state_path.read_text())["last"])
                except Exception:
                    pass
                wait = gap - (time.time() - last)
                if wait > 0:
                    time.sleep(wait)
                state_path.write_text(json.dumps({"last": time.time()}))
            finally:
                fcntl.flock(lf.fileno(), fcntl.LOCK_UN)

    def _cache_get(self, key: str) -> Optional[Any]:
        path = self._cache_dir / f"{key}.json"
        if not path.exists():
            return None
        if time.time() - path.stat().st_mtime > CACHE_TTL:
            path.unlink(missing_ok=True)
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            # 进程在写缓存途中被杀会留下半文件；损坏缓存按未命中处理并删除，
            # 不让单次 enrich/检索任务因历史缓存而永久失败。
            path.unlink(missing_ok=True)
            return None

    def _cache_set(self, key: str, data: Any) -> None:
        path = self._cache_dir / f"{key}.json"
        path.write_text(
            json.dumps(data, ensure_ascii=False, default=str), encoding="utf-8"
        )

    def _cache_key(self, *parts: str) -> str:
        raw = ":".join(parts)
        return hashlib.sha1(raw.encode()).hexdigest()

    def _request(
        self,
        method: str,
        url: str,
        **kwargs,
    ) -> requests.Response:
        # key 必须涵盖全部影响响应的数据参数：params（URL 查询）与 json（POST 体）。
        # 只用 params 时，Sciverse 这类"固定 URL + json body"的 POST 不同查询会撞
        # 同一 key，首个查询的缓存被后续所有查询命中（3.1.0b3 全库审查发现）。
        payload = {"params": kwargs.get("params", {}), "json": kwargs.get("json")}
        cache_key = self._cache_key(
            method, url, json.dumps(payload, sort_keys=True, default=str)
        )
        cached = self._cache_get(cache_key)
        if cached is not None:
            # 模拟 Response 对象
            resp = requests.Response()
            resp.status_code = 200
            resp._content = json.dumps(cached).encode()
            resp.headers["Content-Type"] = "application/json"
            resp.encoding = "utf-8"
            resp.from_cache = True  # type: ignore
            return resp

        timeout = kwargs.pop("timeout", 30)
        for attempt in range(MAX_RETRIES + 1):
            self._rate_limit()
            try:
                resp = self._session.request(method, url, timeout=timeout, **kwargs)
            except (requests.exceptions.ConnectionError,
                    requests.exceptions.ChunkedEncodingError,
                    requests.exceptions.Timeout) as exc:
                # 传输层瞬断（对端截断/连接重置/超时）与 5xx 同样可重试
                # （2026-10-17 全库 citation 补跑在 40% 处被 ChunkedEncodingError
                # 打断：此前仅按状态码重试，传输错误直接报废整个任务）
                if attempt == MAX_RETRIES:
                    raise RetriesExhaustedError(
                        f"{method} {redact_secrets(url)} -> "
                        f"{type(exc).__name__}: {redact_secrets(str(exc))}"
                    ) from None
                time.sleep(min(60.0, 2.0 ** (attempt + 1)))
                continue
            if resp.status_code not in RETRYABLE_STATUS:
                break
            if attempt == MAX_RETRIES:
                hint = ("; rate limited, safe to retry later"
                        if resp.status_code == 429 else "")
                raise RetriesExhaustedError(
                    f"{method} {redact_secrets(url)} -> {resp.status_code} "
                    f"after {MAX_RETRIES} retries{hint}"
                )
            time.sleep(min(60.0, 2.0 ** (attempt + 1)))

        if resp.ok:
            try:
                self._cache_set(cache_key, resp.json())
            except Exception:
                pass
        return resp

    def get(self, url: str, **kwargs) -> requests.Response:
        return self._request("GET", url, **kwargs)

    def post(self, url: str, **kwargs) -> requests.Response:
        return self._request("POST", url, **kwargs)