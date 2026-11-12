"""论文 dir_name 重命名：保留论文 id、衍生数据与目录内容。"""
from pathlib import Path

import pytest

from backend.config import library_root
from backend.db.conn import get_conn
from backend.services.paper_maintenance import rename_paper_dir


def _seed_paper(dir_name="unknown2005_sample", year=2005, year_dir=None) -> tuple[int, Path]:
    year_dir = year_dir or str(year)
    old_dir = library_root() / year_dir / dir_name
    old_dir.mkdir(parents=True)
    pdf = old_dir / f"{dir_name}.pdf"
    pdf.write_bytes(b"%PDF-1.7\n" + b"x" * 2000)
    (old_dir / "full.md").write_text("# Paper\n\nbody", encoding="utf-8")
    (old_dir / "images").mkdir()
    (old_dir / "images" / "a.png").write_bytes(b"png")
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO papers (dir_name, year, first_author, title, pdf_path, parsed_path,"
            " summary_path) VALUES (?, ?, 'sample', 'Paper', ?, ?, ?)",
            (dir_name, year,
             f"{year_dir}/{dir_name}/{dir_name}.pdf",
             f"{year_dir}/{dir_name}/full.md",
             f"{year_dir}/{dir_name}/summary.md"),
        )
        pid = cur.lastrowid
        conn.execute(
            "INSERT INTO summaries (paper_id, content_md, version, source) VALUES (?, 'summary', 1, 'auto')",
            (pid,),
        )
        conn.execute(
            "INSERT INTO annotations (paper_id, type, anchor, payload_json, source)"
            " VALUES (?, 'comment', 'en', '{}', 'agent')", (pid,),
        )
    finally:
        conn.close()
    return pid, old_dir


def _paper(paper_id: int) -> dict:
    conn = get_conn()
    try:
        return dict(conn.execute("SELECT * FROM papers WHERE id = ?", (paper_id,)).fetchone())
    finally:
        conn.close()


def test_rename_paper_dir_dry_run_then_execute(temp_db):
    pid, old_dir = _seed_paper()
    plan = rename_paper_dir(pid, "sample2005_neural_activity", dry_run=True)
    assert plan["dry_run"] is True
    assert old_dir.exists()
    assert _paper(pid)["dir_name"] == "unknown2005_sample"

    result = rename_paper_dir(pid, "sample2005_neural_activity")
    assert result["ok"] is True
    new_dir = old_dir.parent / "sample2005_neural_activity"
    assert not old_dir.exists()
    assert (new_dir / "sample2005_neural_activity.pdf").exists()
    assert (new_dir / "full.md").read_text(encoding="utf-8") == "# Paper\n\nbody"
    assert (new_dir / "images" / "a.png").exists()

    p = _paper(pid)
    assert p["id"] == pid
    assert p["dir_name"] == "sample2005_neural_activity"
    assert p["pdf_path"] == "2005/sample2005_neural_activity/sample2005_neural_activity.pdf"
    assert p["parsed_path"] == "2005/sample2005_neural_activity/full.md"
    assert p["summary_path"] == "2005/sample2005_neural_activity/summary.md"

    conn = get_conn()
    try:
        assert conn.execute("SELECT content_md FROM summaries WHERE paper_id = ?", (pid,)).fetchone()[0] == "summary"
        assert conn.execute("SELECT COUNT(*) FROM annotations WHERE paper_id = ?", (pid,)).fetchone()[0] == 1
    finally:
        conn.close()


def test_rename_preserves_year_directory_even_if_year_field_differs(temp_db):
    pid, old_dir = _seed_paper(dir_name="unknown1982_untitled", year=1990, year_dir="1982")
    rename_paper_dir(pid, "sample1990_cortical_microstimulation")
    assert (old_dir.parent / "sample1990_cortical_microstimulation").exists()
    p = _paper(pid)
    assert p["year"] == 1990
    assert p["pdf_path"].startswith("1982/sample1990_cortical_microstimulation/")


def test_rename_rejects_db_conflict_without_changes(temp_db):
    pid, old_dir = _seed_paper()
    other, _ = _seed_paper(dir_name="other2005_paper")
    with pytest.raises(ValueError, match="已被"):
        rename_paper_dir(pid, "other2005_paper")
    assert old_dir.exists()
    assert _paper(pid)["dir_name"] == "unknown2005_sample"


def test_rename_rejects_invalid_name(temp_db):
    pid, _ = _seed_paper()
    with pytest.raises(ValueError):
        rename_paper_dir(pid, "../escape")


def test_rename_with_new_year_moves_directory_and_updates_year(temp_db):
    """new_year：目录跨年份文件夹移动并同步 papers.year（入库错位纠正）。"""
    pid, old_dir = _seed_paper(dir_name="unknown2005_untitled", year=2015, year_dir="2005")
    result = rename_paper_dir(pid, "sample2015_vacillation_indecision", new_year=2015)
    assert result["ok"] is True
    new_dir = library_root() / "2015" / "sample2015_vacillation_indecision"
    assert not old_dir.exists()
    assert (new_dir / "sample2015_vacillation_indecision.pdf").exists()
    assert (new_dir / "full.md").read_text(encoding="utf-8") == "# Paper\n\nbody"
    p = _paper(pid)
    assert p["year"] == 2015
    assert p["pdf_path"] == "2015/sample2015_vacillation_indecision/sample2015_vacillation_indecision.pdf"
    assert p["parsed_path"] == "2015/sample2015_vacillation_indecision/full.md"


