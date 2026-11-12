"""引文网络 API — 返回节点 + 边的图结构（Cytoscape-friendly）。"""
from flask import Blueprint, abort, jsonify, request

from backend.db import tasks_repo
from backend.db.conn import get_conn

bp = Blueprint("citations", __name__, url_prefix="/api")


@bp.get("/citations")
def citations_graph():
    """返回库内引文网络（仅包含 src_paper_id 和 dst_paper_id 都不为空的边）。

    参数：
      year_from / year_to：可选年份过滤
      include_external (0/1)：是否包含外部节点
    """
    args = request.args
    yf = int(args["year_from"]) if args.get("year_from") else None
    yt = int(args["year_to"]) if args.get("year_to") else None
    include_external = args.get("include_external", "0") == "1"

    conn = get_conn()
    try:
        # 节点：所有 papers
        sql = "SELECT id, dir_name, title, year, first_author, cited_by_count FROM papers"
        clauses = []
        params: list = []
        if yf is not None:
            clauses.append("year >= ?"); params.append(yf)
        if yt is not None:
            clauses.append("year <= ?"); params.append(yt)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        papers = conn.execute(sql, params).fetchall()

        nodes = [
            {
                "id":      f"p{p['id']}",
                "label":   p["dir_name"],
                "title":   p["title"],
                "year":    p["year"],
                "author":  p["first_author"],
                "citations": p["cited_by_count"],
                "in_library": True,
                "paper_id": p["id"],
            }
            for p in papers
        ]

        # 边：限制为 src/dst 都在库内
        edges_rows = conn.execute(
            """SELECT src_paper_id, dst_paper_id, dst_openalex_id, dst_title, dst_year
               FROM citations
               WHERE dst_paper_id IS NOT NULL"""
        ).fetchall()

        in_lib_ids = {p["id"] for p in papers}
        edges = []
        for e in edges_rows:
            if e["src_paper_id"] in in_lib_ids and e["dst_paper_id"] in in_lib_ids:
                edges.append({
                    "id":     f"e{e['src_paper_id']}_{e['dst_paper_id']}",
                    "source": f"p{e['src_paper_id']}",
                    "target": f"p{e['dst_paper_id']}",
                })

        external_nodes: list[dict] = []
        if include_external:
            # 取外部高频引用（被库内 N 篇引用的外部节点）
            ext_rows = conn.execute(
                """SELECT dst_openalex_id, dst_title, dst_year, COUNT(*) as inbound
                   FROM citations
                   WHERE dst_paper_id IS NULL AND dst_openalex_id IS NOT NULL
                   GROUP BY dst_openalex_id
                   HAVING inbound >= 3
                   ORDER BY inbound DESC
                   LIMIT 80"""
            ).fetchall()
            for r in ext_rows:
                external_nodes.append({
                    "id":      f"x{r['dst_openalex_id'].replace('https://openalex.org/', '')}",
                    "label":   (r["dst_title"] or "")[:40],
                    "title":   r["dst_title"],
                    "year":    r["dst_year"],
                    "in_library": False,
                    "inbound": r["inbound"],
                })

            # 添加边到外部节点
            ext_ids = {n["id"] for n in external_nodes}
            ext_edge_rows = conn.execute(
                """SELECT src_paper_id, dst_openalex_id
                   FROM citations
                   WHERE dst_paper_id IS NULL AND dst_openalex_id IS NOT NULL"""
            ).fetchall()
            for e in ext_edge_rows:
                xid = f"x{e['dst_openalex_id'].replace('https://openalex.org/', '')}"
                if e["src_paper_id"] in in_lib_ids and xid in ext_ids:
                    edges.append({
                        "id":     f"ex{e['src_paper_id']}_{xid}",
                        "source": f"p{e['src_paper_id']}",
                        "target": xid,
                        "external": True,
                    })

        return jsonify({
            "nodes": nodes + external_nodes,
            "edges": edges,
            "stats": {
                "library_nodes":  len(nodes),
                "external_nodes": len(external_nodes),
                "edges":          len(edges),
            },
        })
    finally:
        conn.close()


@bp.post("/citations/rebuild")
def citations_rebuild():
    """触发一次引文网络重建（全库）。"""
    paper_id = (request.get_json(silent=True) or {}).get("paper_id")
    payload = {"rebuild": True} if not paper_id else {"paper_id": paper_id}
    tid = tasks_repo.create_task(kind="citation", payload=payload)
    return jsonify({"task_id": tid})