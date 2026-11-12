"""Papers API：列表/详情/统计。"""
from flask import Blueprint, jsonify, request, abort

from backend.db.conn import get_conn
from backend.db.repo import (
    list_papers,
    list_papers_by_doi,
    count_papers,
    get_paper,
    stats as repo_stats,
)

bp = Blueprint("papers", __name__, url_prefix="/api")


@bp.get("/papers")
def papers_list():
    args = request.args
    try:
        limit = min(int(args.get("limit", 200)), 1000)
        offset = int(args.get("offset", 0))
        year_from = int(args["year_from"]) if args.get("year_from") else None
        year_to = int(args["year_to"]) if args.get("year_to") else None
    except ValueError:
        abort(400, "invalid integer parameter")

    status_filter = {
        k: v for k, v in args.items()
        if k.startswith("status_") and v
    }

    conn = get_conn()
    try:
        items = list_papers(
            conn,
            q=args.get("q"),
            author=args.get("author"),
            fulltext=args.get("fulltext"),
            year_from=year_from,
            year_to=year_to,
            status_filter=status_filter or None,
            limit=limit,
            offset=offset,
            order_by=args.get("order_by", "year_desc"),
        )
        # count 是过滤后的总条数（非当前页条数）：前端分页器用它算总页数，
        # 返回 len(items) 会在库超过单页上限时把分页锁死在第 1 页
        total = count_papers(
            conn,
            q=args.get("q"),
            author=args.get("author"),
            fulltext=args.get("fulltext"),
            year_from=year_from,
            year_to=year_to,
            status_filter=status_filter or None,
        )
    finally:
        conn.close()

    return jsonify({"items": items, "limit": limit, "offset": offset, "count": total})


@bp.get("/papers/by_doi")
def paper_by_doi():
    """按 DOI 精确匹配（大小写不敏感，容忍 doi.org/doi: 前缀）。
    无命中返回 200 + count=0（与 /api/papers?q= 空结果语义一致）。"""
    doi = (request.args.get("doi") or "").strip()
    if not doi:
        abort(400, "doi parameter required")
    conn = get_conn()
    try:
        items = list_papers_by_doi(conn, doi)
    finally:
        conn.close()
    return jsonify({"count": len(items), "items": items})


@bp.get("/papers/<int:paper_id>")
def paper_detail(paper_id: int):
    conn = get_conn()
    try:
        p = get_paper(conn, paper_id)
    finally:
        conn.close()
    if not p:
        abort(404)
    return jsonify(p)


@bp.get("/stats")
def stats():
    conn = get_conn()
    try:
        return jsonify(repo_stats(conn))
    finally:
        conn.close()
