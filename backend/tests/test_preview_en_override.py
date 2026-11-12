"""Test: /api/papers/<id>/preview?type=en 优先 EN override，返回 source 字段。"""
from pathlib import Path

import pytest

from backend.db.conn import get_conn


@pytest.fixture
def client(temp_db):
    """提供 Flask test_client；temp_db 已 monkeypatch DB_PATH。"""
    from backend.app import create_app
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


def _insert_paper(parsed_path: str | None = None, *, dir_name="pv2023_x") -> int:
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO papers (dir_name, year, first_author, title, parsed_path)"
            " VALUES (?, ?, ?, ?, ?)",
            (dir_name, 2023, "test", "T", parsed_path),
        )
        return cur.lastrowid
    finally:
        conn.close()


def _insert_override(paper_id: int, *, en_text: str = "", zh_text: str = "") -> None:
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO translations (paper_id, chunk_index, en_text, zh_text, status, source)"
            " VALUES (?, -1, ?, ?, 'ok', 'manual_edit')",
            (paper_id, en_text, zh_text),
        )
    finally:
        conn.close()


def _write_parsed(tmp_path: Path, body: str) -> Path:
    parsed = tmp_path / "parsed"
    parsed.mkdir()
    md = parsed / "full.md"
    md.write_text(body, encoding="utf-8")
    return md


def test_preview_en_returns_override_text(client, tmp_path):
    md = _write_parsed(tmp_path, "FROM_PARSED_PATH content.\n")
    pid = _insert_paper(parsed_path=str(md))
    _insert_override(pid, en_text="OVERRIDE_TEXT_MARKER from outline.")

    r = client.get(f"/api/papers/{pid}/preview?type=en")
    assert r.status_code == 200
    body = r.get_json()
    assert body["available"] is True
    assert body["source"] == "manual_edit"
    assert "OVERRIDE_TEXT_MARKER" in body["text"]
    assert "FROM_PARSED_PATH" not in body["text"]


def test_preview_en_source_parsed_when_no_override(client, tmp_path):
    md = _write_parsed(tmp_path, "ORIG_PARSED_CONTENT.\n")
    pid = _insert_paper(parsed_path=str(md), dir_name="pv2023_orig")

    r = client.get(f"/api/papers/{pid}/preview?type=en")
    assert r.status_code == 200
    body = r.get_json()
    assert body["available"] is True
    assert body["source"] == "parsed"
    assert "ORIG_PARSED_CONTENT" in body["text"]


def test_preview_en_offset_limit_slice(client, tmp_path):
    full = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    pid = _insert_paper(parsed_path=None, dir_name="pv2023_slice")
    _insert_override(pid, en_text=full)

    r = client.get(f"/api/papers/{pid}/preview?type=en&offset=3&limit=5")
    body = r.get_json()
    assert body["text"] == "DEFGH"
    assert body["total_size"] == 26
    assert body["has_more"] is True
    assert body["source"] == "manual_edit"


def test_preview_en_empty_override_falls_back(client, tmp_path):
    md = _write_parsed(tmp_path, "PARSED_BODY\n")
    pid = _insert_paper(parsed_path=str(md), dir_name="pv2023_emptyovr")
    _insert_override(pid, en_text="", zh_text="ZH-only override")

    r = client.get(f"/api/papers/{pid}/preview?type=en")
    body = r.get_json()
    assert body["source"] == "parsed"
    assert "PARSED_BODY" in body["text"]


def test_preview_zh_source_manual_edit(client):
    pid = _insert_paper(parsed_path=None, dir_name="pv2023_zh1")
    _insert_override(pid, zh_text="人工编辑后的中文版")

    r = client.get(f"/api/papers/{pid}/preview?type=zh")
    body = r.get_json()
    assert body["available"] is True
    assert body["source"] == "manual_edit"
    assert "人工编辑后的中文版" in body["text"]


def test_preview_zh_source_auto_when_no_override(client):
    pid = _insert_paper(parsed_path=None, dir_name="pv2023_zh2")
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO translations (paper_id, chunk_index, en_text, zh_text, status, source)"
            " VALUES (?, 0, 'en', '自动翻译', 'ok', 'auto')",
            (pid,),
        )
    finally:
        conn.close()

    r = client.get(f"/api/papers/{pid}/preview?type=zh")
    body = r.get_json()
    assert body["source"] == "auto"
    assert "自动翻译" in body["text"]


def test_images_traversal_blocked(client, tmp_path):
    """images 端点路径穿越防护：锚点限定在 images/ 子目录（回归）。"""
    md = _write_parsed(tmp_path, "SECRET_FULL_MD\n")
    pid = _insert_paper(parsed_path=str(md), dir_name="pv2023_trav")
    (tmp_path / "parsed" / "images").mkdir()
    (tmp_path / "parsed" / "images" / "fig.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    # 论文目录内的 full.md 也不可读
    assert client.get(f"/api/papers/{pid}/images/..%2Ffull.md").status_code in (403, 404)
    # 逃出论文目录
    assert client.get(f"/api/papers/{pid}/images/..%2F..%2Fsecret").status_code in (403, 404)
    # 正常图片可读
    assert client.get(f"/api/papers/{pid}/images/fig.png").status_code == 200
