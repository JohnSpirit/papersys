"""文献调研系统 MCP 服务器（stdio 与 HTTP 两种传输，无登录体系）。

工具面 = 现有 Flask API 的核心子集 + 全文读取（19 个工具；3.1 起移除调研两轮）：
  检索    search_papers / semantic_search / search_library / search_fulltext
  入库    ingest_by_url / ingest_local_file / ingest_local_files / get_task / list_tasks
  读取    get_paper / get_papers / get_paper_by_doi / get_papers_by_dois
          read_fulltext / read_translation / get_summary
  网络    get_citations
  批注    get_annotations / add_annotation

并发模型：MCP 进程写 tasks 表入队 -> Flask runner（若在运行）claim_pending 领取执行；
Flask 未运行时 task 保持 pending（用户启动后自动消费）。SQLite WAL 保证双进程安全。

启动：`paper mcp`（stdio 传输，供 opencode / Claude Desktop 等接入）；
`paper mcp --transport http` 启动 HTTP 传输（端点 /mcp；不注册
ingest_local_file / ingest_local_files——远程调用方不应触碰服务器本地路径）。
"""
from __future__ import annotations

import json
from typing import Optional

from fastmcp import FastMCP

from backend.db.conn import get_conn
from backend.db.repo import get_paper as _repo_get_paper
from backend.services.doc_text import read_latest_summary
from backend.version import get_version

mcp = FastMCP("paper-survey", version=get_version())


# ── 检索 ────────────────────────────────────────────────


@mcp.tool
def search_papers(source: str, query: str, limit: int = 10,
                  year_from: Optional[int] = None, year_to: Optional[int] = None,
                  brief: bool = False) -> str:
    """多源学术文献检索（不写库）。

    query 为裸 DOI（或 doi.org URL / doi: 前缀）时，semantic_scholar/openalex
    两个源自动改走精确 DOI 解析；semantic_scholar 未命中时依次回退
    openalex、crossref，并在 sources_tried 返回实际尝试的数据源。

    Args:
        source: 检索源，one of ["arxiv", "openalex", "semantic_scholar", "sciverse"]
        query: 检索关键词或 DOI
        limit: 返回条数上限（默认 10，最大 100）
        year_from: 起始年份（可选）
        year_to: 截止年份（可选）
        brief: True 时去掉 abstract/tldr，保留标题、DOI、链接和 in_library

    Returns:
        JSON 文本：{source, sources_tried, count, items: [{title, authors, year,
        doi, arxiv_id, url, pdf_url, abstract, ...}]}；item 的 DOI 命中本地库时带
        in_library: [paper_id, ...]（同 DOI 重复入库会列出全部），可据此直接
        转 get_paper/get_summary，无需再逐条 get_paper_by_doi 核对
    """
    from backend.api.search import _DISPATCHERS, compact_search_items
    from backend.db.repo import list_papers_by_doi
    if source not in _DISPATCHERS:
        return json.dumps({"error": f"unknown source: {source}; must be {list(_DISPATCHERS)}"})
    try:
        items = _DISPATCHERS[source](query, max(1, min(int(limit), 100)), year_from, year_to)
        sources_tried = getattr(items, "sources_tried", [source])
        # 摘要截断防爆 context
        for it in items:
            ab = it.get("abstract")
            if isinstance(ab, str) and len(ab) > 800:
                it["abstract"] = ab[:800] + "…"
        # 本地库 DOI 比对：命中的条目直接标 in_library（实测：agent 外部
        # 检索后逐条 get_paper_by_doi 核对本地是否已收录）
        conn = get_conn()
        try:
            for it in items:
                doi = (it.get("doi") or "").strip()
                if not doi:
                    continue
                hits = list_papers_by_doi(conn, doi)
                if hits:
                    it["in_library"] = [h["id"] for h in hits]
        finally:
            conn.close()
        items = compact_search_items(items, brief)
        return json.dumps({"source": source, "sources_tried": sources_tried,
                           "count": len(items), "items": items},
                          ensure_ascii=False, default=str)
    except Exception as e:  # noqa: BLE001
        return json.dumps({"error": f"search failed: {e}"})


