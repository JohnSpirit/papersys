"""Glossary API — 热词库 CRUD。"""
from flask import Blueprint, abort, jsonify, request

from backend.db import glossary_repo

bp = Blueprint("glossary", __name__, url_prefix="/api")


@bp.get("/glossary")
def glossary_list():
    items = glossary_repo.list_glossary()
    return jsonify({"items": items, "count": len(items)})


@bp.post("/glossary")
def glossary_create():
    data = request.get_json(silent=True) or {}
    if not data.get("en") or not data.get("zh"):
        abort(400, "en and zh required")
    entry_id = glossary_repo.upsert_entry(
        en=data["en"].strip(),
        zh=data["zh"].strip(),
        context=data.get("context", "").strip() or None,
        notes=data.get("notes", "").strip() or None,
    )
    return jsonify({"id": entry_id}), 201


@bp.put("/glossary/<int:entry_id>")
def glossary_update(entry_id: int):
    data = request.get_json(silent=True) or {}
    if not data.get("en") or not data.get("zh"):
        abort(400, "en and zh required")
    glossary_repo.delete_entry(entry_id)
    new_id = glossary_repo.upsert_entry(
        en=data["en"].strip(),
        zh=data["zh"].strip(),
        context=data.get("context", "").strip() or None,
        notes=data.get("notes", "").strip() or None,
    )
    return jsonify({"id": new_id})


@bp.delete("/glossary/<int:entry_id>")
def glossary_delete(entry_id: int):
    glossary_repo.delete_entry(entry_id)
    return jsonify({"ok": True})