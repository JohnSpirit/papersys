"""Sciverse REST API client。

直接走 REST（不走 MCP），更易在 Flask 进程内调用。
限频：60 req/min。
"""
from __future__ import annotations

from typing import Optional

from backend.external.base import RateLimitedClient
from backend.config import CONFIG


class SciverseClient(RateLimitedClient):
    service_name = "sciverse"

    @property
    def rps(self) -> float:  # type: ignore[override]
        return float(CONFIG["external"]["sciverse_rpm"]) / 60.0

    def __init__(self) -> None:
        super().__init__()
        self._api_key = CONFIG["secrets"].get("sciverse_api_token", "")
        if self._api_key:
            self._session.headers.update({"Authorization": f"Bearer {self._api_key}"})

    def semantic_search(self, query: str, *, top_k: int = 10) -> list[dict]:
        body = {"query": query[:4096], "top_k": min(top_k, 100), "sub_queries": 0}
        resp = self.post("https://api.sciverse.space/agentic-search", json=body, timeout=30)
        if resp.status_code != 200:
            return []
        hits = (resp.json() or {}).get("hits") or []
        return [_to_record(h) for h in hits]

    def meta_search(
        self,
        query: Optional[str] = None,
        *,
        page_size: int = 20,
        year_from: Optional[int] = None,
        year_to: Optional[int] = None,
    ) -> list[dict]:
        filters: list[dict] = []
        if year_from is not None:
            filters.append({"field": "publication_published_year", "operator": "FILTER_OP_GTE", "value": year_from})
        if year_to is not None:
            filters.append({"field": "publication_published_year", "operator": "FILTER_OP_LTE", "value": year_to})
        body: dict = {
            "page": 1,
            "page_size": min(page_size, 200),
            "fields": ["title", "doi", "publication_published_year", "abstract", "authors",
                       "citation_count", "publication_journal_name"],
        }
        if query:
            body["query"] = query
        else:
            body["sort"] = [{"field": "citation_count", "order": "SORT_ORDER_DESC"}]
        if filters:
            body["filters"] = filters

        resp = self.post("https://api.sciverse.space/meta-search", json=body, timeout=30)
        if resp.status_code != 200:
            return []
        records = (resp.json() or {}).get("records") or (resp.json() or {}).get("items") or []
        return [_meta_to_record(r) for r in records]


def _to_record(h: dict) -> dict:
    return {
        "source":    "sciverse",
        "id":        h.get("doc_id") or h.get("chunk_id"),
        "title":     h.get("title") or "",
        "abstract":  h.get("abstract") or "",
        "snippet":   h.get("chunk") or "",
        "page_no":   h.get("page_no"),
        "score":     h.get("score"),
        "url":       h.get("url"),
        "pdf_url":   None,
        "doi":       h.get("doi"),
        "arxiv_id":  None,
        "authors":   h.get("authors") or [],
        "year":      h.get("publication_published_year") or h.get("year"),
        "journal":   h.get("publication_journal_name") or h.get("journal"),
    }


def _meta_to_record(r: dict) -> dict:
    return {
        "source":    "sciverse",
        "id":        r.get("doc_id") or r.get("id"),
        "title":     r.get("title") or "",
        "authors":   r.get("authors") or [],
        "year":      r.get("publication_published_year"),
        "abstract":  r.get("abstract") or "",
        "doi":       r.get("doi"),
        "arxiv_id":  None,
        "url":       r.get("url"),
        "pdf_url":   None,
        "journal":   r.get("publication_journal_name"),
        "cited_by_count": r.get("citation_count"),
    }