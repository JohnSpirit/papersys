"""MCP 检索工具：DOI 回退、批量查询与篇内定位。"""
import json

from backend.db.conn import get_conn
from backend.db.vectors import pack


def _j(text: str) -> dict:
    return json.loads(text)


def _insert_paper(dir_name: str, *, doi=None, title="Test Paper", parsed_path=None) -> int:
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO papers (dir_name, year, first_author, title, doi, parsed_path,"
            " status_parsed) VALUES (?, 2024, 'test', ?, ?, ?, 'ok')",
            (dir_name, title, doi, parsed_path),
        )
        return cur.lastrowid
    finally:
        conn.close()


def test_doi_search_falls_back_to_crossref(temp_db, monkeypatch):
    from backend import mcp_server

    class _FakeSS:
        def by_doi(self, doi):
            return None

    class _FakeOA:
        def get_work(self, doi):
            return {}

    class _FakeCR:
        def get_work(self, doi):
            return {
                "title": ["Factor analysis of single-trial neural population activity"],
                "DOI": doi, "type": "journal-article",
                "issued": {"date-parts": [[2009]]},
                "author": [{"given": "Alice", "family": "Example"}],
                "container-title": ["Journal of Example Physiology"],
            }

    monkeypatch.setattr("backend.external.semantic_scholar.SemanticScholarClient", _FakeSS)
    monkeypatch.setattr("backend.external.openalex.OpenAlexClient", _FakeOA)
    monkeypatch.setattr("backend.external.crossref.CrossrefClient", _FakeCR)

    result = _j(mcp_server.search_papers("semantic_scholar", "10.1234/example.2009.03"))
    assert result["count"] == 1
    assert result["sources_tried"] == ["semantic_scholar", "openalex", "crossref"]
    assert result["items"][0]["title"] == "Factor analysis of single-trial neural population activity"
    assert result["items"][0]["year"] == 2009


def test_doi_search_stops_at_openalex_hit(temp_db, monkeypatch):
    from backend import mcp_server

    class _FakeSS:
        def by_doi(self, doi):
            return None

    class _FakeOA:
        def get_work(self, doi):
            return {
                "id": "W1", "title": "Example 2006", "doi": f"https://doi.org/{doi}",
                "publication_year": 2006, "authorships": [],
                "primary_location": {"source": {"display_name": "Example Journal"}},
            }

    class _ShouldNotRunCR:
        def get_work(self, doi):
            raise AssertionError("crossref should not be called")

    monkeypatch.setattr("backend.external.semantic_scholar.SemanticScholarClient", _FakeSS)
    monkeypatch.setattr("backend.external.openalex.OpenAlexClient", _FakeOA)
    monkeypatch.setattr("backend.external.crossref.CrossrefClient", _ShouldNotRunCR)

    result = _j(mcp_server.search_papers("semantic_scholar", "10.1234/example.2006.01"))
    assert result["count"] == 1
    assert result["sources_tried"] == ["semantic_scholar", "openalex"]
    assert result["items"][0]["title"] == "Example 2006"


def test_search_papers_brief_removes_abstract_and_tldr(temp_db, monkeypatch):
    from backend import mcp_server

    class _FakeSS:
        def search(self, query, *, limit=20, year=None):
            return [{
                "source": "semantic_scholar", "title": "Paper", "doi": "10.1/x",
                "abstract": "long abstract", "tldr": "short summary",
                "authors": [], "year": 2024,
            }]

    monkeypatch.setattr("backend.external.semantic_scholar.SemanticScholarClient", _FakeSS)
    full = _j(mcp_server.search_papers("semantic_scholar", "anything"))
    assert full["items"][0]["abstract"] == "long abstract"
    brief = _j(mcp_server.search_papers("semantic_scholar", "anything", brief=True))
    assert "abstract" not in brief["items"][0]
    assert "tldr" not in brief["items"][0]
    assert brief["items"][0]["title"] == "Paper"


