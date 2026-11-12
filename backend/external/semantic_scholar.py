"""Semantic Scholar Graph API。

限频严格：1 req/s（带 API Key），未带 key 时共享桶慢。
"""
from __future__ import annotations

from typing import Optional

from backend.external.base import RateLimitedClient
from backend.config import CONFIG


class SemanticScholarClient(RateLimitedClient):
    service_name = "semantic_scholar"
    rps = float(CONFIG["external"]["semantic_scholar_rps"])

    def __init__(self) -> None:
        super().__init__()
        self._api_key = CONFIG["secrets"].get("semantic_scholar_key", "")
        if self._api_key:
            self._session.headers.update({"x-api-key": self._api_key})

    def search(self, query: str, *, limit: int = 20, year: Optional[str] = None) -> list[dict]:
        params = {
            "query": query,
            "limit": min(limit, 100),
            "fields": "title,authors,year,abstract,citationCount,influentialCitationCount,"
                      "externalIds,openAccessPdf,journal,tldr,publicationTypes",
        }
        if year:
            params["year"] = year
        resp = self.get("https://api.semanticscholar.org/graph/v1/paper/search", params=params, timeout=30)
        if resp.status_code != 200:
            return []
        data = resp.json()
        return [_to_record(p) for p in (data.get("data") or [])]

    def by_doi(self, doi: str) -> Optional[dict]:
        resp = self.get(
            f"https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}",
            params={"fields": "title,authors,year,abstract,citationCount,externalIds,openAccessPdf,journal,tldr"},
            timeout=30,
        )
        if resp.status_code != 200:
            return None
        return _to_record(resp.json())


def _to_record(p: dict) -> dict:
    if not p:
        return {}
    ext = p.get("externalIds") or {}
    pdf = (p.get("openAccessPdf") or {}).get("url")
    journal = p.get("journal") or {}
    tldr = (p.get("tldr") or {}).get("text") if isinstance(p.get("tldr"), dict) else None
    return {
        "source":         "semantic_scholar",
        "id":             p.get("paperId"),
        "title":          p.get("title") or "",
        "authors":        [a.get("name", "") for a in p.get("authors") or []],
        "year":           p.get("year"),
        "abstract":       p.get("abstract") or "",
        "tldr":           tldr,
        "doi":            ext.get("DOI"),
        "arxiv_id":       ext.get("ArXiv"),
        "pmid":           ext.get("PubMed"),
        "url":            f"https://www.semanticscholar.org/paper/{p.get('paperId')}" if p.get("paperId") else None,
        "pdf_url":        pdf,
        "journal":        journal.get("name") if isinstance(journal, dict) else None,
        "cited_by_count": p.get("citationCount"),
        "influential_citations": p.get("influentialCitationCount"),
    }