@mcp.tool
def semantic_search(query: str, topk: int = 5,
                    paper_id: Optional[int] = None,
                    include_context: int = 200) -> str:
    """库内语义检索（向量 RAG）：按自然语言查询找到最相关的论文片段。

    Args:
        query: 自然语言查询（任意语言）
        topk: 返回条数（默认 5）
        paper_id: 限定单篇论文内检索。在指定论文里定位特定概念/说法时使用，
                  返回同论文多个片段，与 read_fulltext 的 offset 配合读取上下文
        include_context: 每条片段回显字符数（默认 200，最大 6000）。核实具体
                         数值/公式时调大，避免为找一句话翻全文页

    Returns:
        JSON：{count, items: [{paper_id, title, year, chunk_index, score, text,
               char_start, char_end}]}
        跨库检索按论文去重（paper_id + 归一化标题，每篇至多一个最相关片段），
        按精排 relevance_score >= 0.5 阈值过滤；单篇检索（paper_id 锁定）保留
        同论文多片段且不做阈值过滤（「在这篇里找」意图下弱相关片段也是有效
        答案），低于阈值的条目标 low_confidence: true。内容状态列为 manual_hold
        的论文不参与检索；status_enrich 不参与排除。库内无向量时返回
        {error: "no embeddings..."}（先对论文执行 embed）；指定论文无向量时
        报错带该篇的 status_parsed/status_embedded，指向单篇 parse/embed 而非全库重建
    """
    from backend.services.semantic_search import semantic_search as _ss
    include_context = max(1, min(int(include_context), 6000))
    return json.dumps(_ss(query, topk=topk, paper_id=paper_id,
                          include_context=include_context), ensure_ascii=False)


@mcp.tool
def ingest_by_url(url: str, filename: str = "",
                  steps: Optional[list[str]] = None) -> str:
    """下载 PDF 到 inbox 并创建流水线任务（异步：返回 task_id，用 get_task 轮询）。

    Args:
        url: PDF 直链 URL（仅 http/https，拒绝内网地址；下载逻辑与
             /api/inbox/from_url 共用 services/pdf_download.py）
        filename: 可选文件名（缺省从 URL 推断）
        steps: 顺序执行的步骤，默认 ["ingest","parse","enrich","embed","summary"]；
               首项必须是 "ingest"；传 ["ingest"] 时只入库、不自动解析

    Returns:
        JSON：{task_id, path, size, steps} 或 {error}
    """
    from backend.config import ensure_inbox_dir
    from backend.db import tasks_repo
    from backend.pipeline.steps import normalize_feed_steps
    from backend.services.pdf_download import (
        DownloadError,
        UrlValidationError,
        stage_pdf_from_url,
    )

    try:
        step_list = normalize_feed_steps(steps)
    except ValueError as e:
        return json.dumps({"error": str(e)})

    try:
        inbox = ensure_inbox_dir()
    except OSError as e:
        return json.dumps({"error": str(e)})

    try:
        target, size = stage_pdf_from_url(url, inbox, filename)
    except (UrlValidationError, DownloadError) as e:
        return json.dumps({
            "error": str(e),
            "hint": "下载失败时可先手动下载 PDF，再调用 ingest_local_file(path) 导入。",
        })
    except Exception as e:  # noqa: BLE001 — 红线 11：MCP 工具永不抛异常
        return json.dumps({"error": str(e)})

    tid = tasks_repo.create_task(
        kind="pipeline",
        payload={"inbox_path": str(target), "steps": step_list},
    )
    return json.dumps(
        {"task_id": tid, "path": str(target), "size": size, "steps": step_list}
    )


