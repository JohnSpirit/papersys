from pathlib import Path

import pytest

from backend.workers import parse_worker


class _Context:
    def __init__(self):
        self.errors = []

    def info(self, message):
        pass

    def warn(self, message):
        pass

    def error(self, message):
        self.errors.append(message)

    def progress(self, value):
        pass


def _make_paper(tmp_path, temp_db, monkeypatch):
    from backend.config import CONFIG, library_root
    from backend.db.conn import get_conn
    from backend.db.repo import upsert_paper

    # 公开候选目录没有 .env；MinerUClient 构造要求 cloud 模式 token，
    # 这里注入假 token 保证测试不依赖环境（parse_pdf 均被 mock，无真实调用）。
    monkeypatch.setitem(CONFIG["secrets"], "mineru_token", "test-token")
    pdf_path = library_root() / "2013" / "test_paper" / "test.pdf"
    pdf_path.parent.mkdir(parents=True, exist_ok=True)
    pdf_path.write_bytes(b"%PDF-1.7 test")
    conn = get_conn()
    try:
        paper_id, _ = upsert_paper(conn, {
            "dir_name": "test_paper",
            "year": 2013,
            "first_author": "test",
            "title": "Test Paper",
            "pdf_path": "2013/test_paper/test.pdf",
            "status_pdf": "ok",
        })
    finally:
        conn.close()
    monkeypatch.setattr(parse_worker, "is_real_pdf", lambda path: True)
    monkeypatch.setattr(parse_worker, "pdfinfo", lambda path: {"pdf_title": "Test Paper"})
    monkeypatch.setattr(parse_worker, "classify_pdf", lambda info: "vector")
    return paper_id


def _status(paper_id):
    from backend.db.conn import get_conn

    conn = get_conn()
    try:
        return conn.execute(
            "SELECT status_parsed FROM papers WHERE id = ?", (paper_id,)
        ).fetchone()[0]
    finally:
        conn.close()


def test_parse_worker_sets_error_when_mineru_fails(tmp_path, temp_db, monkeypatch):
    paper_id = _make_paper(tmp_path, temp_db, monkeypatch)

    def fail(*args, **kwargs):
        raise RuntimeError("Device string must not be empty")

    monkeypatch.setattr(parse_worker.MinerUClient, "parse_pdf", fail)
    with pytest.raises(RuntimeError, match="Device string"):
        parse_worker.parse_worker(_Context(), {"paper_id": paper_id})
    assert _status(paper_id) == "error"


def test_parse_worker_sets_error_when_result_read_fails(tmp_path, temp_db, monkeypatch):
    paper_id = _make_paper(tmp_path, temp_db, monkeypatch)
    missing = tmp_path / "missing" / "full.md"
    monkeypatch.setattr(
        parse_worker.MinerUClient,
        "parse_pdf",
        lambda *args, **kwargs: {"full_md": missing, "model": "hybrid-engine"},
    )
    with pytest.raises(FileNotFoundError):
        parse_worker.parse_worker(_Context(), {"paper_id": paper_id})
    assert _status(paper_id) == "error"


def test_parse_worker_success_sets_ok(tmp_path, temp_db, monkeypatch):
    paper_id = _make_paper(tmp_path, temp_db, monkeypatch)

    def succeed(client, pdf_path, output_dir, model, is_ocr):
        output_dir.mkdir(parents=True, exist_ok=True)
        full_md = output_dir / "full.md"
        full_md.write_text("# Test Paper\n\nBody.", encoding="utf-8")
        return {"full_md": full_md, "model": "hybrid-engine"}

    monkeypatch.setattr(parse_worker.MinerUClient, "parse_pdf", succeed)
    parse_worker.parse_worker(_Context(), {"paper_id": paper_id})
    assert _status(paper_id) == "ok"


def test_parse_worker_preserves_original_error_when_status_update_fails(
    tmp_path, temp_db, monkeypatch
):
    paper_id = _make_paper(tmp_path, temp_db, monkeypatch)
    original_set_status = parse_worker._set_status

    def fail_status_update(paper_id, status):
        if status == "error":
            raise RuntimeError("status database unavailable")
        original_set_status(paper_id, status)

    def fail_parse(*args, **kwargs):
        raise RuntimeError("original MinerU error")

    monkeypatch.setattr(parse_worker, "_set_status", fail_status_update)
    monkeypatch.setattr(parse_worker.MinerUClient, "parse_pdf", fail_parse)
    context = _Context()
    with pytest.raises(RuntimeError, match="original MinerU error"):
        parse_worker.parse_worker(context, {"paper_id": paper_id})
    assert context.errors
    assert "status database unavailable" in context.errors[0]
