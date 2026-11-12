"""浏览器跨站写保护（独立于任何登录机制）。

无登录公开部署下，写接口没有身份可校验，剩余的网络安全边界是
「写请求必须来自本站页面」：浏览器对跨站写请求会携带
Sec-Fetch-Site: cross-site 或第三方 Origin 头，这里在 before_request
统一拒绝；同源写（Origin 的 host 与请求 Host 一致）与无 Origin 头的
非浏览器客户端（curl / CLI / MCP）放行。GET/HEAD/OPTIONS 不检查。

允许的来源 = config.toml [server].cors_origins 白名单（覆盖 Vite dev
等跨端口调试场景）+ 请求自身 Host（覆盖生产同源部署）。
"""
from urllib.parse import urlsplit

from flask import Flask, jsonify, request

from backend.config import CONFIG


def _deny(code: str, message: str):
    return jsonify({"error": message, "code": code}), 403


def check_browser_origin():
    """写请求来源检查；通过返回 None，拒绝返回 (response, 403)。"""
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return None
    if request.headers.get("Sec-Fetch-Site", "").lower() == "cross-site":
        return _deny("cross_site_request", "拒绝跨站写请求。")
    origin = request.headers.get("Origin")
    if not origin:
        return None
    if origin in set(CONFIG["server"].get("cors_origins") or []):
        return None
    if urlsplit(origin).netloc == request.host:
        return None
    return _deny("origin_denied", "请求来源不在允许列表中。")


def init_origin_guard(app: Flask) -> None:
    """注册跨站写保护 before_request（替代旧 init_auth 中的同名检查）。"""

    @app.before_request
    def _reject_cross_site_writes():
        return check_browser_origin()