@mcp.tool
def ingest_local_file(path: str, filename: str = "",
                      steps: Optional[list[str]] = None) -> str:
    """把本地 PDF 复制到 inbox 并创建流水线任务（异步：返回 task_id，用 get_task 轮询）。

    Args:
        path: 本地 PDF 文件路径（建议绝对路径；支持 ~ 展开；须存在、后缀 .pdf、文件头为 PDF）
        filename: 可选，inbox 中保存的文件名（缺省沿用源文件名）
        steps: 顺序执行的步骤，默认 ["ingest","parse","enrich","embed","summary"]；
               首项必须是 "ingest"；传 ["ingest"] 时只入库、不自动解析

    Returns:
        JSON：{task_id, path, size, steps} 或 {error}
    """
    import re
    import secrets
    import shutil
    from pathlib import Path

    from backend.config import ensure_inbox_dir
    from backend.db import tasks_repo
    from backend.pipeline.pdf_classify import is_real_pdf
    from backend.pipeline.steps import normalize_feed_steps
    from backend.services.pdf_download import MAX_PDF_BYTES

    try:
        step_list = normalize_feed_steps(steps)
    except ValueError as e:
        return json.dumps({"error": str(e)})

    raw = (path or "").strip()
    if not raw:
        return json.dumps({"error": "path required"})
    if "://" in raw:
        return json.dumps(
            {"error": f"not a local path: {raw}; URLs are handled by ingest_by_url"}
        )

    src = Path(raw).expanduser()
    try:
        if not src.is_file():
            return json.dumps({"error": f"file not found: {src}"})
        size = src.stat().st_size
    except OSError as e:
        return json.dumps({"error": str(e)})
    if src.suffix.lower() != ".pdf":
        return json.dumps({"error": f"not a .pdf file: {src}"})
    if size > MAX_PDF_BYTES:
        return json.dumps(
            {"error": f"file exceeds limit {MAX_PDF_BYTES} bytes: {src}"}
        )
    if not is_real_pdf(str(src)):
        return json.dumps({"error": f"not a real PDF (bad magic bytes): {src}"})

    try:
        inbox = ensure_inbox_dir()
    except OSError as e:
        return json.dumps({"error": str(e)})

    name = (filename or src.name).strip() or src.name
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    name = re.sub(r"[^\w\-_. ]", "_", name)
    target = inbox / Path(name).name
    if src.resolve() == target.resolve():
        target = src
    else:
        if target.exists():
            target = inbox / f"{target.stem}_{secrets.token_hex(4)}.pdf"
        try:
            shutil.copyfile(src, target)
        except OSError as e:
            return json.dumps({"error": f"copy to inbox failed: {e}"})

    tid = tasks_repo.create_task(
        kind="pipeline",
        payload={"inbox_path": str(target), "steps": step_list},
    )
    return json.dumps(
        {"task_id": tid, "path": str(target), "size": size, "steps": step_list}
    )


@mcp.tool
def ingest_local_files(paths: list[str], steps: Optional[list[str]] = None) -> str:
    """批量把本地 PDF 复制到 inbox 并创建流水线任务。

    每个文件独立处理，单个文件失败不会阻断后续文件；最多 100 个路径。
    返回 {count, ok, failed, items}，items 每项含 source_path 及单文件结果。
    """
    if not isinstance(paths, list):
        return json.dumps({"error": "paths must be a list of local PDF paths"})
    if not paths:
        return json.dumps({"count": 0, "ok": 0, "failed": 0, "items": []})
    if len(paths) > 100:
        return json.dumps({"error": "at most 100 files per call"})
    items = []
    for path in paths:
        result = json.loads(ingest_local_file(path, steps=steps))
        result["source_path"] = path
        items.append(result)
    ok = sum("error" not in item for item in items)
    return json.dumps({
        "count": len(items), "ok": ok, "failed": len(items) - ok, "items": items,
    }, ensure_ascii=False, default=str)


@mcp.tool
def get_task(task_id: int) -> str:
    """查询任务状态（ingest/parse/translate 等异步操作的轮询入口）。

    Returns:
        JSON：{id, kind, status(pending|running|ok|error|cancelled), progress, paper_id, error}
    """
    from backend.db import tasks_repo
    try:
        t = tasks_repo.get_task(task_id)
    except Exception as e:  # noqa: BLE001
        return json.dumps({"error": str(e)})
    if not t:
        return json.dumps({"error": f"task {task_id} not found"})
    t.pop("log_tail", None)
    return json.dumps(t, ensure_ascii=False, default=str)


@mcp.tool
def list_tasks(status: Optional[str] = None, kind: Optional[str] = None,
               paper_id: Optional[int] = None, limit: int = 20) -> str:
    """列出任务队列，用于批量入库后的进度与失败检查。

    Args:
        status: 可选 pending|running|ok|error|cancelled
        kind: 可选任务类型（pipeline/ingest/parse/enrich/embed/summary/translate）
        paper_id: 可选，限定单篇论文
        limit: 返回条数（默认 20，最大 200，按 id 倒序）

    Returns:
        JSON：{count, items: [{id, kind, status, progress, error, retries,
               paper_id, created_at, started_at, finished_at, log_path}]}
    """
    from backend.db import tasks_repo
    if status and status not in ("pending", "running", "ok", "error", "cancelled"):
        return json.dumps({"error": "invalid status"})
    limit = max(1, min(int(limit), 200))
    try:
        items = tasks_repo.list_tasks(
            status=status, kind=kind, paper_id=paper_id, limit=limit,
        )
    except Exception as e:  # noqa: BLE001
        return json.dumps({"error": str(e)})
    fields = ("id", "kind", "status", "progress", "error", "retries", "paper_id",
              "created_at", "started_at", "finished_at", "log_path")
    return json.dumps({
        "count": len(items),
        "items": [{key: item.get(key) for key in fields} for item in items],
    }, ensure_ascii=False, default=str)


