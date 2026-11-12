"""TranslateWorker — 单 worker 内顺序翻译所有 chunk。

设计：
  * 不 fan-out 子 task（避免单线程 scheduler 死锁）
  * 每个 chunk 单独调 LLM（OpenAI 兼容端点），timeout=180s
  * 每翻译完一个 chunk 立即 commit translations 行
  * force=False 时跳过已 ok 的 chunk（断点续传）
  * 任何一个 chunk 失败 → 整个 task error，但已完成的 chunk 不丢失（重试时跳过）
"""
import time

from backend.config import resolve_path
from backend.db.conn import get_conn
from backend.db.repo import get_paper
from backend.external.llm import LLMClient
from backend.pipeline.translator import (
    Chunk, build_system_prompt, extract_tail_sentence,
    split_into_chunks, translate_chunk,
)
from backend.services.doc_text import read_en_override
from backend.workers.runner import register_worker, WorkerContext


@register_worker("translate")
def translate_worker(ctx: WorkerContext, payload: dict) -> None:
    paper_id = int(payload["paper_id"])
    force = bool(payload.get("force"))

    conn = get_conn()
    try:
        paper = get_paper(conn, paper_id)
    finally:
        conn.close()
    if not paper:
        raise ValueError(f"paper_id={paper_id} not found")

    # 翻译输入源优先级：EN override (translations.chunk_index=-1, en_text 非空) > parsed_path
    en_override = read_en_override(paper_id)
    if en_override:
        _set_status(paper_id, "processing")
        ctx.info(f"using EN override from manual edit ({len(en_override)} chars)")
        full_md = en_override
    else:
        parsed = resolve_path(paper.get("parsed_path"))
        if not parsed:
            raise FileNotFoundError(
                f"parsed full.md not found for paper_id={paper_id} "
                f"(legacy path not migrated? run paper db migrate-v3)"
            )
        _set_status(paper_id, "processing")
        ctx.info(f"reading {parsed}")
        full_md = parsed.read_text(encoding="utf-8", errors="replace")
    try:
        chunks = split_into_chunks(full_md)
        ctx.info(f"split into {len(chunks)} chunks (system_prompt built once)")

        # 读取已有 translations
        existing = _existing_translations(paper_id)

        # force=true 时清空旧 chunks，但保留 chunk_index=-1（人工修改覆盖层行）
        if force and existing:
            conn = get_conn()
            try:
                conn.execute(
                    "DELETE FROM translations WHERE paper_id = ? AND chunk_index >= 0",
                    (paper_id,),
                )
            finally:
                conn.close()
            # 同步清掉 in-memory 视图里 chunk_index>=0 的项
            existing = {k: v for k, v in existing.items() if k < 0}
            ctx.info("force mode: cleared existing chunk-level translations (kept overrides)")

        client = LLMClient()
        client.call_meta = {"kind": "translate", "paper_id": paper_id,
                            "task_id": ctx.task_id}
        system_prompt = build_system_prompt()
    except Exception:
        _set_status(paper_id, "error")
        raise
    prev_tail = ""

    error_count = 0
    done_count = 0
    skipped_count = 0
    total = len(chunks)

    for i, c in enumerate(chunks):
        existing_row = existing.get(c.index)
        if existing_row and existing_row.get("status") in ("ok", "skipped") and not force:
            skipped_count += 1
            prev_tail = extract_tail_sentence(existing_row.get("zh_text") or "")
            ctx.progress((i + 1) / total)
            continue

        # references 整段不翻译
        if c.is_references:
            _persist_chunk(paper_id, c, c.en_text, status="skipped")
            ctx.info(f"chunk #{c.index} [References]: skipped (preserve raw)")
            done_count += 1
            ctx.progress((i + 1) / total)
            continue

        try:
            t0 = time.time()
            zh_text, meta = translate_chunk(
                c, client=client, system_prompt=system_prompt,
                context_tail=prev_tail, timeout=180,
            )
            _persist_chunk(paper_id, c, zh_text, status="ok")
            dt = time.time() - t0
            ctx.info(f"chunk #{c.index} [{c.heading[:40]}]: {meta['en_len']}→{meta['zh_len']} chars in {dt:.1f}s")
            if meta.get("warnings"):
                for w in meta["warnings"]:
                    ctx.warn(f"chunk #{c.index}: {w}")
            done_count += 1
            prev_tail = extract_tail_sentence(zh_text)
        except Exception as exc:  # noqa: BLE001
            _persist_chunk(paper_id, c, "", status="error")
            ctx.error(f"chunk #{c.index} failed: {exc}")
            error_count += 1

        ctx.progress((i + 1) / total)

    ctx.info(f"summary: done={done_count} skipped={skipped_count} error={error_count}")

    if error_count > 0:
        if done_count > 0 or skipped_count > 0:
            _set_status(paper_id, "partial")
            ctx.warn(
                f"{error_count}/{total} chunks failed but {done_count + skipped_count} succeeded; "
                f"status=partial. Rerun without force to retry only failed chunks."
            )
            return
        _set_status(paper_id, "error")
        raise RuntimeError(f"{error_count}/{total} chunks failed (all failed)")

    _set_status(paper_id, "ok")
    ctx.info("status_translated = ok")


def _existing_translations(paper_id: int) -> dict[int, dict]:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM translations WHERE paper_id = ?", (paper_id,)
        ).fetchall()
        return {int(r["chunk_index"]): dict(r) for r in rows}
    finally:
        conn.close()


def _persist_chunk(paper_id: int, c: Chunk, zh_text: str, *, status: str) -> None:
    conn = get_conn()
    try:
        conn.execute(
            """INSERT INTO translations (paper_id, chunk_index, en_text, zh_text, status, source)
               VALUES (?, ?, ?, ?, ?, 'auto')
               ON CONFLICT(paper_id, chunk_index) DO UPDATE SET
                 zh_text = excluded.zh_text,
                 status = excluded.status,
                 updated_at = CURRENT_TIMESTAMP""",
            (paper_id, c.index, c.en_text, zh_text, status),
        )
    finally:
        conn.close()


def _set_status(paper_id: int, status: str) -> None:
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE papers SET status_translated = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (status, paper_id),
        )
    finally:
        conn.close()