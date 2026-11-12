"""Reader API - 自建公示层（接替 Outline 双向流）。

GET  /api/papers/<id>/doc/<doc_type>          读全文（en|zh|summary）
PUT  /api/papers/<id>/doc/<doc_type>          保存编辑（写 chunk_index=-1 覆盖层 + 修订史）
GET  /api/papers/<id>/revisions               修订史列表（可按 doc_type 过滤）
GET  /api/revisions/<rev_id>                  单条修订（含内容）
POST /api/revisions/<rev_id>/restore          回滚该版本（写覆盖层 + 新修订记录）

批注：
GET  /api/papers/<id>/annotations
POST /api/papers/<id>/annotations             {type, anchor, quote, note}
DELETE /api/annotations/<id>
"""
import hashlib
from flask import Blueprint, abort, jsonify, request

from backend.db.conn import conn_ctx, get_conn
from backend.db.repo import get_paper
from backend.db import doc_revisions_repo, tasks_repo
from backend.services.doc_text import read_doc_text

bp = Blueprint("reader", __name__, url_prefix="/api")

VALID_DOC_TYPES = ("en", "zh", "summary")
VALID_ANNOTATION_TYPES = ("highlight", "callout", "comment")


def _doc_text(paper: dict, doc_type: str) -> tuple[str, str]:
    """读文档全文。返回 (text, source)。规则实现见 backend/services/doc_text.py。"""
    return read_doc_text(paper, doc_type)


def _save_doc(conn, paper_id: int, doc_type: str, text: str, source: str) -> None:
    """保存文档内容（覆盖层语义）。在调用方事务内执行（不自开连接）。

    en/zh -> translations.chunk_index=-1 行，ON CONFLICT 只更新本语言列；
    summary -> summaries 新 version。
    """
    if doc_type in ("en", "zh"):
        col = "en_text" if doc_type == "en" else "zh_text"
        other = "zh_text" if doc_type == "en" else "en_text"
        conn.execute(
            f"""INSERT INTO translations (paper_id, chunk_index, {col}, {other}, status, source)
                VALUES (?, -1, ?, NULL, 'ok', ?)
                ON CONFLICT(paper_id, chunk_index) DO UPDATE SET
                  {col} = excluded.{col},
                  source = ?,
                  updated_at = CURRENT_TIMESTAMP""",
            (paper_id, text, source, source),
        )
    else:
        cur = conn.execute(
            "SELECT IFNULL(MAX(version), 0) FROM summaries WHERE paper_id = ?",
            (paper_id,),
        )
        next_v = (cur.fetchone()[0] or 0) + 1
        conn.execute(
            "INSERT INTO summaries (paper_id, content_md, version, source) VALUES (?, ?, ?, ?)",
            (paper_id, text, next_v, source),
        )


def _get_paper_or_404(paper_id: int) -> dict:
    conn = get_conn()
    try:
        p = get_paper(conn, paper_id)
    finally:
        conn.close()
    if not p:
        abort(404, "paper not found")
    return p


def _enqueue_reembed(conn, paper_id: int) -> int | None:
    """EN 复核版变更后自动重嵌：status_embedded 置 missing（向量已过期）并入队 force embed。

    已有 pending/running 的 embed 任务时跳过入队（连续多次保存只触发一次）。
    查重与入队在调用方事务内执行，并发保存由 SQLite 写锁串行化，
    第二个事务的查重能读到第一个事务已提交的任务，不会产生重复任务。
    返回新任务 id；被合并跳过时返回 None。
    """
    conn.execute(
        "UPDATE papers SET status_embedded = 'missing', updated_at = CURRENT_TIMESTAMP"
        " WHERE id = ?",
        (paper_id,),
    )
    dup = conn.execute(
        "SELECT id FROM tasks WHERE kind = 'embed' AND paper_id = ?"
        " AND status IN ('pending', 'running')",
        (paper_id,),
    ).fetchone()
    if dup:
        return None
    return tasks_repo.create_task(
        kind="embed", payload={"paper_id": paper_id, "force": True}, paper_id=paper_id,
        conn=conn,
    )