def test_rename_with_new_year_dry_run_makes_no_changes(temp_db):
    pid, old_dir = _seed_paper(dir_name="unknown2005_untitled", year=2015, year_dir="2005")
    plan = rename_paper_dir(pid, "sample2015_vacillation_indecision", new_year=2015, dry_run=True)
    assert plan["new_year"] == 2015
    assert plan["year_before"] == 2015
    assert old_dir.exists()
    assert _paper(pid)["dir_name"] == "unknown2005_untitled"


def test_rename_with_new_year_rejects_bad_year(temp_db):
    pid, _ = _seed_paper()
    with pytest.raises(ValueError, match="合理范围"):
        rename_paper_dir(pid, "sample2005_neural_activity", new_year=1800)


def _insert_cited_paper(dir_name: str, doi: str | None, cited: int) -> int:
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO papers (dir_name, year, first_author, doi, cited_by_count)"
            " VALUES (?, 2024, 't', ?, ?)",
            (dir_name, doi, cited),
        )
        return cur.lastrowid
    finally:
        conn.close()


def _cited_by(paper_id: int) -> int:
    conn = get_conn()
    try:
        return conn.execute(
            "SELECT cited_by_count FROM papers WHERE id = ?", (paper_id,)
        ).fetchone()["cited_by_count"]
    finally:
        conn.close()


def test_audit_cited_by_counts_reports_and_fixes(temp_db, monkeypatch):
    """audit-citations：match/stale/suspect/oa_missing 分类、--fix 改写与 events。"""
    pid_a = _insert_cited_paper("a2024_match", "10.1/a", 100)
    pid_b = _insert_cited_paper("b2024_stale", "10.1/b", 30)
    pid_c = _insert_cited_paper("c2024_suspect", "10.1/c", 2630)
    pid_d = _insert_cited_paper("d2024_nooa", "10.1/d", 55)
    _insert_cited_paper("e2024_nodoi", None, 999)

    class _StubClient:
        def get_cited_by_dois_batch(self, dois):
            return {
                "10.1/a": {"openalex_id": "W1", "cited_by_count": 100},
                "10.1/b": {"openalex_id": "W2", "cited_by_count": 40},
                "10.1/c": {"openalex_id": "W3", "cited_by_count": 0},
            }

        def get_work(self, doi):
            # 批量漏检的单发回退：d 仍查无 → 维持 oa_missing
            return {}

    monkeypatch.setattr("backend.external.openalex.OpenAlexClient", _StubClient)
    from backend.services.paper_maintenance import audit_cited_by_counts

    rep = audit_cited_by_counts(fix=False)
    assert rep["checked"] == 4
    assert rep["no_doi_skipped"] == 1
    assert (rep["match"], rep["stale"], rep["suspect"], rep["oa_missing"]) == (1, 1, 1, 1)
    assert rep["fixed"] == 0
    assert {it["paper_id"] for it in rep["items"]} == {pid_b, pid_c, pid_d}
    assert _cited_by(pid_b) == 30 and _cited_by(pid_c) == 2630

    fixed = audit_cited_by_counts(fix=True)
    assert fixed["fixed"] == 2
    assert _cited_by(pid_b) == 40 and _cited_by(pid_c) == 0
    assert _cited_by(pid_d) == 55  # oa_missing 永不改写
    assert _cited_by(pid_a) == 100

    conn = get_conn()
    try:
        suspect_events = conn.execute(
            "SELECT COUNT(*) FROM events WHERE paper_id = ? AND level = 'warn'"
            " AND msg LIKE '%疑似坏值%'",
            (pid_c,),
        ).fetchone()[0]
        assert suspect_events == 1
    finally:
        conn.close()


def test_audit_fix_rolls_back_when_event_insert_fails(temp_db, monkeypatch):
    import sqlite3

    pid = _insert_cited_paper("audit2024_atomic", "10.1/atomic", 3)

    class _StubClient:
        def get_cited_by_dois_batch(self, dois):
            return {"10.1/atomic": {"openalex_id": "W1", "cited_by_count": 7}}

    monkeypatch.setattr("backend.external.openalex.OpenAlexClient", _StubClient)
    from backend.services.paper_maintenance import audit_cited_by_counts

    conn = get_conn()
    try:
        conn.execute(
            "CREATE TRIGGER reject_audit_event BEFORE INSERT ON events"
            " BEGIN SELECT RAISE(ABORT, 'forced event failure'); END"
        )
    finally:
        conn.close()

    with pytest.raises(sqlite3.IntegrityError, match="forced event failure"):
        audit_cited_by_counts(fix=True)
    assert _cited_by(pid) == 3

    conn = get_conn()
    try:
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
        conn.execute("DROP TRIGGER reject_audit_event")
    finally:
        conn.close()

    result = audit_cited_by_counts(fix=True)
    assert result["fixed"] == 1
    assert _cited_by(pid) == 7
    conn = get_conn()
    try:
        event_count = conn.execute(
            "SELECT COUNT(*) FROM events WHERE paper_id = ?", (pid,),
        ).fetchone()[0]
        assert event_count == 1
    finally:
        conn.close()