# ── 读取 ────────────────────────────────────────────────


def _paper_meta(p: dict, rich: bool = False) -> dict:
    meta = {
        "id": p["id"], "dir_name": p["dir_name"], "year": p["year"],
        "title": p.get("title"), "first_author": p.get("first_author"),
        "journal": p.get("journal"), "doi": p.get("doi"), "arxiv_id": p.get("arxiv_id"),
        "cited_by_count": p.get("cited_by_count"),
        "status": {
            "pdf": p.get("status_pdf"), "parsed": p.get("status_parsed"),
            "enrich": p.get("status_enrich"),
            "translated": p.get("status_translated"), "summary": p.get("status_summary"),
        },
    }
    if rich:
        # get_paper 专用：暴露 keyword/authors/abstract 及其空缺，
        # abstract 截断防爆 context（与 search_papers 的 800 字符约定一致）
        ab = p.get("abstract") or ""
        meta["keyword"] = p.get("keyword")
        meta["authors"] = p.get("authors")
        meta["abstract"] = (ab[:800] + "…") if len(ab) > 800 else ab
    return meta


@mcp.tool
def get_paper(paper_id: int) -> str:
    """读取单篇论文的元数据与流水线状态（含 abstract/keyword/authors）。"""
    conn = get_conn()
    try:
        p = _repo_get_paper(conn, paper_id)
    except Exception as e:  # noqa: BLE001
        return json.dumps({"error": str(e)})
    finally:
        conn.close()
    if not p:
        return json.dumps({"error": f"paper {paper_id} not found"})
    return json.dumps(_paper_meta(p, rich=True), ensure_ascii=False, default=str)


@mcp.tool
def get_paper_by_doi(doi: str) -> str:
    """按 DOI 精确匹配本地文献库。

    大小写不敏感；容忍 https://doi.org/ 、doi.org/ 、doi: 等前缀。
    返回 {count, items}；count=0 表示该 DOI 不在库（可转 ingest_by_url 入库）。
    同一 DOI 重复入库时全部返回（按 id 升序），凭 title/dir_name 区分。
    items 为完整元数据（含 keyword/authors/abstract，abstract 超 800 字符截断）。
    """
    from backend.db.repo import list_papers_by_doi
    conn = get_conn()
    try:
        papers = list_papers_by_doi(conn, doi)
    except Exception as e:  # noqa: BLE001 — 红线 11：MCP 工具永不抛异常
        return json.dumps({"error": str(e)})
    finally:
        conn.close()
    return json.dumps(
        {"count": len(papers), "items": [_paper_meta(p, rich=True) for p in papers]},
        ensure_ascii=False, default=str,
    )


@mcp.tool
def get_papers_by_dois(dois: list[str]) -> str:
    """批量按 DOI 核对本地文献库，返回每条的命中数和论文 id。

    用于参考文献清单核对，避免逐条 get_paper_by_doi。最多 200 个 DOI；
    前缀和大小写按 get_paper_by_doi 的同一规则归一化。
    返回 {count, items: [{doi, count, ids, title, dir_name}]}。
    """
    from backend.db.repo import _normalize_doi, list_papers_by_dois
    if not isinstance(dois, list):
        return json.dumps({"error": "dois must be a list of strings"})
    if len(dois) > 200:
        return json.dumps({"error": "at most 200 DOIs per call"})
    conn = get_conn()
    try:
        grouped = list_papers_by_dois(conn, dois)
    except Exception as e:  # noqa: BLE001
        return json.dumps({"error": str(e)})
    finally:
        conn.close()
    items = []
    for doi in dois:
        hits = grouped.get(_normalize_doi(doi), [])
        first = hits[0] if hits else {}
        items.append({
            "doi": doi,
            "count": len(hits),
            "ids": [p["id"] for p in hits],
            "title": first.get("title"),
            "dir_name": first.get("dir_name"),
        })
    return json.dumps({"count": len(items), "items": items},
                      ensure_ascii=False, default=str)


