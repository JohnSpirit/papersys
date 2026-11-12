"""Search API — 多源文献搜索。

POST /api/search   body: {source, query, limit, year_from, year_to}
  source ∈ {arxiv, openalex, semantic_scholar, sciverse}
  query 为裸 DOI（或 doi.org URL / doi: 前缀）时，semantic_scholar/openalex
  两个源走精确 DOI 解析而非关键词检索（S2 的 /paper/search 不解析裸 DOI，
  实测 count=0）。

POST /api/inbox/from_url  body: {url}
  下载 PDF 到 inbox，创建 ingest 任务（下载逻辑在 services/pdf_download.py，
  与 MCP ingest_by_url 共用：协议白名单 / 内网地址拦截 / 大小上限 / 魔数校验）。
"""
from __future__ import annotations

import re

from flask import Blueprint, abort, jsonify, request

from backend.config import ensure_inbox_dir
from backend.db import tasks_repo
from backend.services.pdf_download import (
    DownloadError,
    UrlValidationError,
    stage_pdf_from_url,
)

bp = Blueprint("search", __name__, url_prefix="/api")

# 裸 DOI 形态：10.xxxx/...，容忍 doi.org / doi: 前缀；句尾标点后剥
_DOI_RE = re.compile(
    r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)?(10\.\d{4,9}/\S+)$",
    re.IGNORECASE,
)


def _match_doi(q: str) -> str | None:
    m = _DOI_RE.match(q.strip())
    if not m:
        return None
    return m.group(1).rstrip(".,;)")


def _search_arxiv(q: str, limit: int, year_from, year_to) -> list[dict]:
    from backend.external.arxiv import ArxivClient
    items = ArxivClient().search(q, max_results=limit)
    if year_from is not None:
        items = [it for it in items if it.get("year") and it["year"] >= year_from]
    if year_to is not None:
        items = [it for it in items if it.get("year") and it["year"] <= year_to]
    return items


class SearchItems(list):
    """检索结果列表，附带实际尝试过的数据源。"""

    def __init__(self, items=(), sources_tried=None):
        super().__init__(items)
        self.sources_tried = list(sources_tried or [])


def _crossref_to_search_record(item: dict, fallback_doi: str = "") -> dict:
    from backend.external.crossref import _item_title, _item_year, strip_jats
    doi = item.get("DOI") or fallback_doi or None
    container = item.get("container-title") or []
    return {
        "source": "crossref",
        "id": doi,
        "title": _item_title(item),
        "authors": [
            " ".join(x for x in (a.get("given"), a.get("family")) if x)
            or a.get("name", "")
            for a in (item.get("author") or [])
        ],
        "year": _item_year(item),
        "abstract": strip_jats(item.get("abstract")),
        "doi": doi,
        "arxiv_id": None,
        "url": f"https://doi.org/{doi}" if doi else item.get("URL"),
        "pdf_url": None,
        "journal": container[0] if container else None,
        "cited_by_count": item.get("is-referenced-by-count"),
    }


def _search_openalex(q: str, limit: int, year_from, year_to) -> list[dict]:
    from backend.external.openalex import OpenAlexClient, _work_to_search_record
    cli = OpenAlexClient()
    doi = _match_doi(q)
    if doi:
        tried = ["openalex"]
        w = cli.get_work(doi)
        if w:
            return SearchItems([_work_to_search_record(w)], tried)
        from backend.external.crossref import CrossrefClient
        tried.append("crossref")
        item = CrossrefClient().get_work(doi)
        return SearchItems([_crossref_to_search_record(item, doi)] if item else [], tried)
    return SearchItems(
        cli.search_papers(q, per_page=limit, year_from=year_from, year_to=year_to),
        ["openalex"],
    )


def _search_ss(q: str, limit: int, year_from, year_to) -> list[dict]:
    from backend.external.semantic_scholar import SemanticScholarClient
    cli = SemanticScholarClient()
    doi = _match_doi(q)
    if doi:
        tried = ["semantic_scholar"]
        rec = cli.by_doi(doi)
        if rec:
            return SearchItems([rec], tried)
        from backend.external.openalex import OpenAlexClient, _work_to_search_record
        tried.append("openalex")
        work = OpenAlexClient().get_work(doi)
        if work:
            return SearchItems([_work_to_search_record(work)], tried)
        from backend.external.crossref import CrossrefClient
        tried.append("crossref")
        item = CrossrefClient().get_work(doi)
        return SearchItems([_crossref_to_search_record(item, doi)] if item else [], tried)
    year_filter = None
    if year_from or year_to:
        yf = year_from or 1900
        yt = year_to or 2100
        year_filter = f"{yf}-{yt}"
    return SearchItems(cli.search(q, limit=limit, year=year_filter), ["semantic_scholar"])


