"""CitationWorker — 从 OpenAlex 拉引文网络，写 citations 表。

payload: {"paper_id": int} 或 {} 表示全量重建。

每个 paper 的 referenced_works（它引用了谁）被存入 citations 表。
同时也在 papers.openalex_id 上查 inside-library 引用。
"""
import json
import time
from typing import Optional

from backend.db.conn import get_conn
from backend.db.repo import get_paper
from backend.external.openalex import OpenAlexClient
from backend.workers.runner import register_worker, WorkerContext


@register_worker("citation")
def citation_worker(ctx: WorkerContext, payload: dict) -> None:
    paper_id = payload.get("paper_id")
    rebuild = payload.get("rebuild", False)

    client = OpenAlexClient()

    if rebuild or not paper_id:
        # 全量重建
        conn = get_conn()
        try:
            if rebuild:
                conn.execute("DELETE FROM citations")
                ctx.info("cleared all citations for rebuild")
            papers = conn.execute(
                "SELECT id, dir_name, openalex_id, doi, title, year FROM papers WHERE openalex_id IS NOT NULL OR doi IS NOT NULL"
            ).fetchall()
            papers = [dict(r) for r in papers]
        finally:
            conn.close()
    else:
        conn = get_conn()
        try:
            paper = get_paper(conn, paper_id)
        finally:
            conn.close()
        if not paper:
            raise ValueError(f"paper_id={paper_id} not found")
        papers = [paper]

    ctx.info(f"processing {len(papers)} papers")
    total = len(papers)
    done_count = 0

    for p in papers:
        wid = p.get("openalex_id", None)
        doi = p.get("doi", None)
        pid = p["id"]

        # 清除旧引用
        conn = get_conn()
        try:
            conn.execute("DELETE FROM citations WHERE src_paper_id = ?", (pid,))
            if not rebuild and not paper_id:
                conn.execute("DELETE FROM citations WHERE dst_paper_id = ?", (pid,))
        finally:
            conn.close()

        work: dict = {}
        if wid:
            work = client.get_work_by_openalex_id(wid)
        elif doi:
            work = client.get_work(doi)

        if not work:
            ctx.warn(f"no work: {p.get('dir_name')}")
            done_count += 1
            ctx.progress(done_count / total)
            continue

        ref_ids = work.get("referenced_works") or []
        if not ref_ids:
            ctx.info(f"no refs: {p.get('dir_name')}")
            done_count += 1
            ctx.progress(done_count / total)
            continue

        # 批量获取引用论文信息
        ref_details = client.get_references_batch(ref_ids)

        # 写 citations 表 + 至少存 dst_openalex_id
        conn = get_conn()
        try:
            conn.execute("BEGIN")
            for rid in ref_ids:
                info = ref_details.get(rid) or {}
                dst_doi = (info.get("doi") or "").replace("https://doi.org/", "")
                conn.execute(
                    """INSERT OR IGNORE INTO citations
                       (src_paper_id, dst_openalex_id, dst_title, dst_year, edge_type)
                       VALUES (?, ?, ?, ?, 'references')""",
                    (
                        pid,
                        rid,
                        info.get("title") or None,
                        info.get("publication_year"),
                    ),
                )
                # 如果引用论文也在库里，也写 dst_paper_id
                if rid:
                    inner = conn.execute(
                        "SELECT id FROM papers WHERE openalex_id = ? OR ? LIKE '%' || openalex_id",
                        (rid, rid),
                    ).fetchone()
                    if inner:
                        conn.execute(
                            "UPDATE citations SET dst_paper_id = ? WHERE src_paper_id = ? AND dst_openalex_id = ?",
                            (inner["id"], pid, rid),
                        )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

        ctx.info(f"done: {p.get('dir_name')} ({len(ref_ids)} refs)")
        done_count += 1
        ctx.progress(done_count / total)
        time.sleep(0.3)

    ctx.info(f"citation network complete: {total} papers")