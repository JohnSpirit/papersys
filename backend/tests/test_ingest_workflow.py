"""批量入库、去重、SI 关联与 doctor 检查。"""
import json
from pathlib import Path
from types import SimpleNamespace

import fitz

from backend.db.conn import get_conn


def _make_pdf(path: Path, text: str, *, title=None, authors=None) -> None:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_textbox(fitz.Rect(48, 48, 560, 760), text, fontsize=10)
    metadata = {}
    if title is not None:
        metadata["title"] = title
    if authors is not None:
        metadata["author"] = authors
    if metadata:
        doc.set_metadata(metadata)
    doc.save(str(path))
    doc.close()


def _insert_library_paper(path: Path, dir_name: str, doi=None, title="Paper") -> int:
    conn = get_conn()
    try:
        root = path.parent.parent.parent
        cur = conn.execute(
            "INSERT INTO papers (dir_name, year, first_author, title, doi, pdf_path,"
            " pdf_size_bytes) VALUES (?, 2024, 'test', ?, ?, ?, ?)",
            (dir_name, title, doi, str(path.relative_to(root)), path.stat().st_size),
        )
        return cur.lastrowid
    finally:
        conn.close()


def test_inbox_scan_dry_run_and_duplicate_skip(temp_db, tmp_path, monkeypatch, capsys):
    from backend import config as cfg
    from backend.cli import cmd_inbox_scan

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    monkeypatch.setitem(cfg.CONFIG["paths"], "inbox_dir", str(inbox))
    library = tmp_path / "library"
    existing_pdf = library / "2024" / "existing" / "existing.pdf"
    existing_pdf.parent.mkdir(parents=True)
    _make_pdf(existing_pdf, "Existing paper title\n10.1038/existing")
    existing_id = _insert_library_paper(existing_pdf, "existing", doi="10.1038/existing")

    duplicate = inbox / "duplicate.pdf"
    duplicate.write_bytes(existing_pdf.read_bytes())
    fresh = inbox / "sample2005.pdf"
    _make_pdf(fresh, "Neural Activity in Parietal Cortex\n10.1234/sample.2005.02")
    risky = inbox / "no_year.pdf"
    _make_pdf(risky, "short body without identifiers")

    cmd_inbox_scan(SimpleNamespace(steps=None, dry_run=True, force=True))
    output = capsys.readouterr().out
    assert "dry-run" in output
    assert "重复" in output
    assert "预计 year unknown" in output
    conn = get_conn()
    try:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
    finally:
        conn.close()

    cmd_inbox_scan(SimpleNamespace(steps=None, dry_run=False, force=True))
    output = capsys.readouterr().out
    conn = get_conn()
    try:
        tasks = conn.execute("SELECT id, payload_json FROM tasks").fetchall()
        assert len(tasks) == 2
        payloads = [json.loads(t["payload_json"])["inbox_path"] for t in tasks]
        assert str(duplicate) not in payloads
        assert str(fresh) in payloads
        assert str(risky) in payloads
    finally:
        conn.close()
    assert f"#{existing_id}" in output


class _Ctx:
    def __init__(self):
        self.task_id, self.paper_id = 0, None
        self.warns: list[str] = []

    def info(self, m):
        pass

    def warn(self, m):
        self.warns.append(m)

    def error(self, m):
        pass

    def progress(self, v):
        pass


class _FakeCR:
    record = None

    def get_work(self, doi):
        if self.record and doi.lower() == self.record["DOI"].lower():
            return self.record
        return None

    def search_best(self, q, **kw):
        return None


class _FakeOA:
    def get_work(self, doi):
        return {}

    def search_work(self, title, year=None, author=None):
        return {}


def _run_ingest(monkeypatch, path: Path) -> _Ctx:
    from backend.workers import ingest_worker as iw
    monkeypatch.setattr(iw, "CrossrefClient", _FakeCR)
    monkeypatch.setattr(iw, "OpenAlexClient", _FakeOA)
    ctx = _Ctx()
    iw.ingest_worker(ctx, {"inbox_path": str(path)})
    return ctx


def test_ingest_dedup_and_si_parent(temp_db, tmp_path, monkeypatch):
    doi = "10.1234/example.2006.01"
    title = "Population dynamics in association cortex during a perceptual decision task"
    _FakeCR.record = {
        "title": [title], "DOI": doi, "type": "journal-article",
        "issued": {"date-parts": [[2006]]}, "author": [{"family": "Example"}],
    }
    main = tmp_path / "main.pdf"
    _make_pdf(main, title + "\n" + doi)
    main_bytes = main.read_bytes()
    main_id = _run_ingest(monkeypatch, main).paper_id
    assert main_id

    duplicate = tmp_path / "duplicate.pdf"
    duplicate.write_bytes(main_bytes)
    duplicate_id = _run_ingest(monkeypatch, duplicate).paper_id
    assert duplicate_id == main_id

    si = tmp_path / "SI_main.pdf"
    _make_pdf(si, "Supplementary Information\n" + title + "\n" + doi)
    si_bytes = si.read_bytes()
    si_id = _run_ingest(monkeypatch, si).paper_id
    assert si_id != main_id

    si_duplicate = tmp_path / "SI_duplicate.pdf"
    si_duplicate.write_bytes(si_bytes)
    assert _run_ingest(monkeypatch, si_duplicate).paper_id == si_id

    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id, attachment_kind, parent_paper_id FROM papers ORDER BY id"
        ).fetchall()
        assert len(rows) == 2
        assert rows[0]["id"] == main_id
        assert rows[1]["id"] == si_id
        assert rows[1]["attachment_kind"] == "si"
        assert rows[1]["parent_paper_id"] == main_id
    finally:
        conn.close()


