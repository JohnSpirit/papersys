"""Inbox 摄入 Worker：接收 inbox_dir 中的新 PDF。

payload: {"inbox_path": "/abs/path/to/file.pdf"}

流程：
  1) pdfinfo -> 抽 metadata；前两页文本 -> 抽强标识符（DOI/arXiv/PII）
  2) DOI 判定（按可靠性从高到低）：
     a. PDF 前两页印刷的 DOI（首页正文题名参与校验；首页前部实际印出的 DOI
        在排版垃圾标题下仍采纳并记警告；SI 附件页面上印的是母论文 DOI，
        放宽采纳；文件名派生的母论文 DOI 仍要求标题匹配）
     b. Elsevier PII（正文/pdf_title 里的 Sxxxx-xxxx(xx)xxxxx-x，S 前缀可选）
        拼 10.1016/ 前缀；as-printed 与 S 前缀切换两个变体都试
     c. arXiv 水印 -> 10.48550/arxiv.*（DataCite DOI，Crossref 无记录，交 OpenAlex 校验）
     d. Crossref 打分制反查（标题+文件名作者+文件名年份+类型多维打分，非首 hit 直采）
     e. OpenAlex 打分制反查（同上）
  3) OpenAlex 富化（有 DOI 按 DOI，否则按标题；标题相似度<0.6 或年份差>=3 拒收）
  4) 生成 dir_name = {first_author}{year}_{keyword}
  5) 复制 PDF 到 library/{year}/{dir_name}/{dir_name}.pdf（相对路径入库）
  6) upsert papers 记录，状态：
      - status_pdf=ok
      - 其余仍 missing；若能确定 DOI/title 则元数据填好
      - SI 附件（filename_suggests_si / headline_suggests_supporting_material
        命中）在行上写 attachment_kind='si'，下游（重复预警 / 语义去重 / Zotero 导出）
        据此排除与母论文行的结构性冲突

inbox_dir 为本地目录（数据根目录下 inbox/），3.0 起无外部存储依赖。

历史教训（错配复盘）：旧流程"Crossref 首 hit + sim>=0.5 即采"会被同名勘误/
来信/评论/SI 组件/书章/学位论文抢位（同名记录 sim 可达 1.0，提阈值无解）；
PDF 首页印着的 DOI 才是最可靠的信号，必须先抽。
"""
from pathlib import Path
from typing import Optional

from backend.config import library_root
from backend.db import tasks_repo
from backend.db.conn import get_conn
from backend.db.repo import upsert_paper, update_fts
from backend.external.crossref import CrossrefClient, title_sim, _item_year
from backend.external.openalex import OpenAlexClient
from backend.services.ingest_dedup import find_duplicate_paper, find_parent_for_si
from backend.pipeline.pdf_classify import (
    pdfinfo, extract_first_page_text, extract_pages_text, is_real_pdf,
    guess_year, guess_first_author, slugify_keyword, build_dir_name,
    strip_filename_prefix, is_junk_title, normalize_author,
    extract_title_from_text, first_author_from_pdf_metadata,
    extract_identifiers, filename_author_year,
    is_derivative_literature, titles_compatible,
    filename_suggests_si, filename_derived_dois,
    headline_suggests_supporting_material,
)
from backend.workers.runner import register_worker, WorkerContext