@mcp.tool
def get_papers(ids: list[int]) -> str:
    """批量读取论文的简要元数据与流水线状态，最多 200 个 id。

    返回 {count, items: [论文简要记录], missing: [不存在的 id]}。
    """
    from backend.db.repo import get_papers_by_ids
    if not isinstance(ids, list):
        return json.dumps({"error": "ids must be a list of integers"})
    if len(ids) > 200:
        return json.dumps({"error": "at most 200 paper ids per call"})
    try:
        normalized = list(dict.fromkeys(int(pid) for pid in ids))
    except (TypeError, ValueError) as e:
        return json.dumps({"error": str(e)})
    conn = get_conn()
    try:
        papers = get_papers_by_ids(conn, normalized)
    except Exception as e:  # noqa: BLE001
        return json.dumps({"error": str(e)})
    finally:
        conn.close()
    found = {p["id"] for p in papers}
    missing = [pid for pid in normalized if pid not in found]
    return json.dumps({
        "count": len(papers),
        "items": [_paper_meta(p) for p in papers],
        "missing": missing,
    }, ensure_ascii=False, default=str)


# 过滤只看内容状态列；status_enrich 的 manual_hold 是元数据层失败（内容完好，
# 回填实证 enrich-hold 含 example2008 等经典论文），不排除检索，
# 经 _paper_meta 的 status.enrich / stats 暴露
_STATUS_COLS = ("status_pdf", "status_parsed", "status_translated",
                "status_summary", "status_outline", "status_embedded")


def _drop_held_and_dup_titles(rows: list[dict]) -> tuple[list[dict], dict]:
    """过滤内容状态列为 manual_hold 的论文与同标题重复，返回 (行, 排除计数)。

    仅用于 MCP 检索工具（agent 消费端降噪，实测重复记录会在固定 topk 中
    反复占用名额）；repo.list_papers 与 Web UI 列表不经过此过滤，held 论文
    仍在 Library 页可见。同标题归一化 = lower + 压空白（重复对标题可仅差
    一个空格）。同标题双版本（preprint/正式版）并存时正式版顶替预印本占据的
    名额（is_preprint_record 判定）。
    """
    from backend.db.repo import is_preprint_record
    held = dup = 0
    best: dict[str, dict] = {}
    out: list[dict] = []
    for r in rows:
        if "manual_hold" in (r.get(c) for c in _STATUS_COLS):
            held += 1
            continue
        title = " ".join((r.get("title") or "").lower().split())
        if not title:
            out.append(r)
            continue
        cur = best.get(title)
        if cur is None:
            best[title] = r
            out.append(r)
            continue
        dup += 1
        if is_preprint_record(cur.get("journal"), cur.get("doi")) and \
                not is_preprint_record(r.get("journal"), r.get("doi")):
            for i, x in enumerate(out):
                if x is cur:
                    out[i] = r
                    break
            best[title] = r
    return out, ({"held": held, "dup_title": dup} if held or dup else {})