@bp.get("/papers/<int:paper_id>/doc/<doc_type>")
def paper_doc(paper_id: int, doc_type: str):
    if doc_type not in VALID_DOC_TYPES:
        abort(400, f"doc_type must be one of {VALID_DOC_TYPES}")
    paper = _get_paper_or_404(paper_id)
    text, source = _doc_text(paper, doc_type)
    return jsonify({
        "doc_type": doc_type,
        "available": bool(text),
        "text": text,
        "source": source,
        "size": len(text),
        "hash": hashlib.md5(text.encode("utf-8")).hexdigest() if text else None,
    })


@bp.put("/papers/<int:paper_id>/doc/<doc_type>")
def paper_doc_save(paper_id: int, doc_type: str):
    """保存阅读器编辑。body: {"text": "...", "base_hash": "可选的读取时摘要"}（全量替换）。

    - 写 chunk_index=-1 覆盖层（en/zh 各自动列，互不覆盖）
    - 携带 base_hash 且服务器内容已变化时返回 409，不写入覆盖层
    - 若内容相对当前有变化，记 doc_revisions(source='web_edit')
    - EN 变更且此前已嵌向量（ok/error）时：status_embedded 置 missing 并自动入队 force re-embed
    - 返回是否需要提示重翻译（EN 变更时）
    """
    if doc_type not in VALID_DOC_TYPES:
        abort(400, f"doc_type must be one of {VALID_DOC_TYPES}")
    paper = _get_paper_or_404(paper_id)

    body = request.get_json(silent=True) or {}
    text = body.get("text")
    if not isinstance(text, str):
        abort(400, "text (string) required")
    if len(text.encode("utf-8")) > 20 * 1024 * 1024:
        abort(413, "text too large (>20MB)")
    base_hash = body.get("base_hash")
    if "base_hash" in body and base_hash is not None and not isinstance(base_hash, str):
        abort(400, "base_hash must be a string or null")

    new_hash = hashlib.md5(text.encode("utf-8")).hexdigest()
    revision_id = None
    reembed_task_id = None
    # 摘要检查、覆盖层、修订史与 re-embed 入队在同一写事务内完成。
    with conn_ctx(immediate=True) as conn:
        old_text, _old_source = _doc_text(paper, doc_type)
        old_hash = hashlib.md5(old_text.encode("utf-8")).hexdigest() if old_text else None
        if "base_hash" in body and base_hash != old_hash:
            return jsonify({
                "error": "document changed since it was loaded",
                "code": 409,
                "current_hash": old_hash,
            }), 409
        _save_doc(conn, paper_id, doc_type, text, "web_edit")
        if new_hash != old_hash:
            revision_id = doc_revisions_repo.record_revision(
                paper_id, doc_type, text, "web_edit", conn=conn,
            )
        if doc_type == "en" and new_hash != old_hash and paper.get("status_embedded") in ("ok", "error"):
            reembed_task_id = _enqueue_reembed(conn, paper_id)

    return jsonify({
        "saved": True,
        "revision_id": revision_id,
        "changed": new_hash != old_hash,
        "hash": new_hash,
        "suggest_retranslate": doc_type == "en" and new_hash != old_hash,
        "reembed_task_id": reembed_task_id,
    })


@bp.get("/papers/<int:paper_id>/revisions")
def paper_revisions(paper_id: int):
    _get_paper_or_404(paper_id)
    doc_type = request.args.get("doc_type") or None
    if doc_type and doc_type not in VALID_DOC_TYPES:
        abort(400, f"doc_type must be one of {VALID_DOC_TYPES}")
    items = doc_revisions_repo.list_revisions(paper_id, doc_type=doc_type)
    return jsonify({"items": items, "count": len(items)})