def test_batch_doi_and_id_queries(temp_db):
    from backend import mcp_server
    pid1 = _insert_paper("batch_a", doi="10.1234/example.2006.01", title="Example 2006")
    pid2 = _insert_paper("batch_b", doi="10.1234/example.2006.01", title="Example 2006 copy")
    pid3 = _insert_paper("batch_c", doi="10.1234/other", title="Other")

    result = _j(mcp_server.get_papers_by_dois([
        "https://doi.org/10.1234/EXAMPLE.2006.01", "10.1234/missing",
    ]))
    assert result["count"] == 2
    first = result["items"][0]
    assert first["count"] == 2
    assert first["ids"] == [pid1, pid2]
    assert first["title"] == "Example 2006"
    assert result["items"][1]["count"] == 0

    result2 = _j(mcp_server.get_papers([pid3, 99999, pid1]))
    assert result2["count"] == 2
    assert [p["id"] for p in result2["items"]] == [pid1, pid3]
    assert result2["missing"] == [99999]
    assert "abstract" not in result2["items"][0]


class _FakeHostedClient:
    def embed(self, texts):
        return [[1.0, 0.0] for _ in texts]

    def rerank(self, query, docs):
        return [{"index": 0, "score": 0.99}]


def _fake_hrc():
    return ({"rerank_enabled": True, "candidate_multiplier": 10,
             "rerank_threshold": 0.0, "search_max_embeddings": 200000}, None)


def test_semantic_search_returns_character_offsets(temp_db, tmp_path, monkeypatch):
    from backend.services.semantic_search import semantic_search

    chunk = "The Fano factor is computed from spike-count variance across trials."
    full_text = "# Methods\n\nintro\n\n" + chunk + "\n\nmore text"
    md = tmp_path / "full.md"
    md.write_text(full_text, encoding="utf-8")
    pid = _insert_paper("ss_offsets", parsed_path=str(md))
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO embeddings (paper_id, chunk_id, chunk_index, text, vector, dim, model)"
            " VALUES (?, 'c0', 0, ?, ?, 2, 'test')",
            (pid, chunk, pack([1.0, 0.0])),
        )
    finally:
        conn.close()
    monkeypatch.setattr("backend.services.semantic_search.hosted_retrieval_config", _fake_hrc)
    monkeypatch.setattr("backend.external.hosted_retrieval.HostedRetrievalClient",
                        _FakeHostedClient)

    result = semantic_search("Fano factor", topk=1, paper_id=pid)
    assert result["count"] == 1
    item = result["items"][0]
    assert item["char_start"] == full_text.index(chunk)
    assert item["char_end"] == full_text.index(chunk) + len(chunk)
    assert full_text[item["char_start"]:item["char_end"]] == chunk


def test_search_fulltext_returns_offsets_and_context(temp_db, tmp_path):
    from backend import mcp_server
    text = "alpha\nFano factor definition\nbeta\nsecond Fano mention\nomega"
    md = tmp_path / "full.md"
    md.write_text(text, encoding="utf-8")
    pid = _insert_paper("fulltext_hits", parsed_path=str(md))

    result = _j(mcp_server.search_fulltext(pid, "fano", context_chars=12))
    assert result["available"] is True
    assert result["count"] == 2
    assert result["total_hits"] == 2
    assert result["has_more"] is False
    assert result["items"][0]["offset"] == text.index("Fano")
    assert "Fano factor" in result["items"][0]["text"]
    assert result["items"][1]["offset"] == text.index("Fano", text.index("second"))

    empty = _j(mcp_server.search_fulltext(pid, "  "))
    assert "error" in empty


def test_citations_exclude_self_edges(temp_db):
    from backend import mcp_server
    src = _insert_paper("cite_src")
    dst = _insert_paper("cite_dst")
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO citations (src_paper_id, dst_paper_id, dst_title, dst_year)"
            " VALUES (?, ?, 'Self', 2024)", (src, src),
        )
        conn.execute(
            "INSERT INTO citations (src_paper_id, dst_paper_id, dst_title, dst_year)"
            " VALUES (?, ?, 'Other', 2023)", (src, dst),
        )
        conn.execute(
            "INSERT INTO citations (src_paper_id, dst_paper_id, dst_title, dst_year)"
            " VALUES (?, ?, 'Incoming', 2022)", (dst, src),
        )
    finally:
        conn.close()

    refs = _j(mcp_server.get_citations(src, direction="refs"))
    assert refs["count"] == 1
    assert refs["items"][0]["dst_paper_id"] == dst
    cited_by = _j(mcp_server.get_citations(src, direction="cited_by"))
    assert cited_by["count"] == 1
    assert cited_by["items"][0]["dst_paper_id"] == dst
