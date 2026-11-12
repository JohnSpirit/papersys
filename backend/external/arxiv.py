"""arXiv REST API (Atom XML) — 关键词搜索。

限频：~1 req/3s
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Optional

from backend.external.base import RateLimitedClient
from backend.config import CONFIG


ATOM_NS = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}


class ArxivClient(RateLimitedClient):
    service_name = "arxiv"
    rps = float(CONFIG["external"]["arxiv_rps"])

    def search(
        self,
        query: str,
        *,
        max_results: int = 20,
        sort_by: str = "relevance",  # relevance | lastUpdatedDate | submittedDate
        sort_order: str = "descending",
    ) -> list[dict]:
        params = {
            "search_query": f"all:{query}" if ":" not in query else query,
            "max_results": max_results,
            "sortBy": sort_by,
            "sortOrder": sort_order,
        }
        resp = self.get("https://export.arxiv.org/api/query", params=params, timeout=30)
        if resp.status_code != 200:
            return []
        # 缓存层把响应当 JSON 缓存了 → 命中缓存时返回的是 list[dict]
        # 这里需要兼容
        if getattr(resp, "from_cache", False):
            try:
                import json as _json
                cached = _json.loads(resp.content)
                if isinstance(cached, list):
                    return cached
            except Exception:
                pass

        items = _parse_atom(resp.text)
        # 把解析结果写回缓存（用 list 覆盖 dict 行为）
        try:
            cache_key = self._cache_key("GET", "https://export.arxiv.org/api/query",
                                        __import__("json").dumps(params))
            self._cache_set(cache_key, items)
        except Exception:
            pass
        return items


def _parse_atom(xml_text: str) -> list[dict]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    out: list[dict] = []
    for entry in root.findall("atom:entry", ATOM_NS):
        title = (entry.findtext("atom:title", "", ATOM_NS) or "").strip()
        title = re.sub(r"\s+", " ", title)
        summary = (entry.findtext("atom:summary", "", ATOM_NS) or "").strip()
        summary = re.sub(r"\s+", " ", summary)
        published = entry.findtext("atom:published", "", ATOM_NS) or ""
        year = published[:4] if published else None

        authors = [
            (a.findtext("atom:name", "", ATOM_NS) or "").strip()
            for a in entry.findall("atom:author", ATOM_NS)
        ]
        arxiv_url = entry.findtext("atom:id", "", ATOM_NS) or ""
        arxiv_id = ""
        m = re.search(r"arxiv\.org/abs/([\d\.v]+)", arxiv_url)
        if m:
            arxiv_id = m.group(1)
        # 去除版本号
        arxiv_id_clean = re.sub(r"v\d+$", "", arxiv_id)

        pdf_url: Optional[str] = None
        for link in entry.findall("atom:link", ATOM_NS):
            if link.get("title") == "pdf" or link.get("type") == "application/pdf":
                pdf_url = link.get("href")
                break
        if not pdf_url and arxiv_id_clean:
            pdf_url = f"https://arxiv.org/pdf/{arxiv_id_clean}"

        primary_cat = ""
        pc = entry.find("arxiv:primary_category", ATOM_NS)
        if pc is not None:
            primary_cat = pc.get("term") or ""

        out.append({
            "source":     "arxiv",
            "id":         arxiv_id_clean or arxiv_url,
            "title":      title,
            "authors":    authors,
            "year":       int(year) if year and year.isdigit() else None,
            "abstract":   summary,
            "url":        arxiv_url,
            "pdf_url":    pdf_url,
            "arxiv_id":   arxiv_id_clean or None,
            "doi":        None,
            "category":   primary_cat,
        })
    return out