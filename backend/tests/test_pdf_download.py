"""Test: services/pdf_download 的 URL 前置校验与下载约束（3.1.0b4 修复回归）。

覆盖：协议白名单 / 内网地址拦截 / 重定向逐跳校验 / 大小上限 / 魔数校验 / 正常路径。
"""
import pytest

from backend.services import pdf_download as pd
from backend.services.pdf_download import (
    DownloadError,
    UrlValidationError,
    stage_pdf_from_url,
)


# ── URL 前置校验（不触网）─────────────────────────────────


def test_validate_rejects_non_http_scheme():
    with pytest.raises(UrlValidationError):
        pd._validate_url("ftp://example.com/a.pdf")
    with pytest.raises(UrlValidationError):
        pd._validate_url("file:///etc/passwd")


def test_validate_rejects_private_addresses():
    # IP 字面量不走 DNS，用例无网络依赖
    for url in (
        "http://127.0.0.1/x.pdf",
        "http://10.0.0.1/x.pdf",
        "http://192.168.1.1/x.pdf",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]/x.pdf",
    ):
        with pytest.raises(UrlValidationError):
            pd._validate_url(url)


def test_validate_accepts_public_ip():
    assert pd._validate_url("https://8.8.8.8/paper.pdf") == "https://8.8.8.8/paper.pdf"


def test_validate_accepts_fakeip_dns_range():
    """fake-IP DNS 代理（Clash/mihomo）把域名解析进 198.18.0.0/15：放行。

    IP 字面量与域名解析结果走同一校验循环，用字面量覆盖该分支，无网络依赖。
    该网段两端地址都判 is_private=True，不豁免则本机所有 URL 入库被拒（已修）。
    """
    for url in ("https://198.18.0.1/paper.pdf", "https://198.19.255.254/paper.pdf"):
        assert pd._validate_url(url) == url


def test_validate_still_rejects_private_outside_fakeip():
    # 豁免只覆盖 198.18.0.0/15，其余 RFC 1918 私网仍拦截
    for url in ("http://172.16.0.1/x.pdf", "http://192.0.2.1/x.pdf"):
        with pytest.raises(UrlValidationError):
            pd._validate_url(url)


# ── 下载流程（fake requests.get，不触网）─────────────────


class _Resp:
    def __init__(self, status_code=200, headers=None, chunks=()):
        self.status_code = status_code
        self.headers = headers or {}
        self._chunks = list(chunks)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.exceptions.HTTPError(f"{self.status_code}")

    def iter_content(self, _size):
        yield from self._chunks


def _pdf_bytes(n_pages_fill: int = 8) -> bytes:
    return b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n" + b"x" * n_pages_fill * 512


def test_stage_happy_path(tmp_path, monkeypatch):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    monkeypatch.setattr(pd.requests, "get", lambda *a, **kw: _Resp(chunks=[_pdf_bytes()]))
    target, size = stage_pdf_from_url("https://8.8.8.8/paper.pdf", inbox)
    assert target.parent == inbox
    assert target.name == "paper.pdf"
    assert size == target.stat().st_size
    assert size >= 1000


def test_stage_sanitizes_filename(tmp_path, monkeypatch):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    monkeypatch.setattr(pd.requests, "get", lambda *a, **kw: _Resp(chunks=[_pdf_bytes()]))
    target, _ = stage_pdf_from_url(
        "https://8.8.8.8/paper.pdf", inbox, filename="../../evil.pdf"
    )
    # 穿越片段被消毒为文件名字符，目标仍落在 inbox 内
    assert target.parent == inbox
    assert ".." not in target.name.replace(".._", "")


def test_stage_rejects_redirect_to_private(tmp_path, monkeypatch):
    inbox = tmp_path / "inbox"
    inbox.mkdir()

    def _redirect(*a, **kw):
        return _Resp(status_code=302, headers={"Location": "http://127.0.0.1/steal.pdf"})

    monkeypatch.setattr(pd.requests, "get", _redirect)
    with pytest.raises(UrlValidationError):
        stage_pdf_from_url("https://8.8.8.8/paper.pdf", inbox)
    assert list(inbox.glob("*.pdf")) == []


def test_stage_rejects_oversize_content_length(tmp_path, monkeypatch):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    monkeypatch.setattr(pd, "MAX_PDF_BYTES", 1000)
    resp = _Resp(headers={"Content-Length": "99999999"}, chunks=[b"x"])
    monkeypatch.setattr(pd.requests, "get", lambda *a, **kw: resp)
    with pytest.raises(DownloadError):
        stage_pdf_from_url("https://8.8.8.8/big.pdf", inbox)
    assert list(inbox.glob("*.pdf")) == []


def test_stage_rejects_oversize_stream(tmp_path, monkeypatch):
    """无 Content-Length 时流式累计仍要拦住（磁盘写满防护）。"""
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    monkeypatch.setattr(pd, "MAX_PDF_BYTES", 1000)
    resp = _Resp(chunks=[b"%PDF-1.7\n" + b"y" * 900, b"z" * 900])
    monkeypatch.setattr(pd.requests, "get", lambda *a, **kw: resp)
    with pytest.raises(DownloadError):
        stage_pdf_from_url("https://8.8.8.8/big.pdf", inbox)
    assert list(inbox.glob("*.pdf")) == []


def test_stage_rejects_fake_pdf_bytes(tmp_path, monkeypatch):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    html = b"<html>not a pdf</html>" * 200  # >1000 字节且非 %PDF- 头
    monkeypatch.setattr(pd.requests, "get", lambda *a, **kw: _Resp(chunks=[html]))
    with pytest.raises(DownloadError, match="not a real PDF"):
        stage_pdf_from_url("https://8.8.8.8/paper.pdf", inbox)
    assert list(inbox.glob("*.pdf")) == []


def test_stage_rejects_tiny_file(tmp_path, monkeypatch):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    monkeypatch.setattr(
        pd.requests, "get", lambda *a, **kw: _Resp(chunks=[b"%PDF-1.7 tiny"])
    )
    with pytest.raises(DownloadError, match="too small"):
        stage_pdf_from_url("https://8.8.8.8/paper.pdf", inbox)
    assert list(inbox.glob("*.pdf")) == []
