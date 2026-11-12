"""Flask 应用入口。

3.11.2-open：无登录体系。Flask 直接托管 frontend/dist（静态资源 + SPA
深层路由回退），原根路由的 API 发现信息移到 /api；健康检查始终返回存储
状态。保留独立的浏览器跨站写保护（同源放行、跨站拒绝）与 ProxyFix。
"""
import os
from pathlib import Path

from flask import Flask, jsonify, send_from_directory
from flask_cors import CORS
from werkzeug.exceptions import HTTPException, NotFound
from werkzeug.middleware.proxy_fix import ProxyFix

from backend.config import CONFIG, PROJECT_ROOT, ensure_dirs, check_storage, validate_bind_security
from backend.db.conn import init_db
from backend.db.authz_schema import require_authz_columns
from backend.version import get_version
from backend.api.origin_guard import init_origin_guard
from backend.api.papers import bp as papers_bp
from backend.api.preview import bp as preview_bp
from backend.api.settings import bp as settings_bp
from backend.api.tasks import bp as tasks_bp
from backend.api.actions import bp as actions_bp
from backend.api.glossary_api import bp as glossary_bp
from backend.api.search import bp as search_bp
from backend.api.citations import bp as citations_bp
from backend.api.reader import bp as reader_bp
from backend.api.llm_usage import bp as llm_usage_bp

# 显式 import 触发 @register_worker 装饰器
from backend.workers import ingest_worker as _ingest  # noqa: F401
from backend.workers import parse_worker as _parse    # noqa: F401
from backend.workers import enrich_worker as _enrich  # noqa: F401
from backend.workers import translate_orchestrator as _translate  # noqa: F401
from backend.workers import summary_worker as _summary  # noqa: F401
from backend.workers import embed_worker as _embed  # noqa: F401
from backend.workers import citation_worker as _cite  # noqa: F401
from backend.workers import pipeline_worker as _pipeline  # noqa: F401
from backend.workers.runner import scheduler
from backend.db.tasks_repo import recover_orphans

_DEFAULT_CORS_ORIGINS = ["http://localhost:5173", "http://127.0.0.1:5173"]


def frontend_dist_dir() -> Path:
    """前端构建产物目录（vite build 输出）；PAPER_FRONTEND_DIST 可覆盖。"""
    override = os.environ.get("PAPER_FRONTEND_DIST")
    return Path(override) if override else PROJECT_ROOT / "frontend" / "dist"


def create_app() -> Flask:
    validate_bind_security()
    before = check_storage()
    ensure_dirs()
    init_db()
    # 业务表操作者列的旧库兼容检查：缺列时启动即失败，不等首次业务操作才报错
    require_authz_columns()

    if not before["library_exists"] or not before["inbox_exists"]:
        print(f"[startup] created missing storage dirs under: {before['library_root']}")

    app = Flask(__name__, static_folder=None)
    _origins = CONFIG["server"].get("cors_origins") or _DEFAULT_CORS_ORIGINS
    CORS(app, origins=_origins)
    if CONFIG["server"].get("proxy_fix", True):
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

    @app.errorhandler(HTTPException)
    def handle_http_error(e: HTTPException):
        return jsonify({"error": e.description, "code": e.code}), e.code

    @app.errorhandler(Exception)
    def handle_unhandled(e: Exception):
        return jsonify({"error": f"internal server error: {e}", "code": 500}), 500

    init_origin_guard(app)

    app.register_blueprint(papers_bp)
    app.register_blueprint(preview_bp)
    app.register_blueprint(settings_bp)
    app.register_blueprint(tasks_bp)
    app.register_blueprint(actions_bp)
    app.register_blueprint(glossary_bp)
    app.register_blueprint(search_bp)
    app.register_blueprint(citations_bp)
    app.register_blueprint(reader_bp)
    app.register_blueprint(llm_usage_bp)

    recovery = recover_orphans()
    if recovery["requeued"] or recovery["failed"]:
        print(f"[startup] task recovery: requeued={recovery['requeued']} "
              f"failed={recovery['failed']}")

    import os as _os
    if not app.debug or _os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        scheduler.start()

    @app.get("/api/health")
    def health():
        return jsonify({
            "ok": True,
            "service": "paper-survey-system",
            "version": get_version(),
            "storage": check_storage(),
        })

    @app.get("/api")
    def api_index():
        return jsonify({
            "service": "paper-survey-system backend",
            "version": get_version(),
            "endpoints": [
                "/api/health",
                "/api/stats",
                "/api/papers",
                "/api/papers/by_doi?doi=",
                "/api/papers/<id>",
                "/api/papers/<id>/pdf?page=N",
                "/api/papers/<id>/preview?type=en|zh|summary",
                "/api/papers/<id>/doc/<doc_type>",
                "/api/papers/<id>/revisions",
                "/api/revisions/<id>",
                "/api/revisions/<id>/restore",
                "/api/papers/<id>/annotations",
                "/api/annotations/<id>",
                "/api/semantic_search",
                "/api/papers/<id>/reembed",
                "/api/papers/<id>/images/<name>",
                "/api/papers/<id>/translations",
                "/api/papers/<id>/reparse",
                "/api/papers/<id>/reenrich",
                "/api/papers/<id>/bibtex",
                "/api/papers/<id>/retranslate",
                "/api/papers/<id>/resummarize",
                "/api/papers/<id>/pipeline",
                "/api/citations",
                "/api/citations/rebuild",
                "/api/inbox",
                "/api/inbox/scan",
                "/api/inbox/upload",
                "/api/inbox/from_url",
                "/api/search",
                "/api/tasks",
                "/api/tasks/<id>",
                "/api/tasks/<id>/retry",
                "/api/events  (SSE)",
                "/api/glossary",
                "/api/settings",
                "/api/llm_usage",
            ],
        })

    def _send_spa_index():
        dist = frontend_dist_dir()
        try:
            return send_from_directory(dist, "index.html")
        except NotFound:
            return jsonify({
                "service": "paper-survey-system backend",
                "version": get_version(),
                "hint": "frontend dist not built; API discovery at /api",
            }), 404

    @app.get("/")
    def index():
        return _send_spa_index()

    @app.get("/<path:filename>")
    def frontend(filename: str):
        """托管 frontend/dist：静态资源直发，其余路径回退 index.html（SPA）。

        /api 下的未知路径保持 JSON 404，不回退到前端页面。
        """
        if filename == "api" or filename.startswith("api/"):
            return jsonify({"error": "unknown api endpoint", "code": 404}), 404
        dist = frontend_dist_dir()
        max_age = 31536000 if filename.startswith("assets/") else None
        try:
            return send_from_directory(dist, filename, max_age=max_age)
        except NotFound:
            pass
        return _send_spa_index()

    return app


app = create_app()


if __name__ == "__main__":
    import os as _os2
    cfg = CONFIG["server"]
    host = _os2.environ.get("PAPER_HOST", cfg["host"])
    port = int(_os2.environ.get("PAPER_PORT", cfg["port"]))
    debug = _os2.environ.get("PAPER_DEBUG", str(cfg.get("debug", False))).lower() in ("1", "true")
    app.run(host=host, port=port, debug=debug, threaded=True, use_reloader=False)
