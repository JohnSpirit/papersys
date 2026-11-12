"""Test: Papers API 分页总数 + inbox 上传文件名消毒（3.1.0b4 修复回归）。"""
import pytest


@pytest.fixture
def client(temp_db):
    from backend.app import create_app
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


def _insert_papers(n: int, *, year_start: int = 2020) -> None:
    from backend.db.conn import get_conn
    conn = get_conn()
    try:
        for i in range(n):
            conn.execute(
                "INSERT INTO papers (dir_name, year, first_author, title)"
                " VALUES (?, ?, ?, ?)",
                (f"pg_test_{i}", year_start + i % 3, "auth", f"Paper {i}"),
            )
    finally:
        conn.close()


def test_papers_count_is_total_not_page_size(client):
    """count 必须是过滤后的总条数：limit 截断列表时它仍应等于全库/过滤总数。"""
    _insert_papers(7)
    r = client.get("/api/papers?limit=3")
    assert r.status_code == 200
    body = r.get_json()
    # 修复前：count == len(items) == 3，前端 totalPages 恒为 1，第 4 篇起无法翻页
    assert len(body["items"]) == 3
    assert body["count"] == 7


def test_papers_count_respects_filters(client):
    _insert_papers(7)  # 年份 2020/2021/2022 循环：2022 2 篇、2021 2 篇、2020 3 篇
    r = client.get("/api/papers?limit=2&year_from=2022")
    body = r.get_json()
    assert body["count"] == 2
    r2 = client.get("/api/papers?limit=2&year_to=2020")
    assert r2.get_json()["count"] == 3


def test_inbox_upload_sanitizes_traversal_filename(client, tmp_path, monkeypatch):
    """multipart 文件名含 ../ 时必须消毒后落 inbox（路径穿越防护）。"""
    from backend import config as cfg
    inbox = tmp_path / "inbox"
    monkeypatch.setitem(cfg.CONFIG["paths"], "inbox_dir", str(inbox))

    from pathlib import Path
    import io
    r = client.post(
        "/api/inbox/upload",
        data={"file": (io.BytesIO(b"not actually a pdf"), "../../evil.pdf")},
        content_type="multipart/form-data",
    )
    assert r.status_code == 200
    saved = Path(r.get_json()["path"])
    # 消毒后文件必须仍在 inbox 内（修复前：写到 inbox/../../evil.pdf）
    assert saved.parent.resolve() == inbox.resolve()
    assert saved.exists()
    # 投递默认走完整流水线，而非仅 ingest
    from backend.db import tasks_repo
    task = tasks_repo.get_task(r.get_json()["task_id"])
    assert task["kind"] == "pipeline"
    assert task["payload"]["steps"][0] == "ingest"
    # inbox 之外的任何位置都不应出现 evil.pdf（tmp_path 上一级）
    assert not (tmp_path.parent / "evil.pdf").exists()


def _insert_paper_with_doi(doi: str, *, dir_name: str) -> int:
    from backend.db.conn import get_conn
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO papers (dir_name, year, first_author, title, doi)"
            " VALUES (?, 2023, 'auth', 'Paper DOI', ?)",
            (dir_name, doi),
        )
        return cur.lastrowid
    finally:
        conn.close()


def test_paper_by_doi_found(client):
    """前缀 + 原样两种输入均命中；items 为完整论文 dict（同 paper_detail 形态）。"""
    pid = _insert_paper_with_doi("10.1234/example.2015.06", dir_name="pg_doi_ok")
    r = client.get("/api/papers/by_doi?doi=https://doi.org/10.1234/example.2015.06")
    assert r.status_code == 200
    body = r.get_json()
    assert body["count"] == 1
    assert body["items"][0]["id"] == pid
    assert body["items"][0]["doi"] == "10.1234/example.2015.06"

    r2 = client.get("/api/papers/by_doi?doi=10.1234/example.2015.06")
    assert r2.get_json()["count"] == 1


def test_paper_by_doi_missing_param(client):
    r = client.get("/api/papers/by_doi")
    assert r.status_code == 400


def test_paper_by_doi_not_found(client):
    """无命中返回 200 + count=0（与 /api/papers?q= 空结果语义一致）。"""
    r = client.get("/api/papers/by_doi?doi=10.9999/none")
    assert r.status_code == 200
    assert r.get_json()["count"] == 0
