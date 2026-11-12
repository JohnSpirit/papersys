"""预览接口：PDF 页缩略图 + Markdown 切片。"""
import io
from pathlib import Path

import fitz  # PyMuPDF
from flask import Blueprint, Response, jsonify, request, abort, send_file

from backend.config import resolve_path
from backend.db.conn import get_conn
from backend.db.repo import get_paper
from backend.services.doc_text import read_doc_text

bp = Blueprint("preview", __name__, url_prefix="/api")


@bp.get("/papers/<int:paper_id>/pdf")
def paper_pdf(paper_id: int):
    """渲染 PDF 单页为 PNG。

    参数:
        page (int): 1-indexed 页码，默认 1
        dpi (int): 默认 100，最高 200
    """
    conn = get_conn()
    try:
        p = get_paper(conn, paper_id)
    finally:
        conn.close()
    if not p:
        abort(404, "paper not found")
    pdf_path = resolve_path(p.get("pdf_path"))
    if not pdf_path:
        abort(404, "PDF file not found (legacy path not migrated? run paper db migrate-v3)")

    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        abort(400, "invalid page")
    try:
        dpi = int(request.args.get("dpi", 110))
    except ValueError:
        abort(400, "invalid dpi")
    if not 50 <= dpi <= 200:
        abort(400, f"dpi out of range (50..200), got {dpi}")

    try:
        doc = fitz.open(pdf_path)
    except Exception as e:
        abort(500, f"open pdf failed: {e}")

    try:
        if page > doc.page_count:
            abort(400, f"page out of range (1..{doc.page_count})")
        pg = doc.load_page(page - 1)
        zoom = dpi / 72.0
        pix = pg.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        png_bytes = pix.tobytes("png")
    finally:
        doc.close()

    return Response(
        png_bytes,
        mimetype="image/png",
        headers={
            "Cache-Control": "private, max-age=300",
            "X-PDF-Pages": str(p.get("pdf_pages") or ""),
        },
    )


@bp.get("/papers/<int:paper_id>/preview")
def paper_md_preview(paper_id: int):
    """返回 markdown 切片。

    参数:
        type (str): en | zh | summary  (默认 en)
        offset (int): 起始字符位置，默认 0
        limit (int): 字符数，默认 4096，最大 16384
    """
    conn = get_conn()
    try:
        p = get_paper(conn, paper_id)
    finally:
        conn.close()
    if not p:
        abort(404)

    typ = request.args.get("type", "en")
    if typ not in ("en", "zh", "summary"):
        abort(400, "type must be en|zh|summary")

    # 覆盖层规则（EN/ZH/Summary）统一在 backend/services/doc_text.py
    text, source = read_doc_text(p, typ)
    if source == "missing":
        reasons = {
            "en": "parsed full.md missing",
            "zh": "no translations yet (run /retranslate)",
            "summary": "no summary yet (run /resummarize)",
        }
        return jsonify({
            "type": typ, "available": False,
            "reason": reasons[typ],
            "text": "", "total_size": 0,
        })
    path: str | None = p.get("parsed_path")

    try:
        offset = max(0, int(request.args.get("offset", 0)))
        limit = min(16384, max(1, int(request.args.get("limit", 4096))))
    except ValueError:
        abort(400, "invalid offset/limit")

    total = len(text)
    snippet = text[offset:offset + limit]
    return jsonify({
        "type":         typ,
        "available":    True,
        "path":         path,
        "source":       source,
        "total_size":   total,
        "offset":       offset,
        "limit":        limit,
        "text":         snippet,
        "has_more":     offset + limit < total,
    })


@bp.get("/papers/<int:paper_id>/images/<path:name>")
def paper_image(paper_id: int, name: str):
    """读取 library/{year}/{dir}/images/{name}。"""
    conn = get_conn()
    try:
        p = get_paper(conn, paper_id)
    finally:
        conn.close()
    if not p or not p.get("parsed_path"):
        abort(404)
    parsed_dir = resolve_path(p["parsed_path"])
    if not parsed_dir:
        abort(404)
    parsed_dir = parsed_dir.parent
    img_path = parsed_dir / "images" / name
    # 防止路径穿越：锚点限定在 images/ 子目录内
    try:
        img_path.resolve().relative_to((parsed_dir / "images").resolve())
    except ValueError:
        abort(403)
    if not img_path.exists():
        abort(404)
    return send_file(img_path)


@bp.get("/papers/<int:paper_id>/translations")
def paper_translations(paper_id: int):
    """列出该论文的所有 chunk-level translation 记录。"""
    conn = get_conn()
    try:
        rows = conn.execute(
            """SELECT chunk_index, status, source,
                      LENGTH(en_text) AS en_len,
                      LENGTH(zh_text) AS zh_len,
                      updated_at
               FROM translations WHERE paper_id = ?
               ORDER BY chunk_index""",
            (paper_id,),
        ).fetchall()
    finally:
        conn.close()
    return jsonify({"items": [dict(r) for r in rows], "count": len(rows)})