@mcp.tool
def search_library(q: str = "", fulltext: str = "", limit: int = 20,
                   year_from: Optional[int] = None, year_to: Optional[int] = None) -> str:
    """检索本地文献库（元数据 LIKE + 可选 FTS 全文）。

    Args:
        q: 关键字（title/dir_name/first_author/keyword/abstract/authors/doi；
           多词按逐词 AND 匹配——词不必相邻连续，每个词都命中即算；元数据
           无命中时自动回退 FTS 全文检索，检索词出现在正文也算命中）
        fulltext: 全文检索（FTS5，命中 parsed_text；词间隐式 AND，建议 2-4 个
           实词；AND 落空且词数 > 3 时自动降级 OR + bm25 排序。提供时禁用
           q 回退，与 q 取 AND）
        limit: 上限（默认 20，最大 100）

    Returns:
        JSON：{count, matched_by(metadata|fulltext，仅 q 提供时输出),
               items: [{id, dir_name, year, title, first_author, doi, status}]}
        fulltext 命中的条目按 bm25 相关度排序（非 fulltext 路径按 year DESC）；
        内容状态列为 manual_hold 的论文与同标题重复（保留首条）被过滤，计数见
        excluded: {held, dup_title}；q 与 fulltext 同时提供且 0 命中时附
        q_matches / fulltext_matches（各条件单独命中数，判断哪个条件为空）。
    """
    from backend.db.repo import list_papers, count_papers, _fts_search
    q = (q or "").strip()
    fulltext = (fulltext or "").strip()
    lim = min(int(limit), 100)
    conn = get_conn()
    try:
        ranked: Optional[list[int]] = None
        fts_query = fulltext or None
        rows: list[dict] = []
        matched_by = "metadata" if q else None
        if q and not fulltext:
            rows = list_papers(conn, q=q, year_from=year_from, year_to=year_to, limit=lim)
            if not rows:
                # q 无命中时回退 FTS。先经 _fts_search 预检：非空 id
                # 列表才回退——None（FTS 表不可用）会让 _papers_where 跳过过滤返回全库，
                # []（无命中）回退也无意义，两者都保持 count=0。
                ranked = _fts_search(conn, q)
                if ranked:
                    fts_query = q
                    matched_by = "fulltext"
        elif not fulltext:
            # 无 q 无 fulltext：浏览模式，列出全部（过滤 manual_hold/重复后返回）
            rows = list_papers(conn, year_from=year_from, year_to=year_to, limit=lim)
        if fts_query:
            if ranked is None:
                ranked = _fts_search(conn, fts_query)
            # 取数窗口放宽后按 bm25 排名重排再截断（避免先按年份截断丢失相关条目）。
            # 回退路径（fulltext 未显式提供）不重放 q——原语义是只用 FTS 重查，
            # 把 q 叠回 AND 会让元数据不命中的回退永远为零
            fetch = min(max(lim, 50), 100)
            rows = list_papers(conn, q=(q if fulltext else None), fulltext=fts_query,
                               year_from=year_from, year_to=year_to, limit=fetch)
            if ranked:
                order = {pid: i for i, pid in enumerate(ranked)}
                rows.sort(key=lambda r: order.get(r["id"], len(order)))
            rows = rows[:lim]
        zero_diag: dict = {}
        if q and fulltext and not rows:
            zero_diag = {
                "q_matches": count_papers(conn, q=q),
                "fulltext_matches": count_papers(conn, fulltext=fulltext),
            }
    except Exception as e:  # noqa: BLE001
        return json.dumps({"error": str(e)})
    finally:
        conn.close()
    rows, excluded = _drop_held_and_dup_titles(rows)
    out = {"count": len(rows), "items": [_paper_meta(r) for r in rows]}
    if q:
        out["matched_by"] = matched_by
    if excluded:
        out["excluded"] = excluded
    out.update(zero_diag)
    return json.dumps(out, ensure_ascii=False, default=str)


@mcp.tool
def read_fulltext(paper_id: int, offset: int = 0, limit: int = 8000) -> str:
    """读取论文全文（英文 markdown，MinerU 解析结果；人工复核版优先）。

    分页读取：offset/limit 为字符偏移。全文长度见返回的 total。
    toc（章节标题 + 字符偏移）仅随 offset=0 的首页返回，可直接以某章节的
    offset 跳转读取；后续翻页不再重复携带。

    Returns:
        JSON：{available, source(parsed|manual_edit), total, offset, text, has_more,
               toc（仅 offset=0）}
    """
    from backend.api.reader import _doc_text
    from backend.services.doc_text import extract_toc
    conn = get_conn()
    try:
        p = _repo_get_paper(conn, paper_id)
    finally:
        conn.close()
    if not p:
        return json.dumps({"error": f"paper {paper_id} not found"})
    text, source = _doc_text(p, "en")
    if not text:
        return json.dumps({"available": False, "reason": "no parsed full.md (run parse first)",
                           "source": source, "total": 0, "text": "", "has_more": False})
    limit = max(1, min(int(limit), 50000))
    offset = max(0, int(offset))
    snippet = text[offset:offset + limit]
    out = {
        "available": True, "source": source, "total": len(text),
        "offset": offset, "text": snippet, "has_more": offset + limit < len(text),
    }
    if offset == 0:
        out["toc"] = extract_toc(text)
    return json.dumps(out, ensure_ascii=False)


