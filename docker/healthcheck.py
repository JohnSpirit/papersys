"""容器健康检查：请求 /api/health，期望 HTTP 200 且 JSON ok=true。

地址解析：PAPER_HEALTH_URL 优先；否则按 PAPER_BIND / PAPER_PORT 推导，
通配监听地址（0.0.0.0 / ::）映射到 127.0.0.1。
"""
import json
import os
import sys
import urllib.request


def health_url() -> str:
    explicit = os.environ.get("PAPER_HEALTH_URL")
    if explicit:
        return explicit
    bind = os.environ.get("PAPER_BIND", "")
    port = os.environ.get("PAPER_PORT", "")
    if not port:
        port = bind.rsplit(":", 1)[1] if ":" in bind else "5050"
    host = bind.rsplit(":", 1)[0] if bind.count(":") == 1 else bind
    if host.strip("[]") in ("", "0.0.0.0", "::"):
        host = "127.0.0.1"
    return f"http://{host}:{port}/api/health"


def main() -> int:
    try:
        with urllib.request.urlopen(health_url(), timeout=5) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            ok = resp.status == 200 and bool(body.get("ok"))
    except Exception as exc:
        print(f"unhealthy: {exc}", file=sys.stderr)
        return 1
    if not ok:
        print("unhealthy: /api/health returned ok=false", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
