"""Test: is_real_pdf magic bytes 校验（防假 PDF 入库）。

拦截伪装成 PDF 的 HTML/文本文件（扩展名 .pdf 但文件头不是 %PDF-）。
"""
from pathlib import Path

from backend.pipeline.pdf_classify import is_real_pdf


def test_real_pdf_detected(tmp_path: Path):
    """真实 PDF 文件头以 %PDF- 开头。"""
    p = tmp_path / "real.pdf"
    p.write_bytes(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\nrest of content")
    assert is_real_pdf(str(p)) is True


def test_html_disguised_as_pdf_rejected(tmp_path: Path):
    """HTML 文件改 .pdf 扩展名应被拒绝。"""
    p = tmp_path / "fake.pdf"
    p.write_bytes(b"\n\n\n<html>\n<head>\n<title>fake</title>\n</head>\n</html>")
    assert is_real_pdf(str(p)) is False


def test_plain_text_rejected(tmp_path: Path):
    """纯文本文件应被拒绝。"""
    p = tmp_path / "notes.pdf"
    p.write_text("This is just plain text, not a PDF.", encoding="utf-8")
    assert is_real_pdf(str(p)) is False


def test_pdf_with_leading_whitespace_accepted(tmp_path: Path):
    """PDF 前置 BOM/空白仍应识别（部分 PDF 有前置字节）。"""
    p = tmp_path / "bom.pdf"
    p.write_bytes(b"\xef\xbb\xbf%PDF-1.5\ncontent")
    assert is_real_pdf(str(p)) is True


def test_empty_file_rejected(tmp_path: Path):
    """空文件应被拒绝。"""
    p = tmp_path / "empty.pdf"
    p.write_bytes(b"")
    assert is_real_pdf(str(p)) is False


def test_missing_file_rejected(tmp_path: Path):
    """不存在的文件路径返回 False（不抛异常）。"""
    assert is_real_pdf(str(tmp_path / "nope.pdf")) is False
