"""Inbox API + 单论文操作（reparse / reenrich）。"""
import re
import secrets

from flask import Blueprint, abort, jsonify, request

from backend.config import ensure_inbox_dir
from backend.db import tasks_repo
from backend.db.conn import get_conn
from backend.db.repo import get_paper

bp = Blueprint("actions", __name__, url_prefix="/api")


@bp.get("/inbox")
def inbox_list():
    inbox = ensure_inbox_dir()
    items = []
    for f in sorted(inbox.glob("*.pdf")):
        st = f.stat()
        items.append({
            "name":  f.name,
            "path":  str(f),
            "size_bytes": st.st_size,
            "mtime": st.st_mtime,
        })
    return jsonify({"items": items, "count": len(items)})


@bp.post("/inbox/scan")
def inbox_scan():
    """为 inbox 中所有 PDF 创建 pipeline task（默认入库后继续解析/富化/嵌入/总结）。"""
    from backend.pipeline.steps import normalize_feed_steps
    body = request.get_json(silent=True) or {}
    try:
        steps = normalize_feed_steps(body.get("steps"))
    except ValueError as e:
        abort(400, str(e))
    inbox = ensure_inbox_dir()
    pdfs = sorted(inbox.glob("*.pdf"))
    created = []
    for f in pdfs:
        tid = tasks_repo.create_task(
            kind="pipeline", payload={"inbox_path": str(f), "steps": steps},
        )
        created.append({"file": f.name, "task_id": tid})
    return jsonify({"queued": created, "count": len(created), "steps": steps})


@bp.post("/inbox/upload")
def inbox_upload():
    """multipart 上传 PDF -> 落到 inbox -> 创建 pipeline task。"""
    if "file" not in request.files:
        abort(400, "missing file field")
    f = request.files["file"]
    if not f.filename or not f.filename.lower().endswith(".pdf"):
        abort(400, "only .pdf supported")
    from backend.pipeline.steps import normalize_feed_steps
    try:
        steps = normalize_feed_steps(request.form.get("steps"))
    except ValueError as e:
        abort(400, str(e))
    inbox = ensure_inbox_dir()
    # multipart 原始 filename 不可信：含 ../ 时 `inbox / f.filename` 会逃出 inbox
    # 目录（路径穿越写任意 .pdf 路径）。与 /api/inbox/from_url 用同一套消毒规则。
    filename = re.sub(r"[^\w\-_. ]", "_", f.filename)
    target = inbox / filename
    if target.exists():
        target = inbox / f"{target.stem}_{secrets.token_hex(4)}.pdf"
    f.save(target)
    tid = tasks_repo.create_task(
        kind="pipeline", payload={"inbox_path": str(target), "steps": steps},
    )
    return jsonify({"path": str(target), "task_id": tid, "steps": steps})


def _paper_or_404(paper_id: int) -> dict:
    conn = get_conn()
    try:
        paper = get_paper(conn, paper_id)
    finally:
        conn.close()
    if not paper:
        abort(404)
    return paper


def _queue_paper_task(kind: str, paper_id: int, payload: dict):
    task_id, reused = tasks_repo.create_or_reuse_active_task(
        kind=kind, payload=payload, paper_id=paper_id,
    )
    return jsonify({"task_id": task_id, "reused": reused})


@bp.post("/papers/<int:paper_id>/reparse")
def paper_reparse(paper_id: int):
    paper = _paper_or_404(paper_id)
    if not paper.get("pdf_path"):
        abort(400, "paper has no PDF")
    return _queue_paper_task("parse", paper_id, {"paper_id": paper_id})


@bp.post("/papers/<int:paper_id>/reenrich")
def paper_reenrich(paper_id: int):
    _paper_or_404(paper_id)
    return _queue_paper_task("enrich", paper_id, {"paper_id": paper_id})


@bp.post("/papers/<int:paper_id>/bibtex")
def paper_refresh_bibtex(paper_id: int):
    """同步刷新单篇论文的 BibTeX（Crossref content negotiation）。

    轻量操作（单次 HTTP），同步执行不入队：成功则入库并返回 bibtex 文本，
    无 DOI / Crossref 无命中时返回 404 / 422。
    """
    from backend.external.crossref import CrossrefClient

    paper = _paper_or_404(paper_id)
    doi = paper.get("doi")
    if not doi:
        abort(422, "paper has no DOI; cannot fetch BibTeX")

    cc = CrossrefClient()
    bibtex = cc.fetch_bibtex(doi)
    if not bibtex:
        abort(502, f"Crossref returned no BibTeX for doi={doi}")

    conn = get_conn()
    try:
        conn.execute("BEGIN")
        conn.execute(
            "UPDATE papers SET bibtex = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (bibtex, paper_id),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()
    return jsonify({"bibtex": bibtex, "doi": doi})


@bp.post("/papers/<int:paper_id>/retranslate")
def paper_retranslate(paper_id: int):
    paper = _paper_or_404(paper_id)
    if paper.get("status_parsed") != "ok":
        abort(400, "paper must be parsed before translating")
    force = (request.json or {}).get("force", False) if request.is_json else False
    return _queue_paper_task(
        "translate", paper_id, {"paper_id": paper_id, "force": bool(force)},
    )


@bp.post("/papers/<int:paper_id>/resummarize")
def paper_resummarize(paper_id: int):
    paper = _paper_or_404(paper_id)
    if paper.get("status_parsed") != "ok":
        abort(400, "paper must be parsed before summarizing")
    return _queue_paper_task("summary", paper_id, {"paper_id": paper_id})


@bp.post("/papers/<int:paper_id>/reembed")
def paper_reembed(paper_id: int):
    paper = _paper_or_404(paper_id)
    if paper.get("status_parsed") != "ok":
        abort(400, "paper must be parsed before embedding")
    force = (request.json or {}).get("force", False) if request.is_json else False
    return _queue_paper_task(
        "embed", paper_id, {"paper_id": paper_id, "force": bool(force)},
    )


@bp.post("/papers/<int:paper_id>/pipeline")
def paper_pipeline(paper_id: int):
    """一键串行执行 parse -> enrich -> embed -> translate -> summary。

    可选 JSON body:
      {
        "steps":           ["parse","enrich","embed","translate","summary"],
        "force_steps":     ["translate"],   # 这些步骤无论当前 status 都重跑
        "translate_force": false,           # 透传：清空已有 chunks 重译
        "on_error":        "stop"           # "stop" | "continue"
      }
    """
    paper = _paper_or_404(paper_id)
    if not paper.get("pdf_path"):
        abort(400, "paper has no PDF")

    body = request.get_json(silent=True) or {}
    payload: dict = {"paper_id": paper_id}
    if "steps" in body:
        payload["steps"] = body["steps"]
    if "force_steps" in body:
        payload["force_steps"] = body["force_steps"]
    if "translate_force" in body:
        payload["translate_force"] = body["translate_force"]
    if "on_error" in body:
        payload["on_error"] = body["on_error"]

    return _queue_paper_task("pipeline", paper_id, payload)
