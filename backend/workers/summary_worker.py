"""SummaryWorker — 调 LLM（OpenAI 兼容端点）生成结构化总结，写入 summaries 表。

不写文件系统（summaries 表是真相源）。
"""
from backend.config import CONFIG, resolve_path
from backend.db.conn import get_conn
from backend.db.repo import get_paper
from backend.external.llm import LLMClient
from backend.services.doc_text import read_en_override
from backend.workers.runner import register_worker, WorkerContext


SYSTEM_PROMPT = """你是一个神经科学论文总结助手。请基于给定的英文论文 Markdown，生成结构化的中文总结。

输出格式（严格遵循）：
# {论文标题}

**元数据**
- 作者: ...
- 期刊: ...
- 年份: ...
- DOI: ...

**摘要**
（一段话提炼研究问题、方法、关键结论，200-400 字）

**方法概述**
- 实验对象 / 数据来源
- 关键实验范式 / 分析方法
- 重要参数

**关键发现**
1. ...
2. ...
3. ...

**结论与意义**
（2-4 句话）

**局限性**
- ...

要求：
- 全文中文（专业术语保留英文，如 LIP / MT / choice probability）
- 不复述参考文献
- 不超过 1500 字
"""


@register_worker("summary")
def summary_worker(ctx: WorkerContext, payload: dict) -> None:
    paper_id = int(payload["paper_id"])
    # 字符上限：任务 payload 可覆盖，默认 config.toml [llm].max_input_chars
    #（2026-10-17 起；旧硬编码 30000 对全库 91% 论文过紧，见 config 注释的实测分布）
    max_chars = int(payload.get("max_input_chars")
                    or CONFIG["llm"].get("max_input_chars", 30000))

    conn = get_conn()
    try:
        paper = get_paper(conn, paper_id)
    finally:
        conn.close()
    if not paper:
        raise ValueError(f"paper_id={paper_id} not found")

    # SI 附件（正文补充材料）不生成总结：内容是图表/方法补充而非完整论文，
    # 而提示词按完整论文设计，产出的"总结"挂在母论文标题下具有误导性。
    # 标 skipped（非 missing——missing 表示"还没做"，skipped 表示"对此类记录
    # 禁用"，2026-10-17 用户定）。
    if paper.get("attachment_kind") == "si":
        ctx.info("SI attachment: summary disabled, marking skipped")
        _set_status(paper_id, "skipped")
        return

    # 输入源优先级与 translate/embed 一致：EN 覆盖层（人工复核版）> parsed full.md
    override = read_en_override(paper_id)
    if override:
        full_md = override
        input_source = "manual_edit"
    else:
        parsed = resolve_path(paper.get("parsed_path"))
        if not parsed:
            raise FileNotFoundError(
                f"parsed full.md missing for paper_id={paper_id} "
                f"(legacy path not migrated? run paper db migrate-v3)"
            )
        full_md = parsed.read_text(encoding="utf-8", errors="replace")
        input_source = "parsed"

    _set_status(paper_id, "processing")

    try:
        # 按内容剥参考文献：仅当显式 References 标题（H1-H6，大小写不敏感）命中
        # 时才剥——split_references 的编号列表回退正则会把无标题老论文的正文
        # 编号列表误当参考文献起点，对总结宁可带上文献也不截肢正文
        # （translator.py:91 的 chunking 场景可容忍误切，总结场景不可）。
        from backend.pipeline.translator import REFERENCES_RE, split_references
        if REFERENCES_RE.search(full_md):
            body, refs = split_references(full_md)
            ctx.info(f"references stripped: {len(refs)} chars")
            full_md = body

        # 超长按段落/行边界截断（对齐 translator._split_by_chars 的切法），
        # 避免硬切把最后一个段落切断在半句上。
        if len(full_md) > max_chars:
            cut = full_md.rfind("\n\n", 0, max_chars)
            if cut < max_chars // 5:
                cut = full_md.rfind("\n", 0, max_chars)
            if cut < max_chars // 5:
                cut = max_chars
            ctx.warn(f"truncating input {len(full_md)} -> {cut} chars (boundary)")
            full_md = full_md[:cut].rstrip()

        meta_block = "\n".join([
            f"- 标题: {paper.get('title') or ''}",
            f"- 作者: {paper.get('authors') if isinstance(paper.get('authors'), str) else ''}",
            f"- 期刊: {paper.get('journal') or ''}",
            f"- 年份: {paper.get('year') or ''}",
            f"- DOI: {paper.get('doi') or ''}",
        ])
        user_msg = f"已知元数据：\n{meta_block}\n\n请基于以下原文生成总结：\n\n{full_md}"

        ctx.info(f"calling LLM summary (input={len(user_msg)} chars, source={input_source})")
        client = LLMClient()
        client.call_meta = {"kind": "summary", "paper_id": paper_id,
                            "task_id": ctx.task_id}
        content_md = client.chat(SYSTEM_PROMPT, user_msg, timeout=180)
        ctx.info(f"got {len(content_md)} chars")
    except Exception:
        _set_status(paper_id, "error")
        raise

    # 写入 summaries 表（每次都新增 version）
    conn = get_conn()
    try:
        conn.execute("BEGIN")
        cur = conn.execute(
            "SELECT IFNULL(MAX(version), 0) FROM summaries WHERE paper_id = ?",
            (paper_id,),
        )
        next_v = (cur.fetchone()[0] or 0) + 1
        conn.execute(
            "INSERT INTO summaries (paper_id, content_md, version, source) VALUES (?, ?, ?, 'auto')",
            (paper_id, content_md, next_v),
        )
        conn.execute(
            "UPDATE papers SET status_summary = 'ok', updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (paper_id,),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        _set_status(paper_id, "error")
        raise
    finally:
        conn.close()
    ctx.info(f"summary v{next_v} saved")


def _set_status(paper_id: int, status: str) -> None:
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE papers SET status_summary = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (status, paper_id),
        )
    finally:
        conn.close()