"""Test: papers.url 列（schema/migration/upsert + enrich 带回与手动值保留）。"""
import importlib.util
from pathlib import Path

from backend.db.conn import get_conn


def _paper_cols() -> set[str]:
    conn = get_conn()
    try:
        return {r["name"] for r in conn.execute("PRAGMA table_info(papers)").fetchall()}
    finally:
        conn.close()


class _Ctx:
    def __init__(self):
        self.task_id, self.paper_id = 0, None

    def info(self, m):
        pass

    def warn(self, m):
        pass

    def error(self, m):
        pass

    def progress(self, v):
        pass


def _insert_paper(*, dir_name, url=None) -> int:
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO papers (dir_name, year, first_author, title, url)"
            " VALUES (?, 2023, 'test', 'Test Paper On Neurons', ?)",
            (dir_name, url),
        )
        return cur.lastrowid
    finally:
        conn.close()


def _fake_work(landing_page_url):
    loc = {"source": {"display_name": "J Test"}}
    if landing_page_url is not None:
        loc["landing_page_url"] = landing_page_url
    return {
        "id": "https://openalex.org/W0000000002",
        "doi": None,
        "title": "Test Paper On Neurons",
        "publication_year": 2023,
        "type": "article",
        "authorships": [{"author": {"display_name": "Test Author"}}],
        "primary_location": loc,
        "open_access": {"oa_status": "gold"},
        "cited_by_count": 0,
        "concepts": [],
        "locations": [],
        "abstract_inverted_index": None,
    }


def _patch_oa(monkeypatch, work) -> None:
    from backend.external.openalex import OpenAlexClient
    monkeypatch.setattr(OpenAlexClient, "get_work", lambda self, doi: {})
    monkeypatch.setattr(OpenAlexClient, "search_work",
                        lambda self, title, year=None, author=None: work)
    monkeypatch.setattr(OpenAlexClient, "search_work_by_author_year",
                        lambda self, author, year: {})


def test_schema_contains_url(temp_db):
    assert "url" in _paper_cols()


def test_migration_adds_column_and_is_idempotent(temp_db):
    conn = get_conn()
    try:
        conn.execute("ALTER TABLE papers DROP COLUMN url")
    finally:
        conn.close()
    assert "url" not in _paper_cols()

    spec = importlib.util.spec_from_file_location(
        "m20260916_url",
        Path(__file__).resolve().parents[1] / "db/migrations/20260916_url.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.migrate()
    assert "url" in _paper_cols()
    mod.migrate()  # 幂等：已存在时跳过
    assert "url" in _paper_cols()


def test_upsert_paper_writes_url(temp_db):
    from backend.db.repo import upsert_paper
    conn = get_conn()
    try:
        conn.execute("BEGIN")
        upsert_paper(conn, {"dir_name": "u2024_x", "year": 2024, "first_author": "u",
                            "title": "T", "url": "https://openreview.net/forum?id=x"})
        conn.execute("COMMIT")
        row = conn.execute(
            "SELECT url FROM papers WHERE dir_name = 'u2024_x'").fetchone()
    finally:
        conn.close()
    assert row["url"] == "https://openreview.net/forum?id=x"


def test_enrich_carries_landing_page_url(temp_db, monkeypatch):
    from backend.workers.enrich_worker import enrich_worker
    _patch_oa(monkeypatch, _fake_work("https://openreview.net/forum?id=z"))
    pid = _insert_paper(dir_name="url_carry")
    enrich_worker(_Ctx(), {"paper_id": pid})
    conn = get_conn()
    try:
        row = conn.execute("SELECT url FROM papers WHERE id = ?", (pid,)).fetchone()
    finally:
        conn.close()
    assert row["url"] == "https://openreview.net/forum?id=z"


def test_enrich_preserves_manual_url_when_record_has_none(temp_db, monkeypatch):
    from backend.workers.enrich_worker import enrich_worker
    _patch_oa(monkeypatch, _fake_work(None))
    pid = _insert_paper(dir_name="url_keep", url="https://manual.example/x")
    enrich_worker(_Ctx(), {"paper_id": pid})
    conn = get_conn()
    try:
        row = conn.execute("SELECT url FROM papers WHERE id = ?", (pid,)).fetchone()
    finally:
        conn.close()
    assert row["url"] == "https://manual.example/x"


def test_extract_work_info_filters_doiorg_landing():
    from backend.external.openalex import OpenAlexClient
    work = _fake_work("https://doi.org/10.1/x")
    info = OpenAlexClient.extract_work_info(work)
    assert info["url"] is None
    work2 = _fake_work("https://openreview.net/forum?id=z")
    assert OpenAlexClient.extract_work_info(work2)["url"] == \
        "https://openreview.net/forum?id=z"


def test_extract_work_info_upgrades_http_to_https():
    from backend.external.openalex import OpenAlexClient
    info = OpenAlexClient.extract_work_info(_fake_work("http://arxiv.org/abs/2311.02258"))
    assert info["url"] == "https://arxiv.org/abs/2311.02258"