@register_worker("ingest")
def ingest_worker(ctx: WorkerContext, payload: dict) -> None:
    inbox_path = Path(payload["inbox_path"])
    if not inbox_path.exists():
        raise FileNotFoundError(f"inbox file missing: {inbox_path}")

    # 拦截伪装成 PDF 的非 PDF 文件（如 HTML/文本改扩展名）。
    # MinerU 对这类文件返回含糊的 400 错误，这里提前 fail-fast。
    if not is_real_pdf(str(inbox_path)):
        raise RuntimeError(
            f"not a valid PDF (magic bytes mismatch): {inbox_path}. "
            "Check if the file is actually HTML/text disguised as .pdf."
        )

    ctx.info(f"reading {inbox_path.name}")
    info = pdfinfo(str(inbox_path))
    if "error" in info:
        raise RuntimeError(f"pdfinfo failed: {info['error']}")
    ctx.info(f"pdf: pages={info.get('pages')} title={info.get('pdf_title')!r}")

    first_page = extract_first_page_text(str(inbox_path))
    front_text = extract_pages_text(str(inbox_path), n_pages=2)
    # pdf_title 可能是垃圾（纯数字/乱码/模板名），且 Zotero 文件名带 "作者_年份_"
    # 前缀，两者都会污染反查；首页正文题名作为第三候选，覆盖 metadata 缺失的老 PDF。
    stem_title = strip_filename_prefix(inbox_path.stem)
    page_title = extract_title_from_text(first_page)
    title_candidates = [
        t for t in (info.get("pdf_title"), page_title, stem_title)
        if t and not is_junk_title(t)
    ]
    title = title_candidates[0] if title_candidates else ""
    # 文件名里的作者/年份是区分"原论文 vs 同名勘误/来信/评论"的最强信号
    hint_author, hint_year = filename_author_year(inbox_path.stem)
    ctx.progress(0.1)

    cr = CrossrefClient()
    oac = OpenAlexClient()
    doi: Optional[str] = None
    doi_source = ""  # page_doi / pii / arxiv / crossref_search / oa_search
    cr_item: dict = {}
    # 校验参照标题：始终附加 stem_title——超短标题场景（'SHORT'/'ExampleGrid' 被
    # is_junk_title 杀掉、不能当搜索词，但仍是合法的校验参照；否则 ref 退回带作者
    # 前缀的完整 stem，首页 DOI 校验永远过不了——全量实测 2 例漏 DOI）
    ref_titles = list(title_candidates)
    if stem_title and stem_title not in ref_titles:
        ref_titles.append(stem_title)
    if not ref_titles:
        ref_titles = [inbox_path.stem]

    def _validate_page_doi(d: str, *, si_parent: bool = False,
                           allow_mismatch: bool = False) -> Optional[dict]:
        """校验页面 DOI；SI 与明确的页面标识允许标题不匹配。

        标题匹配仍是首选；页面 DOI 出现在首页前 4000 字符、或由页面 PII
        拼出时，排版垃圾标题不再否决 DOI，只记警告。文件名派生 DOI 与
        Crossref/OpenAlex 搜索来的 DOI 不享受这个放宽，防止误配相邻记录。
        """
        item = cr.get_work(d)
        if not item:
            return None
        t = item.get("title")
        t = t[0] if isinstance(t, list) and t else (t or "")
        if (item.get("type") or "") == "component" or is_derivative_literature(t, d):
            return None
        if any(titles_compatible(r, t) for r in ref_titles) or si_parent:
            return item
        sim = max((title_sim(r, t) for r in ref_titles), default=0.0)
        if allow_mismatch:
            ctx.warn(f"首页 DOI {d} 标题不匹配，按页面标识采纳 (sim={sim:.2f}): {t[:60]!r}")
            return item
        ctx.warn(f"首页 DOI {d} 标题校验失败 (sim={sim:.2f}): {t[:60]!r}")
        return None

    # ── Step 1a: 前两页印刷的 DOI（+ 文件名派生的母论文 DOI 兜底）──
    ids = extract_identifiers(front_text, info.get("pdf_title") or "")
    # SI 判定 = 文件名信号 + 首页文本信号（严格标题形态判定 headline_
    # suggests_supporting_material：正文首页指向 SI 的 boilerplate——
    # "supporting information: <url>" / "can be found at" / 带 Abstract 的
    # 导航栏——不算命中，2026-10-15 回填实测各 1 例假阳性）。Nature 老 SI
    # 首页无 SI 字样，只有 'MOESM1_ESM' 式命名，靠文件名信号兜住。
    si_doc = filename_suggests_si(inbox_path.stem) or \
        headline_suggests_supporting_material(front_text)
    try:
        for d in ids["dois"] + filename_derived_dois(inbox_path.stem):
            # 文件名派生的候选仍需标题校验；正文前部实际印出的 DOI 可在
            # 排版垃圾标题下放宽，避免把正确 DOI 误杀成 year unknown。
            printed_pos = front_text.lower().find(d.lower())
            allow = 0 <= printed_pos < 4000
            item = _validate_page_doi(d, si_parent=si_doc, allow_mismatch=allow)
            if item:
                cr_item, doi, doi_source = item, d, "page_doi"
                ctx.info(f"DOI 取自 PDF 页面: {doi}")
                break
    except Exception as e:  # noqa: BLE001
        ctx.warn(f"page-doi validation failed: {e}")
    ctx.progress(0.2)

    # ── Step 1b: Elsevier PII -> DOI ──
    if not doi:
        try:
            for p in ids["piis"]:
                # 印刷 PII 的 S 前缀与注册 DOI 不一定一致（1983 老记录注册为无前缀
                # 版、2002 注册为 s 版——全量实测各 1 例），两个变体都试，
                # Crossref 里哪个存在用哪个；as-printed 变体优先。
                p_l = p.lower()
                variants = [p_l, p_l[1:] if p_l.startswith("s") else "s" + p_l]
                for v in variants:
                    d = "10.1016/" + v
                    item = _validate_page_doi(d, allow_mismatch=True)
                    if item:
                        cr_item, doi, doi_source = item, d, "pii"
                        ctx.info(f"DOI 由 PII 拼出: {doi}")
                        break
                if doi:
                    break
        except Exception as e:  # noqa: BLE001
            ctx.warn(f"pii validation failed: {e}")

    # ── Step 1c: arXiv 预印本水印（PDF 是预印本时 Zotero 记的也是 arXiv DOI）──
    arxiv_from_page = ids["arxiv"]
    if not doi and arxiv_from_page:
        doi = f"10.48550/arxiv.{arxiv_from_page}"
        doi_source = "arxiv"
        ctx.info(f"DOI 取自 arXiv 水印: {doi}")

    # ── Step 1d: Crossref 打分制反查（候选标题依次尝试）──
    if not doi:
        try:
            for cand in dict.fromkeys(title_candidates or [inbox_path.stem]):
                res = cr.search_best(cand, author=hint_author, year=hint_year)
                if not res:
                    continue
                item, score = res
                d = cr.extract_doi(item)
                if not d:
                    continue
                cr_item, doi, title, doi_source = item, d, cand, "crossref_search"
                ctx.info(f"Crossref doi={doi} (score={score:.2f})")
                break
        except Exception as e:  # noqa: BLE001
            ctx.warn(f"crossref failed: {e}")
    ctx.progress(0.3)

    # 页面 DOI / PII 是 PDF 自身携带、并经 Crossref 标题自指校验的标识符；
    # 文件名年份来自 Zotero 条目，可能是预印本年份（实证：文件名 2020，
    # 正式版 2026），不能反过来否决按 DOI 取回的 OpenAlex 记录。
    authoritative_doi = doi_source in ("page_doi", "pii")

    # ── Step 2: OpenAlex 富化（打分制兜底 + 严格校验）──
    oa_info: dict = {}
    try:
        work = (
            oac.get_work(doi) if doi
            else oac.search_work(title, year=hint_year, author=hint_author) if title
            else {}
        )
        if not doi and work:
            doi_source = "oa_search"
        if work:
            oa_info = oac.extract_work_info(work)
            # 标题/年份校验（防 DOI 张冠李戴）。搜索来的 DOI 是标题猜出来的，
            # 标题或年份不符必须丢弃；页面/PII 来的 DOI 已过自指校验，年份差
            # 只告警不丢弃（预印本与正式版年份可能相差多年）。
            if title and oa_info.get("title"):
                ok_title = any(
                    titles_compatible(r, oa_info["title"], min_sim=0.6)
                    for r in ref_titles
                )
                sim = max(
                    (oac.title_similarity(r, oa_info["title"]) for r in ref_titles),
                    default=0.0,
                )
                oy = oa_info.get("pub_year")
                ydiff = abs(oy - hint_year) if (oy and hint_year) else 0
                if ok_title and authoritative_doi and ydiff >= 3:
                    ctx.warn(
                        f"OpenAlex 年份与文件名不一致 (pub_year={oy},"
                        f" filename_year={hint_year})；DOI 来自页面，保留记录"
                    )
                if not ok_title or (ydiff >= 3 and not authoritative_doi):
                    ctx.warn(
                        f"OpenAlex 校验失败 (sim={sim:.2f}, ydiff={ydiff}): "
                        f"{oa_info['title'][:60]!r}"
                    )
                    oa_info = {}
                    # 搜索来源的 DOI 是"标题猜出来的"，校验失败必须丢弃；
                    # arXiv 水印同样只有正则置信（可能匹配到引用的他人 arXiv id），
                    # Crossref 无 10.48550 记录无法自指校验，故 OpenAlex 否决时一并丢弃。
                    # page_doi/pii 来源已过 _validate_page_doi 自指校验，保留。
                    if doi_source in ("crossref_search", "oa_search", "arxiv"):
                        doi = None
            if oa_info:
                ctx.info(f"OpenAlex matched: {oa_info.get('title', '')[:60]}")
    except Exception as e:  # noqa: BLE001
        ctx.warn(f"openalex failed: {e}")
    ctx.progress(0.5)

    # ── Step 3: 推断 dir_name ──
    cr_title_raw = cr_item.get("title")
    cr_title = (cr_title_raw[0] if isinstance(cr_title_raw, list) and cr_title_raw
                else (cr_title_raw or ""))
    resolved_title = oa_info.get("title") or cr_title or title
    # 年份优先 OpenAlex；页面/PII DOI 的 Crossref 记录年份次之（同一 DOI 的
    # 注册年份比文件名可靠）；文件名词条年份再次之，最后才扫首页。这样即使
    # OpenAlex 暂时不可用，也不会把可解析 DOI 的 PDF 判成 year unknown。
    crossref_year = _item_year(cr_item) if cr_item else None
    year = (
        (oa_info.get("pub_year") if isinstance(oa_info.get("pub_year"), int) else None)
        or (crossref_year if authoritative_doi else None)
        or hint_year
        or crossref_year
        or guess_year(resolved_title or "", inbox_path.stem, first_page)
        or 0
    )

    # first_author：OpenAlex -> 页面 DOI 的 Crossref 记录 -> 文件名 ->
    # PDF 内嵌作者 -> 首页/文件名正则。PDF 内嵌作者只在有完整姓名时采纳。
    first_author = None
    if oa_info.get("authors"):
        import json
        try:
            authors = json.loads(oa_info["authors"])
            if authors:
                first_author = normalize_author(authors[0].split()[-1]) or None
        except Exception:
            pass
    if not first_author and cr_item:
        cr_authors = cr_item.get("author") or []
        if cr_authors:
            first_author = normalize_author(cr_authors[0].get("family", "")) or None
    if not first_author:
        first_author = (
            hint_author
            or first_author_from_pdf_metadata(info.get("pdf_authors"))
            or guess_first_author(first_page, inbox_path.stem)
            or "unknown"
        )

    keyword = slugify_keyword(resolved_title or inbox_path.stem)

    if year == 0:
        # year 是 dir_name 的不可变组成部分；缺失时拒绝入库，
        # 让用户补全元数据（如重命名 PDF 加入年份）后重新投递。
        # PDF 留在 inbox，task 标 error，不归档，不写 papers 表。
        raise RuntimeError(
            "year unknown; cannot generate immutable dir_name. "
            "Please rename the PDF to include year (e.g. '2024_xxx.pdf') and retry."
        )

    dir_name = build_dir_name(first_author, year, keyword)
    status_pdf = "ok"

    # dir_name 冲突消歧：磁盘上已有文件且大小不同 -> 必是两个不同文件
    # （典型：正文与 SI 附件塌缩到同一 keyword。旧逻辑在无 DOI 时整块跳过，
    # 后入队者直接覆盖归档 PDF 与行内容——ExampleGrid 正文/SI
    # 合并丢失 SI 即此因，全量实测）；同大小但双方 DOI 都确定且不同同理分开
    # （多个无标题 SI 附件撞名）。仅同大小且无 DOI 冲突（同一文件重复投递/
    # 重跑）才合并 upsert。
    conn = get_conn()
    try:
        clash = conn.execute(
            "SELECT id, doi, pdf_path FROM papers WHERE dir_name = ?", (dir_name,)
        ).fetchone()
    finally:
        conn.close()
    if clash:
        from backend.config import resolve_path
        exist = resolve_path(clash["pdf_path"])
        same_size = (
            bool(exist)
            and Path(exist).stat().st_size == inbox_path.stat().st_size
        )
        both_have_doi = bool(clash["doi"]) and bool(doi)
        same_doi = both_have_doi and clash["doi"].lower() == doi.lower()
        if (not same_size) or (both_have_doi and not same_doi):
            import secrets as _secrets
            dir_name = f"{dir_name}_{_secrets.token_hex(2)}"
            ctx.warn(f"dir_name 冲突（已有 paper {clash['id']} doi={clash['doi']}），"
                     f"消歧为 {dir_name}")

    ctx.info(f"dir_name = {dir_name}")
    ctx.progress(0.7)

    effective_doi = doi or oa_info.get("doi")
    conn = get_conn()
    try:
        duplicate, duplicate_reason = find_duplicate_paper(
            conn,
            doi=effective_doi,
            file_path=inbox_path,
            title=resolved_title,
            attachment_kind="si" if si_doc else None,
        )
        si_parent = find_parent_for_si(conn, effective_doi) if si_doc else None
    finally:
        conn.close()
    if duplicate:
        # 同 DOI/同文件的重复投递复用已有论文，不再复制归档或新增 papers 行。
        ctx.paper_id = duplicate["id"]
        tasks_repo.update_task(ctx.task_id, paper_id=duplicate["id"])
        ctx.info(f"duplicate {duplicate_reason}: reuse paper {duplicate['id']} "
                 f"({duplicate['dir_name']}); skip archive")
        if duplicate.get("dir_name") != dir_name or (
            duplicate.get("year") and year and duplicate["year"] != year
        ):
            ctx.warn(
                f"重复 DOI 推断的目录与现有记录不一致：本次 {dir_name}/{year}，"
                f"现有 {duplicate['dir_name']}/{duplicate.get('year')}；"
                "保留现有目录，如需纠正用 paper db rename --year"
            )
        try:
            inbox_path.unlink()
            ctx.info("inbox file removed")
        except Exception as e:  # noqa: BLE001
            ctx.warn(f"failed to remove inbox file: {e}")
        ctx.progress(1.0)
        return

    # ── Step 4: 归档到本地文库 ──
    pdf_dir = library_root() / str(year) / dir_name
    pdf_dir.mkdir(parents=True, exist_ok=True)
    dest = pdf_dir / f"{dir_name}.pdf"

    dest.write_bytes(inbox_path.read_bytes())
    ctx.info(f"archived -> {dest}")
    ctx.progress(0.85)

    # ── Step 5: upsert DB（pdf_path 存相对路径，读取走 resolve_path） ──
    paper_data = {
        "dir_name":      dir_name,
        "year":          year or 0,
        "first_author":  first_author,
        "keyword":       keyword,
        "title":         resolved_title or None,
        "authors":       oa_info.get("authors"),
        "journal":       oa_info.get("journal"),
        "doi":           effective_doi,
        "arxiv_id":      oa_info.get("arxiv_id") or arxiv_from_page,
        "openalex_id":   oa_info.get("openalex_id"),
        "url":           oa_info.get("url"),
        "abstract":      oa_info.get("abstract"),
        "cited_by_count": oa_info.get("cited_by_count"),
        "oa_status":     oa_info.get("oa_status"),
        "pub_type":      oa_info.get("pub_type"),
        "concepts":      oa_info.get("concepts"),

        "pdf_path":      str(dest.relative_to(library_root())),
        "pdf_pages":     info.get("pages"),
        "pdf_size_bytes": info.get("size_bytes"),
        "pdf_title":     info.get("pdf_title"),
        "pdf_authors":   info.get("pdf_authors"),
        "created_by_user_id": getattr(ctx, "created_by_user_id", None),

        "status_pdf":       status_pdf,
        "status_parsed":    "missing",
        "status_translated": "missing",
        "status_summary":   "missing",
        "status_outline":   "missing",
    }
    if si_doc:
        # SI 附件标记；键只在判定时写入——upsert 语义为"未指定字段保持原值"，
        # 正文重投不会把已标记行改回 NULL（也保留未来人工修正的空间）。
        paper_data["attachment_kind"] = "si"
        if si_parent:
            paper_data["parent_paper_id"] = si_parent["id"]
    conn = get_conn()
    try:
        conn.execute("BEGIN")
        paper_id, is_new = upsert_paper(conn, paper_data)
        update_fts(conn, paper_id,
                   title=paper_data.get("title") or "",
                   abstract=paper_data.get("abstract") or "",
                   parsed_text="")
        conn.execute("COMMIT")
    finally:
        conn.close()
    # 回填 task → paper 关联，使前端任务面板与事件流能定位到论文。
    # 注意 task 创建于 ingest 之前（inbox_upload），此时才有 paper_id。
    ctx.paper_id = paper_id
    tasks_repo.update_task(ctx.task_id, paper_id=paper_id)
    ctx.info(f"paper_id={paper_id} {'created' if is_new else 'updated'}")

    # 删除 inbox 文件
    try:
        inbox_path.unlink()
        ctx.info("inbox file removed")
    except Exception as e:  # noqa: BLE001
        ctx.warn(f"failed to remove inbox file: {e}")

    ctx.progress(1.0)