def compact_search_items(items: list[dict], brief: bool) -> list[dict]:
    """brief=True 时去掉摘要与 tldr，保留定位与核对所需字段。"""
    if not brief:
        return list(items)
    return [
        {k: v for k, v in item.items() if k not in ("abstract", "tldr")}
        for item in items
    ]


def _search_sciverse(q: str, limit: int, year_from, year_to) -> list[dict]:
    from backend.external.sciverse import SciverseClient
    cli = SciverseClient()
    # 优先用 meta-search（结构化）
    try:
        items = cli.meta_search(query=q, page_size=limit, year_from=year_from, year_to=year_to)
        if items:
            return items
    except Exception:
        pass
    # 回退到 semantic_search
    return cli.semantic_search(q, top_k=limit)


_DISPATCHERS = {
    "arxiv":            _search_arxiv,
    "openalex":         _search_openalex,
    "semantic_scholar": _search_ss,
    "sciverse":         _search_sciverse,
}


@bp.post("/search")
def search():
    body = request.get_json(silent=True) or {}
    source = (body.get("source") or "arxiv").lower()
    query = (body.get("query") or "").strip()
    if not query:
        abort(400, "query required")
    if source not in _DISPATCHERS:
        abort(400, f"unknown source; must be one of {list(_DISPATCHERS)}")

    try:
        limit = min(int(body.get("limit", 20)), 100)
        year_from = int(body["year_from"]) if body.get("year_from") else None
        year_to = int(body["year_to"]) if body.get("year_to") else None
    except (TypeError, ValueError):
        abort(400, "invalid number parameter")

    try:
        items = _DISPATCHERS[source](query, limit, year_from, year_to)
    except Exception as e:  # noqa: BLE001
        return jsonify({"source": source, "items": [], "count": 0, "error": str(e)}), 502

    sources_tried = getattr(items, "sources_tried", [source])
    items = compact_search_items(items, bool(body.get("brief")))
    return jsonify({
        "source": source, "sources_tried": sources_tried,
        "items": items, "count": len(items),
    })


@bp.post("/semantic_search")
def semantic_search_ep():
    """库内语义检索（RAG）。body: {query, topk?, paper_id?}"""
    from backend.services.semantic_search import semantic_search as _ss
    body = request.get_json(silent=True) or {}
    query = (body.get("query") or "").strip()
    if not query:
        abort(400, "query required")
    try:
        topk = min(int(body.get("topk", 5)), 50)
    except (TypeError, ValueError):
        abort(400, "invalid topk")
    paper_id = body.get("paper_id")
    if paper_id:
        try:
            paper_id = int(paper_id)
        except (TypeError, ValueError):
            abort(400, "invalid paper_id")
    else:
        paper_id = None
    return jsonify(_ss(query, topk=topk, paper_id=paper_id))


# ── PDF ingest from URL ────────────────────────────────


@bp.post("/inbox/from_url")
def inbox_from_url():
    from backend.pipeline.steps import normalize_feed_steps
    body = request.get_json(silent=True) or {}
    url = (body.get("url") or "").strip()
    filename = (body.get("filename") or "").strip()
    if not url:
        abort(400, "url required")
    try:
        steps = normalize_feed_steps(body.get("steps"))
    except ValueError as e:
        abort(400, str(e))

    inbox = ensure_inbox_dir()
    try:
        target, size = stage_pdf_from_url(url, inbox, filename)
    except UrlValidationError as e:
        abort(400, str(e))
    except DownloadError as e:
        abort(502, str(e))

    tid = tasks_repo.create_task(
        kind="pipeline", payload={"inbox_path": str(target), "steps": steps},
    )
    return jsonify({"path": str(target), "size": size, "task_id": tid, "steps": steps})