@mcp.tool
def search_fulltext(paper_id: int, query: str, context_chars: int = 500,
                    max_hits: int = 20) -> str:
    """在单篇论文英文全文中做大小写不敏感的字面检索。

    Args:
        paper_id: 论文 ID
        query: 要查找的原文字符串（不支持正则）
        context_chars: 命中位置前后各返回的字符数（默认 500，最大 6000）
        max_hits: 最多返回的命中数（默认 20，最大 100）

    Returns:
        JSON：{available, source, query, count, total_hits, has_more,
               items: [{offset, context_start, context_end, text}]}；
        offset 可直接传给 read_fulltext。
    """
    from backend.api.reader import _doc_text
    query = (query or "").strip()
    if not query:
        return json.dumps({"error": "query required"})
    conn = get_conn()
    try:
        p = _repo_get_paper(conn, paper_id)
    finally:
        conn.close()
    if not p:
        return json.dumps({"error": f"paper {paper_id} not found"})
    text, source = _doc_text(p, "en")
    if not text:
        return json.dumps({"available": False, "reason": "no parsed full.md (run parse first)",
                           "source": source, "count": 0, "total_hits": 0, "items": []})
    context_chars = max(0, min(int(context_chars), 6000))
    max_hits = max(1, min(int(max_hits), 100))
    lowered = text.lower()
    needle = query.lower()
    total_hits = lowered.count(needle)
    items = []
    start = 0
    while len(items) < max_hits:
        index = lowered.find(needle, start)
        if index < 0:
            break
        context_start = max(0, index - context_chars)
        context_end = min(len(text), index + len(query) + context_chars)
        items.append({
            "offset": index,
            "context_start": context_start,
            "context_end": context_end,
            "text": text[context_start:context_end],
        })
        start = index + len(needle)
    return json.dumps({
        "available": True, "source": source, "query": query,
        "count": len(items), "total_hits": total_hits,
        "has_more": len(items) < total_hits,
        "total": len(text), "items": items,
    }, ensure_ascii=False)


@mcp.tool
def read_translation(paper_id: int, offset: int = 0, limit: int = 8000) -> str:
    """读取论文中文翻译（人工编辑版优先，否则 chunk 拼接）。

    分页读取：offset/limit 为字符偏移；toc（章节标题 + 字符偏移）仅随
    offset=0 的首页返回。

    Returns:
        JSON：{available, source(manual_edit|auto), total, offset, text, has_more,
               toc（仅 offset=0）}
    """
    from backend.api.reader import _doc_text
    from backend.services.doc_text import extract_toc
    conn = get_conn()
    try:
        p = _repo_get_paper(conn, paper_id)
    finally:
        conn.close()
    if not p:
        return json.dumps({"error": f"paper {paper_id} not found"})
    text, source = _doc_text(p, "zh")
    if not text:
        return json.dumps({"available": False, "reason": "no translation yet (run translate first)",
                           "source": source, "total": 0, "text": "", "has_more": False})
    limit = max(1, min(int(limit), 50000))
    offset = max(0, int(offset))
    snippet = text[offset:offset + limit]
    out = {
        "available": True, "source": source, "total": len(text),
        "offset": offset, "text": snippet, "has_more": offset + limit < len(text),
    }
    if offset == 0:
        out["toc"] = extract_toc(text)
    return json.dumps(out, ensure_ascii=False)


@mcp.tool
def get_summary(paper_id: int) -> str:
    """读取论文的结构化中文总结（最新版本）。"""
    conn = get_conn()
    try:
        p = _repo_get_paper(conn, paper_id)
    finally:
        conn.close()
    if not p:
        return json.dumps({"error": f"paper {paper_id} not found"})
    row = read_latest_summary(paper_id)
    if not row:
        return json.dumps({"available": False, "reason": "no summary yet (run summarize first)"})
    return json.dumps({"available": True, "version": row["version"], "summary": row["content_md"]},
                      ensure_ascii=False)


# ── 引文网络 ────────────────────────────────────────────


