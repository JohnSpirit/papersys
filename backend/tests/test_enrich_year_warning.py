"""enrich 年份不一致告警：pdf_title 有效时也要暴露入库年份偏差。"""
from backend.db.conn import get_conn
from backend.workers.enrich_worker import enrich_worker


class _Ctx:
    def __init__(self):
        self.task_id, self.paper_id = 0, None
        self.infos: list[str] = []
        self.warns: list[str] = []

    def info(self, m):
        self.infos.append(m)

    def warn(self, m):
        self.warns.append(m)

    def error(self, m):
        pass

    def progress(self, v):
        pass


def _insert_paper() -> int:
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO papers (dir_name, year, first_author, title, doi, pdf_title)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                "sample2020_inferring_network",
                2020,
                "sample",
                "Inferring network interactions using data-constrained"
                " recurrent models",
                "10.1234/sample.2026.05",
                "Inferring network interactions using data-constrained"
                " recurrent models",
            ),
        )
        return cur.lastrowid
    finally:
        conn.close()


def test_enrich_warns_on_year_mismatch_with_valid_pdf_title(temp_db, monkeypatch):
    """pdf_title 校验只比标题时不静默保留 2020/2026 的年份差。"""
    from backend.external.crossref import CrossrefClient
    from backend.external.openalex import OpenAlexClient

    info = {
        "doi": "10.1234/sample.2026.05",
        "title": "Inferring network interactions using data-constrained"
                 " recurrent models",
        "pub_year": 2026,
        "abstract": " ".join(f"word{i}" for i in range(1, 41)),
        "authors": '["Alice B. Sample"]',
        "journal": "Neuron",
        "openalex_id": "https://openalex.org/W1234567890",
        "url": None,
        "arxiv_id": None,
        "pub_type": "article",
        "oa_status": "hybrid",
        "cited_by_count": 59,
        "concepts": "[]",
    }
    monkeypatch.setattr(OpenAlexClient, "get_work", lambda self, doi: {"id": "W1"})
    monkeypatch.setattr(
        OpenAlexClient, "extract_work_info", staticmethod(lambda work: info)
    )
    monkeypatch.setattr(CrossrefClient, "fetch_bibtex", lambda self, doi: "")

    pid = _insert_paper()
    ctx = _Ctx()
    enrich_worker(ctx, {"paper_id": pid})

    assert any("pub_year=2026" in w and "2020" in w for w in ctx.warns)
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT year, status_enrich FROM papers WHERE id = ?", (pid,)
        ).fetchone()
    finally:
        conn.close()
    assert row["year"] == 2020
    assert row["status_enrich"] == "ok"