def test_duplicate_reingest_warns_when_inferred_dir_differs(
        temp_db, tmp_path, monkeypatch):
    """重复 DOI 复用时，推断出的 dir_name/year 与现有记录不一致要告警。"""
    doi = "10.1234/example.2006.01"
    title = "Population dynamics in association cortex during a perceptual decision task"
    _FakeCR.record = {
        "title": [title], "DOI": doi, "type": "journal-article",
        "issued": {"date-parts": [[2006]]}, "author": [{"family": "Example"}],
    }
    first = tmp_path / "example2006.pdf"
    _make_pdf(first, title + "\n" + doi)
    first_id = _run_ingest(monkeypatch, first).paper_id
    assert first_id

    _FakeCR.record = {
        "title": [title], "DOI": doi, "type": "journal-article",
        "issued": {"date-parts": [[2026]]}, "author": [{"family": "Sample"}],
    }
    second = tmp_path / "Sample 等 - 2026 - paper.pdf"
    _make_pdf(second, title + "\n" + doi)
    ctx = _run_ingest(monkeypatch, second)

    assert ctx.paper_id == first_id
    assert any("目录与现有记录不一致" in w for w in ctx.warns)
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT dir_name, year FROM papers WHERE id = ?", (first_id,)
        ).fetchone()
    finally:
        conn.close()
    assert row["dir_name"] == "example2006_population_dynamics"
    assert row["year"] == 2006


def test_parent_migration_links_existing_si(tmp_path):
    import importlib.util
    import sqlite3
    from backend import config as cfg

    spec = importlib.util.spec_from_file_location(
        "parent_migration",
        Path(cfg.__file__).parent / "db/migrations/20261024_paper_parent.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    conn = sqlite3.connect(tmp_path / "old.db")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE papers (id INTEGER PRIMARY KEY, dir_name TEXT, year INTEGER,"
        " first_author TEXT, doi TEXT, attachment_kind TEXT)"
    )
    conn.execute("INSERT INTO papers VALUES (1, 'main', 2024, 'a', '10.1/x', NULL)")
    conn.execute("INSERT INTO papers VALUES (2, 'si', 2024, 'a', '10.1/x', 'si')")

    stats = mod.migrate(conn)
    assert stats["added_column"] is True
    assert stats["linked_si"] == 1
    assert conn.execute("SELECT parent_paper_id FROM papers WHERE id = 2").fetchone()[0] == 1

    stats2 = mod.migrate(conn)
    assert stats2["added_column"] is False
    assert stats2["linked_si"] == 0
    conn.close()


def test_mcp_batch_ingest_and_task_list(temp_db, tmp_path, monkeypatch):
    from backend import config as cfg
    from backend import mcp_server

    inbox = tmp_path / "inbox"
    monkeypatch.setitem(cfg.CONFIG["paths"], "inbox_dir", str(inbox))
    good = tmp_path / "good.pdf"
    good.write_bytes(b"%PDF-1.7\n" + b"x" * 2000)
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"not a pdf")

    result = json.loads(mcp_server.ingest_local_files([str(good), str(bad)]))
    assert result["count"] == 2
    assert result["ok"] == 1
    assert result["failed"] == 1
    assert result["items"][1]["error"]

    listed = json.loads(mcp_server.list_tasks(status="pending", limit=10))
    assert listed["count"] == 1
    assert listed["items"][0]["status"] == "pending"
    assert "payload_json" not in listed["items"][0]
    assert "error" in json.loads(mcp_server.list_tasks(status="bogus"))


def test_ingest_by_url_error_hint(temp_db, tmp_path, monkeypatch):
    from backend import config as cfg
    from backend import mcp_server
    from backend.services.pdf_download import DownloadError

    monkeypatch.setitem(cfg.CONFIG["paths"], "inbox_dir", str(tmp_path / "inbox"))
    monkeypatch.setattr(
        "backend.services.pdf_download.stage_pdf_from_url",
        lambda *a, **kw: (_ for _ in ()).throw(DownloadError("403 Forbidden")),
    )
    result = json.loads(mcp_server.ingest_by_url("https://8.8.8.8/paper.pdf"))
    assert "403" in result["error"]
    assert "ingest_local_file" in result["hint"]


def test_doctor_queue_stats(temp_db):
    from backend.db import tasks_repo
    from backend.services.doctor import mcp_processes, task_queue_stats

    tasks_repo.create_task("pipeline", {})
    running = tasks_repo.create_task("summary", {})
    tasks_repo.claim_pending(["summary"])
    stats = task_queue_stats()
    assert stats["counts"]["pending"] == 1
    assert stats["counts"]["running"] == 1
    assert stats["oldest_running"]["id"] == running
    assert isinstance(mcp_processes(), list)
