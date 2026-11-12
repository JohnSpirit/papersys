"""EnrichWorker：用 OpenAlex 富化论文元数据。

payload: {"paper_id": int}
"""
import json

from backend.db.conn import get_conn
from backend.db.repo import get_paper, upsert_paper, update_fts
from backend.external.openalex import OpenAlexClient
from backend.workers.runner import register_worker, WorkerContext


@register_worker("enrich")
def enrich_worker(ctx: WorkerContext, payload: dict) -> None:
    paper_id = int(payload["paper_id"])
    conn = get_conn()
    try:
        paper = get_paper(conn, paper_id)
    finally:
        conn.close()
    if not paper:
        raise ValueError(f"paper_id={paper_id} not found")
    _patch_status(paper_id, "processing")
    try:
        _enrich_run(ctx, paper, paper_id)
    except Exception:
        _patch_status(paper_id, "error")
        raise


def _enrich_run(ctx: WorkerContext, paper: dict, paper_id: int) -> None:
    oac = OpenAlexClient()
    work: dict = {}

    # ── 优先用 DOI ──
    if paper.get("doi"):
        ctx.info(f"querying OpenAlex by DOI {paper['doi']}")
        work = oac.get_work(paper["doi"])
    if not work and paper.get("title"):
        ctx.info(f"falling back to title search: {paper['title'][:60]}")
        work = oac.search_work(
            paper["title"],
            year=paper.get("year") or None,
            author=paper.get("first_author"),
        )
    if not work and paper.get("first_author") and paper.get("year"):
        ctx.info("falling back to author+year")
        work = oac.search_work_by_author_year(paper["first_author"], paper["year"])

    if not work:
        if paper.get("attachment_kind") == "si":
            # SI 附件不带独立元数据（页面无印刷 DOI、无独立标题）是常态，
            # enrich 无结果属预期：不标 manual_hold——semantic_search 会排除
            # 内容状态列为 manual_hold 的论文（3.1.1 降噪），而 SI 的价值恰在于被检索到。
            # 2026-10-17 sample2015 mmc1 实测：三层回退均未匹配。
            ctx.info("OpenAlex 三层回退后仍未匹配；SI 附件无独立元数据属预期，"
                     "跳过 manual_hold")
            _patch_status(paper_id, "ok")
        else:
            ctx.warn("OpenAlex 三层回退后仍未匹配，标记 manual_hold")
            _patch_status(paper_id, "manual_hold")
        return

    info = oac.extract_work_info(work)
    ctx.info(f"matched: {(info.get('title') or '')[:80]}")

    # 年份不一致单独告警：pdf_title 有效时现有校验只比标题，年份偏差会被
    # 静默保留（实证：入库年份 2020，OpenAlex/Crossref 均为 2026）。
    oa_year = info.get("pub_year")
    db_year = paper.get("year")
    if (
        paper.get("attachment_kind") != "si"
        and oa_year and db_year and abs(oa_year - db_year) >= 3
    ):
        ctx.warn(
            f"OpenAlex pub_year={oa_year} 与 papers.year={db_year} 相差 "
            f"{abs(oa_year - db_year)} 年；dir_name 含入库年份，"
            "如需纠正用 paper db rename --year"
        )

    # 交叉验证。基准标题选择：
    # - pdf_title 有效时以它为准（PDF 自带元数据，不受 ingest 反查影响），阈值 0.55；
    # - pdf_title 缺失/垃圾时只能比 paper.title——但它可能已被 ingest 的错误 OA
    #   记录覆盖（循环论证：sim 恒 1.00，全量实测 5 篇错配由此锁死），故改用更高
    #   阈值 0.75 + 年份一致性双保险（epub/print 年差放宽到 2）。
    # titles_compatible 兼容副标题截断（'Tetrode technology' vs 'Tetrode technology:
    # advances in ...'，difflib 只有 0.32，纯阈值会误杀）。
    from backend.pipeline.pdf_classify import is_junk_title, titles_compatible
    pdf_title = paper.get("pdf_title")
    ref_title = pdf_title if pdf_title and not is_junk_title(pdf_title) else None
    if info.get("title"):
        if paper.get("attachment_kind") == "si":
            # SI 行经母论文 DOI 富化，返回母论文标题属预期（行元数据即母论文
            # 元数据）；pdf_title/db_title 多为文件名派生（MOESM 命名），与
            # 母论文标题比对必然不兼容。2026-10-17 双 MOESM 实测：sim=0.10
            # 被 title mismatch 拒为 manual_hold。
            ctx.info("SI attachment: adopting parent metadata by DOI,"
                     " title compatibility check skipped")
        elif ref_title:
            sim = oac.title_similarity(ref_title, info["title"])
            ctx.info(f"title similarity vs pdf_title: {sim:.2f}")
            if not titles_compatible(ref_title, info["title"], min_sim=0.55):
                ctx.warn(f"title mismatch (sim={sim:.2f}); rejecting and marking manual_hold")
                _patch_status(paper_id, "manual_hold")
                return
        elif paper.get("title"):
            sim = oac.title_similarity(paper["title"], info["title"])
            oy, py = oa_year, db_year
            ydiff = abs(oy - py) if (oy and py) else 0
            ctx.info(f"title similarity vs db_title: {sim:.2f} (ydiff={ydiff})")
            if not titles_compatible(paper["title"], info["title"], min_sim=0.75) or ydiff > 2:
                ctx.warn(
                    f"title/year mismatch (sim={sim:.2f}, ydiff={ydiff}); "
                    "rejecting and marking manual_hold"
                )
                _patch_status(paper_id, "manual_hold")
                return

    # 更新 DB（不覆盖 dir_name / first_author / pdf_path 等不可变字段）。
    # year 也保持不变：dir_name 内嵌入库年份，年份纠正走 paper db rename --year。
    update = {
        "dir_name":       paper["dir_name"],
        "year":           paper["year"],
        "first_author":   paper["first_author"],
        "title":          info.get("title") or paper.get("title"),
        "authors":        info.get("authors"),
        "journal":        info.get("journal"),
        "doi":            info.get("doi") or paper.get("doi"),
        "arxiv_id":       info.get("arxiv_id") or paper.get("arxiv_id"),
        "openalex_id":    info.get("openalex_id"),
        "url":            info.get("url") or paper.get("url"),
        "abstract":       info.get("abstract") or paper.get("abstract"),
        "pub_type":       info.get("pub_type"),
        "oa_status":      info.get("oa_status"),
        "cited_by_count": info.get("cited_by_count"),
        "concepts":       info.get("concepts"),
        "status_enrich":  "ok",
    }

    # 拉取 BibTeX（Crossref content negotiation）与补缺 abstract（Crossref 第二源）；
    # 两项失败都不阻断 enrich，不覆盖已有值
    doi_for_bibtex = update["doi"]
    if doi_for_bibtex:
        from backend.external.crossref import CrossrefClient
        cc = CrossrefClient()
        try:
            bibtex = cc.fetch_bibtex(doi_for_bibtex)
            if bibtex:
                update["bibtex"] = bibtex
                ctx.info(f"fetched BibTeX ({len(bibtex)} chars)")
            else:
                ctx.info("no BibTeX from Crossref (kept existing if any)")
        except Exception as e:
            ctx.warn(f"fetch BibTeX failed (kept existing if any): {e}")
        # OpenAlex 无 abstract 时从 Crossref 补缺（abstract 空缺是元数据
        # 检索弱的主要放大因素；Semantic Scholar 实测同空，故第二源选 Crossref）
        if not (info.get("abstract") or paper.get("abstract")):
            try:
                cr_abstract = cc.fetch_abstract(doi_for_bibtex)
                if cr_abstract:
                    update["abstract"] = cr_abstract
                    ctx.info(f"abstract backfilled from Crossref ({len(cr_abstract)} chars)")
            except Exception as e:
                ctx.warn(f"Crossref abstract backfill failed (kept empty): {e}")
        # 第三源 Europe PMC：Crossref 也无 abstract 时兜底（2026-09-08 实测
        # OpenAlex/Crossref 对 Springer Nature / Cell Press 系期刊普遍无 abstract，
        # 15/15 抽样双源皆空；Europe PMC 对同批 DOI 全部有摘要）
        if not (update.get("abstract") or info.get("abstract") or paper.get("abstract")):
            from backend.external.europepmc import EuropePmcClient
            try:
                ep_abstract = EuropePmcClient().fetch_abstract(doi_for_bibtex)
                if ep_abstract:
                    update["abstract"] = ep_abstract
                    ctx.info(f"abstract backfilled from Europe PMC ({len(ep_abstract)} chars)")
            except Exception as e:
                ctx.warn(f"Europe PMC abstract backfill failed (kept empty): {e}")

    # 摘要↔解析文本重合校验（元数据/解析质量交叉验证；归一化口径、阈值与
    # 全库标定见 backend/services/abstract_check.py）。低分只警告不阻断：
    # 全库标定显示低分区多为 SI 附件/预印本↔正式版/无摘要版式等
    # 合法成因，人工复核后再处置。
    abstract_final = update.get("abstract") or ""
    if abstract_final:
        from backend.services.abstract_check import classify_containment, shingle_containment
        from backend.services.doc_text import read_doc_text
        text, src = read_doc_text(paper, "en")
        if src == "missing":
            ctx.info("abstract containment: skip (no parsed text yet)")
        else:
            ratio = shingle_containment(abstract_final, text)
            band = classify_containment(ratio)
            if band is None:
                ctx.info("abstract containment: skip (abstract too short)")
            elif band == "low":
                if paper.get("attachment_kind") == "si":
                    # SI 行携带母论文摘要、正文是补充材料，低分是已知成因，
                    # 警告无信息量（低分区以 SI 附件为主）。
                    ctx.info(f"abstract containment {ratio:.2f} (low): expected"
                             " for SI attachment, warning suppressed")
                else:
                    ctx.warn(f"abstract containment {ratio:.2f} (low): possible SI file, "
                             "DOI mismatch or incomplete parse; needs manual review")
            else:
                ctx.info(f"abstract containment {ratio:.2f} ({band})")

    # 同标题重复预警（实测：重复入库多为旧迁移记录无 DOI +
    # 新入库记录有 DOI 的双份；enrich 期预警让重复在入库时即可见，清理走
    # scripts/dedup_papers.py）。SI 附件行排除：其标题取自母论文 DOI 的富化
    # 结果，与母论文行必然同标题，不构成重复入库（attachment_kind='si'）。
    if paper.get("title") and paper.get("attachment_kind") != "si":
        conn = get_conn()
        try:
            dups = conn.execute(
                "SELECT id, dir_name FROM papers WHERE id != ?"
                " AND replace(lower(title), ' ', '') = replace(lower(?), ' ', '')"
                " AND COALESCE(attachment_kind, '') != 'si'",
                (paper_id, paper["title"]),
            ).fetchall()
        finally:
            conn.close()
        if dups:
            ctx.warn("same-title duplicate(s) in library: "
                     + ", ".join(f"#{d['id']}({d['dir_name']})" for d in dups))

    conn = get_conn()
    try:
        conn.execute("BEGIN")
        upsert_paper(conn, update)
        update_fts(conn, paper_id,
                   title=update["title"] or "",
                   abstract=update["abstract"] or "")
        conn.execute("COMMIT")
    finally:
        conn.close()
    ctx.info("metadata updated")


def _patch_status(paper_id: int, status: str) -> None:
    """写独立状态列 status_enrich（20261026 迁移起）。

    此前借用 status_summary 标 manual_hold/error——后续 summary 任务成功后会把
    标记覆盖成 ok，enrich 失败对 MCP/API 消费方不可见（实证：事件表里
    有「三层回退未匹配」警告而状态已被抹平）。详细原因仍由
    ctx.warn 落 events 表。
    """
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE papers SET status_enrich = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (status, paper_id),
        )
    finally:
        conn.close()