@mcp.tool
def get_citations(paper_id: int, direction: str = "refs") -> str:
    """读取一篇论文的引文关系。

    Args:
        paper_id: 论文 ID
        direction: "refs" = 它引用了谁（参考文献）；"cited_by" = 谁引用了它

    Returns:
        JSON：{count, items: [{dst_paper_id?, dst_title, dst_year, dst_openalex_id}]}
    """
    if direction not in ("refs", "cited_by"):
        return json.dumps({"error": "direction must be one of refs|cited_by"})
    conn = get_conn()
    try:
        if direction == "refs":
            rows = conn.execute(
                """SELECT dst_paper_id, dst_title, dst_year, dst_openalex_id
                   FROM citations WHERE src_paper_id = ?
                     AND (dst_paper_id IS NULL OR dst_paper_id != src_paper_id)""",
                (paper_id,)).fetchall()
        else:
            rows = conn.execute(
                """SELECT src_paper_id AS dst_paper_id,
                          (SELECT title FROM papers WHERE id = src_paper_id) AS dst_title,
                          (SELECT year FROM papers WHERE id = src_paper_id) AS dst_year,
                          NULL AS dst_openalex_id
                   FROM citations WHERE dst_paper_id = ?
                     AND src_paper_id != dst_paper_id""", (paper_id,)).fetchall()
    except Exception as e:  # noqa: BLE001
        return json.dumps({"error": str(e)})
    finally:
        conn.close()
    return json.dumps({"count": len(rows), "items": [dict(r) for r in rows]},
                      ensure_ascii=False, default=str)


# ── 批注 ────────────────────────────────────────────────


@mcp.tool
def get_annotations(paper_id: int) -> str:
    """读取论文的全部批注（高亮/评论）。"""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM annotations WHERE paper_id = ? ORDER BY id", (paper_id,)).fetchall()
    except Exception as e:  # noqa: BLE001
        return json.dumps({"error": str(e)})
    finally:
        conn.close()
    return json.dumps({"count": len(rows), "items": [dict(r) for r in rows]},
                      ensure_ascii=False, default=str)


@mcp.tool
def add_annotation(paper_id: int, quote: str, note: str = "",
                   anchor: str = "en") -> str:
    """给论文添加一条批注（source='agent'）。

    Args:
        paper_id: 论文 ID
        quote: 引用原文片段
        note: 批注内容
        anchor: 锚定文档（en|zh|summary）
    """
    if anchor not in ("en", "zh", "summary"):
        return json.dumps({"error": "anchor must be one of en|zh|summary"})
    payload = json.dumps({"quote": quote, "note": note, "position": None}, ensure_ascii=False)
    conn = get_conn()
    try:
        if not _repo_get_paper(conn, paper_id):
            return json.dumps({"error": f"paper {paper_id} not found"})
        cur = conn.execute(
            """INSERT INTO annotations
               (paper_id, type, anchor, payload_json, source, created_by_user_id)
               VALUES (?, 'comment', ?, ?, 'agent', NULL)""",
            (paper_id, anchor, payload),
        )
        aid = cur.lastrowid
    except Exception as e:  # noqa: BLE001
        return json.dumps({"error": str(e)})
    finally:
        conn.close()
    return json.dumps({"id": aid})


def _http_tools():
    # HTTP 传输不注册本地文件入库工具：远程调用方不应能读写服务器本地路径。
    # stdio 传输保留全部 19 个工具。
    return (
        search_papers, semantic_search, ingest_by_url, get_task, list_tasks,
        get_paper, get_paper_by_doi, get_papers_by_dois, get_papers,
        search_library, read_fulltext, search_fulltext, read_translation,
        get_summary, get_citations, get_annotations, add_annotation,
    )


def create_http_app(host: str = "127.0.0.1"):
    from starlette.applications import Starlette
    from starlette.routing import Mount
    from backend.config import validate_bind_security

    validate_bind_security(host)
    remote = FastMCP("paper-survey-http", version=get_version(), tools=_http_tools())
    mcp_app = remote.http_app(path="/mcp")
    return Starlette(routes=[Mount("/", app=mcp_app)], lifespan=mcp_app.lifespan)


def _prepare_storage() -> None:
    from backend.config import ensure_dirs
    from backend.db.authz_schema import require_authz_columns
    from backend.db.conn import init_db

    ensure_dirs()
    init_db()
    require_authz_columns()


def main(transport: str = "stdio", host: str = "127.0.0.1", port: int = 5051) -> None:
    if transport not in {"stdio", "http"}:
        raise ValueError("transport must be stdio or http")
    _prepare_storage()
    if transport == "stdio":
        mcp.run(transport="stdio")
        return
    import uvicorn
    uvicorn.run(create_http_app(host=host), host=host, port=port)


if __name__ == "__main__":
    main()
