"""设置接口：读取不含 secret 内容的配置状态。"""
from flask import Blueprint, jsonify
from backend.config import CONFIG, retrieval_api_key

bp = Blueprint("settings", __name__, url_prefix="/api")


@bp.get("/settings")
def settings_get():
    secrets_status = {key: bool(value) for key, value in CONFIG["secrets"].items()}
    secrets_status["retrieval_embed_available"] = bool(retrieval_api_key("embed"))
    secrets_status["retrieval_rerank_available"] = bool(retrieval_api_key("rerank"))
    return jsonify({
        "paths":      CONFIG["paths"],
        "server":     CONFIG["server"],
        "workers":    CONFIG["workers"],
        "external":   CONFIG["external"],
        "mineru":     CONFIG["mineru"],
        "llm":        CONFIG["llm"],
        "retrieval":  CONFIG.get("retrieval", {}),
        "secrets":    {"present": secrets_status},
    })
