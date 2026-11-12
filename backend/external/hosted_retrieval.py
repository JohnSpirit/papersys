"""托管两阶段检索 client（Fireworks OpenAI 兼容端点）。

独立实现，流程设计参考 BrainPilot 的纯 HTTP 路径
（embedQueryHosted / rerankHosted），不含本地模型进程逻辑。

错误约定：配置缺失 / HTTP 失败抛 RuntimeError；调用方（service / MCP / doctor）
负责转为 {"error": ...}（工具错误约定：MCP 工具永不抛异常）。
429/5xx 指数退避沿用旧 Volces client 的 urllib3 Retry 写法。
"""
import json

import requests
from requests.adapters import HTTPAdapter, Retry

from backend.config import hosted_retrieval_config, retrieval_api_key

# 托管嵌入批量上限（沿用旧 Volces embed 配置的 10 条/批，Fireworks 兼容）
_EMBED_BATCH = 10


class HostedRetrievalClient:
    """托管嵌入 + 精排（查询与建库必须同模型、同维度）。"""

    def __init__(self) -> None:
        cfg, err = hosted_retrieval_config()
        if err:
            raise RuntimeError(err)
        self._cfg = cfg
        self._session = requests.Session()
        retries = Retry(total=3, backoff_factor=2, allowed_methods=["POST"],
                        status_forcelist=[429, 500, 502, 503])
        self._session.mount("https://", HTTPAdapter(max_retries=retries))

    @property
    def config(self) -> dict:
        return self._cfg

    def _post(self, url: str, body: dict, api_key: str, timeout: int) -> dict:
        resp = self._session.post(
            url,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            data=json.dumps(body, ensure_ascii=False),
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def embed(self, texts: list[str], timeout: int | None = None) -> list[list[float]]:
        """批量托管嵌入（OpenAI 兼容 /embeddings）。

        设了 embed_dimensions 时请求带 dimensions，且校验返回维度一致
        （查询与建库同维度是硬约束，不一致即报错而非静默错配）。
        单条输入按 embed_max_chars 截断（超长 chunk 会触发端点 400）。
        """
        api_key = retrieval_api_key("embed")
        if not api_key:
            raise RuntimeError(
                "hosted embed API key missing "
                "(BP_EMBED_API_KEY / FIREWORKS_API_KEY / ARK_API_KEY)"
            )
        dims = self._cfg["embed_dimensions"]
        max_chars = self._cfg["embed_max_chars"]
        results: list[list[float]] = []
        for i in range(0, len(texts), _EMBED_BATCH):
            # 显式截断：个别 chunk 无段落边界可超 130K 字符，超出模型上下文上限
            batch = [t[:max_chars] for t in texts[i:i + _EMBED_BATCH]]
            body: dict = {"model": self._cfg["embed_model"], "input": batch,
                          "encoding_format": "float"}
            if dims:
                body["dimensions"] = dims
            data = self._post(
                f"{self._cfg['embed_base_url']}/embeddings", body, api_key,
                timeout or self._cfg["embed_timeout"],
            )
            try:
                sorted_data = sorted(data["data"], key=lambda d: d["index"])
                vecs = [d["embedding"] for d in sorted_data]
            except (KeyError, IndexError, TypeError) as e:
                raise RuntimeError(
                    f"hosted embed unexpected response: {json.dumps(data, ensure_ascii=False)[:200]}"
                ) from e
            if dims and vecs and len(vecs[0]) != dims:
                raise RuntimeError(
                    f"embed dim mismatch: expected {dims}, got {len(vecs[0])}"
                )
            results.extend(vecs)
        return results

    def rerank(self, query: str, documents: list[str],
               timeout: int | None = None) -> list[dict]:
        """托管精排（/rerank）。返回 [{index, score}]，按 score 降序。

        score 为 relevance_score：qwen3-reranker-8b 返回有符号 logit（非 0~1），
        阈值语义见 config.toml [retrieval] rerank_threshold。
        文档输入按 rerank_max_chars 显式截断（paper chunk 上限 6000，
        不截断会推高 token 成本与超时风险）。
        """
        api_key = retrieval_api_key("rerank")
        if not api_key:
            raise RuntimeError(
                "hosted rerank API key missing "
                "(BP_RERANK_API_KEY / FIREWORKS_API_KEY / ARK_API_KEY)"
            )
        max_chars = self._cfg["rerank_max_chars"]
        docs = [d[:max_chars] for d in documents]
        data = self._post(
            f"{self._cfg['rerank_base_url']}/rerank",
            {
                "model": self._cfg["rerank_model"],
                "query": query,
                "documents": docs,
                "top_n": len(docs),
                "return_documents": False,
            },
            api_key,
            timeout or self._cfg["rerank_timeout"],
        )
        raw = data.get("data")
        if raw is None and isinstance(data.get("results"), list):
            raw = [{"index": r.get("index"), "relevance_score": r.get("score")}
                   for r in data["results"]]
        if not isinstance(raw, list):
            raise RuntimeError(
                f"rerank: unexpected response shape: {json.dumps(data, ensure_ascii=False)[:200]}"
            )
        out = [
            {"index": int(r["index"]), "score": float(r.get("relevance_score", 0.0))}
            for r in raw
            if isinstance(r, dict) and r.get("index") is not None and int(r["index"]) >= 0
        ]
        out.sort(key=lambda r: -r["score"])
        return out
