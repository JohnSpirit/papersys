"""Test: 本地 MinerU /file_parse 非 200 时错误信息保留。

旧实现 raise_for_status() 丢弃响应体，导致 "Unsupported file type: html"
这类关键诊断信息丢失。新实现应在异常消息中包含响应体。
"""
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


@pytest.fixture(autouse=True)
def _isolate_mineru_lock(tmp_path, monkeypatch):
    from backend.services import mineru_health

    monkeypatch.setitem(mineru_health.CONFIG["paths"], "cache_dir", str(tmp_path))


def test_local_parse_error_includes_body(tmp_path: Path):
    """本地 MinerU 返回 400 时，异常消息应包含响应体文本。"""
    from backend.external.mineru import MinerUClient

    fake_response = MagicMock()
    fake_response.status_code = 400
    fake_response.text = '{"detail":"Unsupported file type: html"}'
    fake_response.json.side_effect = ValueError("not json-able in error path")

    fake_pdf = tmp_path / "fake.pdf"
    fake_pdf.write_bytes(b"%PDF-1.7 fake")

    with patch.object(MinerUClient, "__init__", lambda self: None):
        c = MinerUClient()
        c._mode = "local"
        c._session = MagicMock()
        c._session.post.return_value = fake_response
        # CONFIG 访问
        with patch("backend.external.mineru.CONFIG", {
            "mineru": {"local_backend": "hybrid-engine", "local_effort": "medium",
                       "local_api_base": "http://127.0.0.1:8000"},
            "paths": {"cache_dir": str(tmp_path)},
        }):
            with pytest.raises(RuntimeError) as exc_info:
                c.parse_pdf_local(str(fake_pdf), tmp_path / "out")
            msg = str(exc_info.value)
            assert "400" in msg
            assert "Unsupported file type" in msg


def test_local_parse_success_passthrough(tmp_path: Path):
    """正常 200 响应应正常解析返回结构。"""
    from backend.external.mineru import MinerUClient

    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.return_value = {
        "status": "completed",
        "results": {"file1": {"md_content": "# Title\n\nbody", "images": {}}},
    }

    fake_pdf = tmp_path / "real.pdf"
    fake_pdf.write_bytes(b"%PDF-1.7 real content")

    out_dir = tmp_path / "out"

    with patch.object(MinerUClient, "__init__", lambda self: None):
        c = MinerUClient()
        c._mode = "local"
        c._session = MagicMock()
        c._session.post.return_value = fake_response
        with patch("backend.external.mineru.CONFIG", {
            "mineru": {"local_backend": "hybrid-engine", "local_effort": "medium",
                       "local_api_base": "http://127.0.0.1:8000"},
            "paths": {"cache_dir": str(tmp_path)},
        }):
            result = c.parse_pdf_local(str(fake_pdf), out_dir)
            assert result["chars"] == len("# Title\n\nbody")
            assert (out_dir / "full.md").read_text(encoding="utf-8") == "# Title\n\nbody"
            assert result["model"] == "hybrid-engine"
            assert (tmp_path / "mineru-local.lock").exists()


def test_local_parse_gpu_error_includes_recovery_command(tmp_path: Path):
    from backend.external.mineru import MinerUClient

    fake_response = MagicMock()
    fake_response.status_code = 409
    fake_response.text = '{"status":"failed","error":"Device string must not be empty"}'
    fake_response.json.return_value = {
        "status": "failed",
        "error": "Device string must not be empty",
    }
    fake_pdf = tmp_path / "gpu.pdf"
    fake_pdf.write_bytes(b"%PDF-1.7 gpu")

    with patch.object(MinerUClient, "__init__", lambda self: None):
        client = MinerUClient()
        client._session = MagicMock()
        client._session.post.return_value = fake_response
        with patch("backend.external.mineru.CONFIG", {
            "mineru": {
                "local_backend": "hybrid-engine",
                "local_effort": "medium",
                "local_api_base": "http://127.0.0.1:8000",
                "local_container": "mineru-api-gpu",
            },
            "paths": {"cache_dir": str(tmp_path)},
        }):
            with pytest.raises(RuntimeError) as exc_info:
                client.parse_pdf_local(str(fake_pdf), tmp_path / "out")
    message = str(exc_info.value)
    assert "Device string must not be empty" in message
    assert "mineru-api-gpu" in message
    assert "docker restart mineru-api-gpu" in message
