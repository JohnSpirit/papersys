"""EmbedWorker - 把 parsed full.md 向量化写入 embeddings 表（RAG 层）。

payload: {"paper_id": int}

策略：
  * 复用 translator 的 chunk 切分（H1/H2 + 段落边界），文本一致性高
  * EN override（chunk_index=-1）优先，与翻译输入源一致
  * 每批 embed 后立即 commit（断点续传：重跑只补缺失 chunk）
  * status_embedded 状态列（第 6 状态列）
"""
from backend.config import resolve_path
from backend.db.conn import get_conn
from backend.db.repo import get_paper
from backend.db.vectors import pack as _pack
from backend.external.hosted_retrieval import HostedRetrievalClient
from backend.pipeline.translator import split_into_chunks
from backend.services.doc_text import read_en_override
from backend.workers.runner import register_worker, WorkerContext


@register_worker("embed")
def embed_worker(ctx: WorkerContext, payload: dict) -> None:
    paper_id = int(payload["paper_id"])
    force = bool(payload.get("force"))

    conn = get_conn()
    try:
        paper = get_paper(conn, paper_id)
    finally:
        conn.close()
    if not paper:
        raise ValueError(f"paper_id={paper_id} not found")

    # 输入源优先级与翻译一致：EN override > parsed full.md
    override = read_en_override(paper_id)
    if override:
        full_md = override
    else:
        parsed = resolve_path(paper.get("parsed_path"))
        if not parsed:
            raise FileNotFoundError(
                f"parsed full.md missing for paper_id={paper_id} "
                f"(legacy path not migrated? run paper db migrate-v3)"
            )
        full_md = parsed.read_text(encoding="utf-8", errors="replace")

    chunks = [c for c in split_into_chunks(full_md) if c.en_text and c.en_text.strip()]
    ctx.info(f"embedding {len(chunks)} chunks (force={force})")
    _set_status(paper_id, "processing")

    try:
        client = HostedRetrievalClient()
        model = client.config["embed_model"]

        existing = _existing_chunk_indexes(paper_id)
        done = skipped = 0
        batch: list[tuple[int, str]] = []

        def flush():
            nonlocal done
            if not batch:
                return
            texts = [t for _, t in batch]
            vecs = client.embed(texts)
            conn = get_conn()
            try:
                for (idx, text), vec in zip(batch, vecs):
                    conn.execute(
                        """INSERT INTO embeddings (paper_id, chunk_id, chunk_index, text, vector, dim, model)
                           VALUES (?, ?, ?, ?, ?, ?, ?)
                           ON CONFLICT(paper_id, chunk_index) DO UPDATE SET
                             text = excluded.text, vector = excluded.vector,
                             dim = excluded.dim, model = excluded.model""",
                        (paper_id, f"{paper_id}:{idx}", idx, text, _pack(vec), len(vec), model),
                    )
                conn.commit()
            finally:
                conn.close()
            done += len(batch)
            ctx.progress(min(0.95, done / max(len(chunks), 1)))
            batch.clear()

        for c in chunks:
            if not force and c.index in existing:
                skipped += 1
                continue
            batch.append((c.index, c.en_text))
            if len(batch) >= 10:
                flush()
        flush()

        # 清掉多余的历史行（force 后 chunk 数减少的情况）
        valid = {c.index for c in chunks}
        conn = get_conn()
        try:
            stale = conn.execute(
                "SELECT chunk_index FROM embeddings WHERE paper_id = ?", (paper_id,)
            ).fetchall()
            stale_idx = [r["chunk_index"] for r in stale if r["chunk_index"] not in valid]
            if stale_idx:
                conn.execute(
                    "DELETE FROM embeddings WHERE paper_id = ? AND chunk_index IN "
                    f"({','.join('?' * len(stale_idx))})",
                    [paper_id, *stale_idx],
                )
                conn.commit()
        finally:
            conn.close()
    except Exception:
        _set_status(paper_id, "error")
        raise

    _set_status(paper_id, "ok")
    ctx.info(f"embedded {done} chunks (skipped {skipped} existing); status_embedded=ok")


def _existing_chunk_indexes(paper_id: int) -> set[int]:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT chunk_index FROM embeddings WHERE paper_id = ?", (paper_id,)
        ).fetchall()
        return {r["chunk_index"] for r in rows}
    finally:
        conn.close()


def _set_status(paper_id: int, status: str) -> None:
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE papers SET status_embedded = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (status, paper_id),
        )
    finally:
        conn.close()
