"""Test: enrich 落库前的摘要↔解析文本重合校验（低分 warn / 通过 info / 无解析文本跳过）。"""
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


_ABSTRACT_OK = " ".join(f"word{i}" for i in range(1, 41))    # 与解析文本一致
_ABSTRACT_BAD = " ".join(f"other{i}" for i in range(1, 41))  # 与解析文本无关
_PARSED = f"# Title\n\n{_ABSTRACT_OK}\n\n## Introduction\nbody\n"


def _insert_paper(parsed_path, *, dir_name) -> int:
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO papers (dir_name, year, first_author, title, parsed_path)"
            " VALUES (?, 2023, 'test', 'Test Paper On Neurons', ?)",
            (dir_name, parsed_path),
        )
        return cur.lastrowid
    finally:
        conn.close()


def _fake_work(abstract: str) -> dict:
    """构造 OpenAlex work：标题与 DB 一致、无 DOI（走 search_work 回退路径）。"""
    return {
        "id": "https://openalex.org/W0000000001",
        "doi": None,
        "title": "Test Paper On Neurons",
        "publication_year": 2023,
        "type": "article",
        "authorships": [{"author": {"display_name": "Test Author"}}],
        "primary_location": {"source": {"display_name": "J Test"}},
        "open_access": {"oa_status": "gold"},
        "cited_by_count": 0,
        "concepts": [],
        "locations": [],
        "abstract_inverted_index": {w: [i] for i, w in enumerate(abstract.split())},
    }


def _patch_oa(monkeypatch, work) -> None:
    from backend.external.openalex import OpenAlexClient
    monkeypatch.setattr(OpenAlexClient, "get_work", lambda self, doi: {})
    monkeypatch.setattr(OpenAlexClient, "search_work",
                        lambda self, title, year=None, author=None: work)
    monkeypatch.setattr(OpenAlexClient, "search_work_by_author_year",
                        lambda self, author, year: {})


def test_enrich_warns_on_low_abstract_containment(temp_db, monkeypatch, tmp_path):
    md = tmp_path / "full.md"
    md.write_text(_PARSED, encoding="utf-8")
    pid = _insert_paper(str(md), dir_name="enr_abs_low")
    _patch_oa(monkeypatch, _fake_work(_ABSTRACT_BAD))

    ctx = _Ctx()
    enrich_worker(ctx, {"paper_id": pid})

    assert any("containment" in m and "low" in m for m in ctx.warns)
    assert not any("low" in m for m in ctx.infos)


def test_enrich_containment_match_logs_info_only(temp_db, monkeypatch, tmp_path):
    md = tmp_path / "full.md"
    md.write_text(_PARSED, encoding="utf-8")
    pid = _insert_paper(str(md), dir_name="enr_abs_ok")
    _patch_oa(monkeypatch, _fake_work(_ABSTRACT_OK))

    ctx = _Ctx()
    enrich_worker(ctx, {"paper_id": pid})

    assert not ctx.warns
    assert any("containment" in m for m in ctx.infos)
    conn = get_conn()
    try:
        row = conn.execute("SELECT abstract FROM papers WHERE id = ?", (pid,)).fetchone()
    finally:
        conn.close()
    assert "word1" in row["abstract"]


def test_enrich_containment_skips_without_parsed_text(temp_db, monkeypatch):
    pid = _insert_paper(None, dir_name="enr_abs_noparse")
    _patch_oa(monkeypatch, _fake_work(_ABSTRACT_OK))

    ctx = _Ctx()
    enrich_worker(ctx, {"paper_id": pid})

    assert not ctx.warns
    assert any("skip" in m and "no parsed text" in m for m in ctx.infos)
