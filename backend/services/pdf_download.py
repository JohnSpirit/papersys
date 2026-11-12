"""URL -> inbox 的 PDF 下载暂存服务（Flask /api/inbox/from_url 与 MCP ingest_by_url 共用）。

安全约束（两个入口此前各自实现且 Flask 侧缺失，3.1.0b4 起统一在此）：
  * 协议白名单 http/https；拒绝 file:// 等
  * 目标地址解析后拒绝私网/回环/链路本地地址（SSRF 防护；服务只绑 127.0.0.1，
    但 CORS/浏览器跨站仍可触达本服务，故入口必须自防）；唯一例外：fake-IP
    DNS 代理网段 198.18.0.0/15 放行（见 _FAKEIP_NET 注释）
  * 重定向逐跳重新校验（防公网 URL 302 跳内网绕过首跳校验）
  * 下载大小上限（Content-Length 预检 + 流式累计），防磁盘写满
  * 落盘后 magic bytes 校验（is_real_pdf），与 MCP 入口行为对齐

已知局限（与实现取舍）：校验时解析一次 DNS、requests 连接时再解析一次，
理论上存在 DNS rebinding 窗口；本系统为单用户本机部署，不做 IP pinning。
"""
import ipaddress
import re
import secrets
import socket
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import requests

from backend.pipeline.pdf_classify import is_real_pdf
from backend.version import get_version


ALLOWED_SCHEMES = ("http", "https")
MAX_PDF_BYTES = 200 * 1024 * 1024  # 单文件 200MB（学术论文 PDF 远小于此）
MAX_REDIRECTS = 5
# fake-IP DNS（Clash/mihomo 等）把所有域名解析进 RFC 2544 基准网段 198.18.0.0/15，
# Python ipaddress 判其为 is_private；此时实际连接目标是本地代理而非解析出的 IP，
# 拦截不提供 SSRF 防护、只会拒绝一切下载，故对该网段精确放行（其余私网检查保留）
_FAKEIP_NET = ipaddress.ip_network("198.18.0.0/15")
_UA = {
    "User-Agent": f"paper-survey/{get_version()}",
    "Accept": "application/pdf,application/octet-stream;q=0.9,*/*;q=0.8",
}


class UrlValidationError(ValueError):
    """URL 前置校验失败（协议/主机/地址不可达公网）。调用方应按 4xx 处理。"""


class DownloadError(ValueError):
    """下载过程或落盘校验失败（网络/超限/非 PDF）。调用方应按 5xx 处理。"""


def _validate_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme.lower() not in ALLOWED_SCHEMES:
        raise UrlValidationError(
            f"scheme {parts.scheme!r} not allowed (only http/https)"
        )
    host = parts.hostname
    if not host:
        raise UrlValidationError("URL has no hostname")
    try:
        infos = socket.getaddrinfo(
            host,
            parts.port or (443 if parts.scheme.lower() == "https" else 80),
            proto=socket.IPPROTO_TCP,
        )
    except OSError as e:
        raise UrlValidationError(f"cannot resolve host {host!r}: {e}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip in _FAKEIP_NET:
            continue  # fake-IP DNS 代理网段，见 _FAKEIP_NET 注释
        if (
            ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified
        ):
            raise UrlValidationError(
                f"host {host!r} resolves to non-public address {ip}"
            )
    return url


def stage_pdf_from_url(
    url: str, inbox: Path, filename: str = "", *, timeout: int = 60
) -> tuple[Path, int]:
    """下载 url 指向的 PDF 到 inbox（文件名消毒 + 重名加随机后缀），返回 (路径, 大小)。

    失败时抛 UrlValidationError / DownloadError，并清理已写入的部分文件。
    """
    url = (url or "").strip()
    if not url:
        raise UrlValidationError("url required")

    if not filename:
        m = re.search(r"/([^/?#]+\.pdf)(?:$|[?#])", url, re.IGNORECASE)
        filename = m.group(1) if m else "download.pdf"
    if not filename.lower().endswith(".pdf"):
        filename += ".pdf"
    filename = re.sub(r"[^\w\-_. ]", "_", filename)

    target = inbox / filename
    if target.exists():
        target = inbox / f"{target.stem}_{secrets.token_hex(4)}.pdf"

    current = _validate_url(url)
    try:
        for _hop in range(MAX_REDIRECTS + 1):
            with requests.get(
                current, timeout=timeout, stream=True,
                headers=_UA, allow_redirects=False,
            ) as r:
                if r.status_code in (301, 302, 303, 307, 308):
                    loc = r.headers.get("Location")
                    if not loc:
                        raise DownloadError(f"redirect without Location from {current}")
                    current = _validate_url(urljoin(current, loc))
                    continue
                r.raise_for_status()
                cl = r.headers.get("Content-Length")
                if cl and cl.isdigit() and int(cl) > MAX_PDF_BYTES:
                    raise DownloadError(
                        f"Content-Length {cl} exceeds limit {MAX_PDF_BYTES} bytes"
                    )
                size = 0
                with open(target, "wb") as f:
                    for chunk in r.iter_content(8192):
                        if not chunk:
                            continue
                        size += len(chunk)
                        if size > MAX_PDF_BYTES:
                            raise DownloadError(
                                f"download exceeds limit {MAX_PDF_BYTES} bytes"
                            )
                        f.write(chunk)
                break
        else:
            raise DownloadError(f"too many redirects (> {MAX_REDIRECTS})")
    except (UrlValidationError, DownloadError):
        target.unlink(missing_ok=True)
        raise
    except requests.exceptions.RequestException as e:
        target.unlink(missing_ok=True)
        raise DownloadError(f"download failed: {e}")

    size = target.stat().st_size
    if size < 1000:
        target.unlink(missing_ok=True)
        raise DownloadError(
            f"downloaded file too small ({size} bytes); likely not a PDF"
        )
    if not is_real_pdf(str(target)):
        target.unlink(missing_ok=True)
        raise DownloadError("downloaded file is not a real PDF (bad magic bytes)")
    return target, size
