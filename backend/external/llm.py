"""通用 LLM client — 翻译/总结（OpenAI Chat Completions 兼容端点）。

3.3 起翻译/总结不再绑定 Volces：任何兼容 OpenAI chat/completions 协议的
服务端点（Volces ark / DeepSeek / Kimi / OpenRouter / vLLM / Ollama 等）
均可接入。配置项（config.toml [llm]）：
  - api_url：完整 chat/completions URL（含路径，如 https://api.example.com/v1/chat/completions）
  - model：模型名
  - temperature：默认采样温度
API Key 走 CONFIG["secrets"]["llm_api_key"]（.env 的 LLM_API_KEY）。

调用方约束（红线 4）：翻译必须进程内直调 backend.pipeline.translator，
禁止 shell 包装；timeout=180 与 urllib3 重试沿用旧 volces.py 写法。

日志留存（2026-10-17）：每次调用在 finally 里向 llm_calls 表落一行——
provider 的 usage（prompt/completion tokens）、字符数、时延、成败、调用方
上下文（client.call_meta：kind / paper_id / task_id，由 summary_worker /
translate_orchestrator 注入）。写库失败只吞异常不打断调用本身。
"""
import json
import time
from typing import Optional

import requests
from requests.adapters import HTTPAdapter, Retry

from backend.config import CONFIG


class LLMClient:
    """OpenAI Chat Completions 兼容客户端（翻译/总结）。"""

    def __init__(self) -> None:
        cfg = CONFIG["llm"]
        self.api_url = cfg["api_url"]
        self.model = cfg["model"]
        self.temperature = cfg["temperature"]
        self.api_key = CONFIG["secrets"]["llm_api_key"]
        if not self.api_key:
            raise RuntimeError("LLM_API_KEY missing in env (.env)")
        # 调用方注入的上下文（llm_calls.kind / paper_id / task_id），见模块 docstring
        self.call_meta: dict = {}
        self._session = requests.Session()
        retries = Retry(total=3, backoff_factor=2, allowed_methods=["POST"],
                        status_forcelist=[429, 500, 502, 503])
        self._session.mount("https://", HTTPAdapter(max_retries=retries))

    def chat(
        self,
        system_prompt: str,
        user_message: str,
        temperature: Optional[float] = None,
        timeout: int = 180,
    ) -> str:
        body = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            "temperature": temperature if temperature is not None else self.temperature,
        }
        t0 = time.time()
        data: dict = {}
        err: Optional[str] = None
        try:
            resp = self._session.post(
                self.api_url,
                headers={"Authorization": f"Bearer {self.api_key}",
                         "Content-Type": "application/json"},
                data=json.dumps(body, ensure_ascii=False),
                timeout=timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError) as e:
            err = f"unexpected response: {json.dumps(data, ensure_ascii=False)[:200]}"
            raise RuntimeError(f"LLM {err}") from e
        except Exception as e:  # noqa: BLE001
            err = str(e)[:300]
            raise
        finally:
            _log_call(
                model=self.model,
                system_prompt=system_prompt,
                user_message=user_message,
                data=data,
                latency_ms=int((time.time() - t0) * 1000),
                ok=err is None,
                error=err,
                meta=self.call_meta,
            )
        return content


def _log_call(*, model: str, system_prompt: str, user_message: str,
              data: dict, latency_ms: int, ok: bool, error: Optional[str],
              meta: dict) -> None:
    """每次 chat 调用向 llm_calls 落一行。日志失效绝不反噬调用（吞异常）。"""
    usage = (data or {}).get("usage") or {}
    # provider prompt cache 命中量：DeepSeek 系为 prompt_cache_hit_tokens，部分
    # 网关回 prompt_cache_tokens；均无则 NULL（费用按非缓存价计）。
    cached = (usage.get("prompt_cache_hit_tokens")
              or usage.get("prompt_cache_tokens"))
    try:
        from backend.db.conn import get_conn
        conn = get_conn()
        try:
            conn.execute(
                """INSERT INTO llm_calls
                   (kind, paper_id, task_id, model, prompt_chars, completion_chars,
                    prompt_tokens, completion_tokens, cached_tokens, latency_ms, ok, error)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    meta.get("kind"),
                    meta.get("paper_id"),
                    meta.get("task_id"),
                    model,
                    len(system_prompt) + len(user_message),
                    len((data.get("choices") or [{}])[0].get("message", {}).get("content") or "")
                    if data else None,
                    usage.get("prompt_tokens"),
                    usage.get("completion_tokens"),
                    cached,
                    latency_ms,
                    1 if ok else 0,
                    error,
                ),
            )
        finally:
            conn.close()
    except Exception:  # noqa: BLE001,S110
        pass