@bp.get("/revisions/<int:revision_id>")
def revision_detail(revision_id: int):
    rev = doc_revisions_repo.get_revision(revision_id)
    if not rev:
        abort(404, "revision not found")
    content = doc_revisions_repo.read_revision_payload(revision_id)
    return jsonify({**rev, "content": content})


@bp.post("/revisions/<int:revision_id>/restore")
def revision_restore(revision_id: int):
    """回滚到指定修订：读快照内容 -> 写覆盖层 -> 记新修订(source='web_edit')。

    EN 回滚且内容确有变化时与 PUT 一样自动入队 force re-embed。
    """
    rev = doc_revisions_repo.get_revision(revision_id)
    if not rev:
        abort(404, "revision not found")
    content = doc_revisions_repo.read_revision_payload(revision_id)
    if content is None:
        abort(410, "revision snapshot missing on disk")
    paper = _get_paper_or_404(rev["paper_id"])
    old_text, _ = _doc_text(paper, rev["doc_type"])
    with conn_ctx() as conn:
        _save_doc(conn, rev["paper_id"], rev["doc_type"], content, "web_edit")
        new_id = doc_revisions_repo.record_revision(
            rev["paper_id"], rev["doc_type"], content, "web_edit", conn=conn,
        )
        reembed_task_id = None
        if rev["doc_type"] == "en" and content != old_text and paper.get("status_embedded") in ("ok", "error"):
            reembed_task_id = _enqueue_reembed(conn, rev["paper_id"])
    return jsonify({
        "restored": True,
        "from_revision": revision_id,
        "new_revision_id": new_id,
        "reembed_task_id": reembed_task_id,
    })


# ── 批注 ────────────────────────────────────────────────


@bp.get("/papers/<int:paper_id>/annotations")
def paper_annotations(paper_id: int):
    _get_paper_or_404(paper_id)
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM annotations WHERE paper_id = ? ORDER BY id DESC",
            (paper_id,),
        ).fetchall()
    finally:
        conn.close()
    return jsonify({"items": [dict(r) for r in rows], "count": len(rows)})


@bp.post("/papers/<int:paper_id>/annotations")
def paper_annotation_create(paper_id: int):
    """body: {"type": "highlight|comment|callout", "anchor": "en|zh|summary",
              "quote": "划词文本", "note": "备注", "position": {...}}"""
    _get_paper_or_404(paper_id)
    body = request.get_json(silent=True) or {}
    typ = body.get("type")
    anchor = body.get("anchor")
    if typ not in VALID_ANNOTATION_TYPES:
        abort(400, f"type must be one of {VALID_ANNOTATION_TYPES}")
    if anchor and anchor not in VALID_DOC_TYPES:
        abort(400, f"anchor must be one of {VALID_DOC_TYPES}")
    quote = (body.get("quote") or "").strip()
    note = (body.get("note") or "").strip()
    if typ == "highlight" and not quote:
        abort(400, "highlight requires quote")

    import json as _json
    payload = _json.dumps({
        "quote": quote,
        "note": note,
        "position": body.get("position"),
    }, ensure_ascii=False)

    conn = get_conn()
    try:
        cur = conn.execute(
            """INSERT INTO annotations
               (paper_id, type, anchor, payload_json, source, created_by_user_id)
               VALUES (?, ?, ?, ?, 'web', NULL)""",
            (paper_id, typ, anchor, payload),
        )
        aid = cur.lastrowid
    finally:
        conn.close()
    return jsonify({"id": aid}), 201


@bp.delete("/annotations/<int:annotation_id>")
def annotation_delete(annotation_id: int):
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT id FROM annotations WHERE id=?", (annotation_id,)
        ).fetchone()
        if not row:
            abort(404, "annotation not found")
        conn.execute("DELETE FROM annotations WHERE id = ?", (annotation_id,))
    finally:
        conn.close()
    return jsonify({"deleted": True})
