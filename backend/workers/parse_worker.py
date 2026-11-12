"""ParseWorker：调用 MinerU 解析 PDF，结果写入 library/{year}/{dir}/。

payload: {"paper_id": int}
"""
from pathlib import Path

from backend.config import CONFIG, library_root, resolve_path
from backend.db.conn import get_conn
from backend.db.repo import get_paper, upsert_paper, update_fts
from backend.external.mineru import MinerUClient
from backend.pipeline.pdf_classify import pdfinfo, classify_pdf, is_real_pdf
from backend.workers.runner import register_worker, WorkerContext


@register_worker("parse")
def parse_worker(ctx: WorkerContext, payload: dict) -> None:
    paper_id = int(payload["paper_id"])
    conn = get_conn()
    try:
        paper = get_paper(conn, paper_id)
    finally:
        conn.close()

    if not paper:
        raise ValueError(f"paper_id={paper_id} not found")
    pdf_path = resolve_path(paper.get("pdf_path"))
    if not pdf_path:
        raise FileNotFoundError(
            f"PDF not found: {paper.get('pdf_path')} (legacy path not migrated? run paper db migrate-v3)"
        )
    if not is_real_pdf(str(pdf_path)):
        raise RuntimeError(
            f"not a valid PDF (magic bytes mismatch): {pdf_path}. "
            "Likely a non-PDF file archived by mistake."
        )

    _set_status(paper_id, "processing")
    try:
        _parse_paper(ctx, paper, pdf_path)
    except Exception as exc:
        try:
            _set_status(paper_id, "error")
        except Exception as status_exc:  # noqa: BLE001
            try:
                ctx.error(
                    f"failed to set status_parsed=error for paper_id={paper_id}: "
                    f"{status_exc}; original error: {exc}"
                )
            except Exception:  # noqa: BLE001,S110
                pass
        raise


def _parse_paper(ctx: WorkerContext, paper: dict, pdf_path: Path) -> None:
    paper_id = int(paper["id"])
    ctx.info(f"parsing {paper['dir_name']}")
    ctx.progress(0.05)

    # PDF 分类（本地模式下仅用于记录，后端由 config 决定；云模式下决定 model）
    info = pdfinfo(str(pdf_path))
    kind = classify_pdf(info)
    model = "vlm" if kind == "scan" else "pipeline"
    is_ocr = (kind == "scan")
    ctx.info(f"pdf classified as {kind} -> model={model}, is_ocr={is_ocr}")
    ctx.progress(0.1)

    # 目标目录（本地文库）
    year_dir = str(paper["year"]) if paper["year"] else "unknown"
    parsed_dir = library_root() / year_dir / paper["dir_name"]
    parsed_dir.mkdir(parents=True, exist_ok=True)

    client = MinerUClient()
    mode = CONFIG["mineru"].get("mode", "cloud")
    ctx.info(f"submitting to MinerU (mode={mode})")

    # 统一入口：parse_pdf 按 mode 分发
    # - local: 同步调用 /file_parse，直接返回结果
    # - cloud: submit_v4 + poll_v4 + download_zip
    result = client.parse_pdf(str(pdf_path), parsed_dir, model=model, is_ocr=is_ocr)
    full_md = result["full_md"]
    ctx.info(f"unpacked -> {parsed_dir}")
    ctx.progress(0.9)

    # 记录实际使用的后端模型
    model = result.get("model", model)

    parsed_text = full_md.read_text(encoding="utf-8", errors="replace")
    # 扫描合订本尾巴页剥离：第 1 页是别文末尾时，删到标题锚点（保守判定，
    # 见 services/doc_text.strip_preceding_tail）；标题缺失时不动。
    from backend.services.doc_text import strip_preceding_tail
    anchor_title = paper.get("title") or info.get("pdf_title") or ""
    cleaned, stripped = strip_preceding_tail(parsed_text, anchor_title)
    if stripped:
        full_md.write_text(cleaned, encoding="utf-8")
        parsed_text = cleaned
        ctx.warn(f"stripped {stripped} chars of preceding-article tail (title anchor)")
    parsed_chars = len(parsed_text)
    ctx.info(f"full.md: {parsed_chars} chars")

    # 更新 DB + FTS 索引（parsed_path 存相对路径）
    conn = get_conn()
    try:
        conn.execute("BEGIN")
        upsert_paper(conn, {
            "dir_name":      paper["dir_name"],
            "year":          paper["year"],
            "first_author":  paper["first_author"],
            "parsed_path":   str(full_md.relative_to(library_root())),
            "parsed_chars":  parsed_chars,
            "mineru_model":  model,
            "status_parsed": "ok",
        })
        update_fts(conn, paper_id,
                   title=paper.get("title") or "",
                   abstract=paper.get("abstract") or "",
                   parsed_text=parsed_text)
        conn.execute("COMMIT")
    finally:
        conn.close()


def _set_status(paper_id: int, status: str) -> None:
    conn = get_conn()
    try:
        conn.execute(
            "UPDATE papers SET status_parsed = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (status, paper_id),
        )
    finally:
        conn.close()