"""Test: Zotero 导出（RIS / BibTeX 合成）。"""
import json

from backend.db.conn import get_conn


def _paper(**kw):
    base = {
        "id": 1, "dir_name": "a2023_x", "year": 2023, "first_author": "alice",
        "keyword": "test", "title": "A Test Paper", "authors": json.dumps(["Alice Bob", "Carol Dan"]),
        "journal": "Nature", "doi": "10.1/x", "arxiv_id": None, "pub_type": "article",
        "abstract": "abs", "url": None, "pdf_path": "2023/a2023_x/a2023_x.pdf",
    }
    base.update(kw)
    return base


def test_synth_ris_basic():
    from backend.services.zotero_export import synth_ris
    ris = synth_ris(_paper())
    assert "TY  - JOUR" in ris
    assert "TI  - A Test Paper" in ris
    assert "AU  - Alice Bob" in ris
    assert "AU  - Carol Dan" in ris
    assert "PY  - 2023" in ris
    assert "JO  - Nature" in ris
    assert "DO  - 10.1/x" in ris
    assert "ER  - " in ris
    # 附件相对路径（linked-file）
    assert "L2  - file:pdf/2023/a2023_x/a2023_x.pdf" in ris


def test_synth_ris_arxiv_and_legacy_skip():
    from backend.services.zotero_export import synth_ris
    # arXiv 预印本
    ris = synth_ris(_paper(journal=None, arxiv_id="2401.12345", pub_type="preprint"))
    assert "TY  - PRE" in ris
    assert "UR  - https://arxiv.org/abs/2401.12345" in ris
    # legacy 路径不附附件
    ris2 = synth_ris(_paper(pdf_path="legacy:/nas/old.pdf"))
    assert "L2" not in ris2


def test_synth_ris_no_attachment():
    from backend.services.zotero_export import synth_ris
    ris = synth_ris(_paper(), include_attachment=False)
    assert "L2" not in ris


def test_synth_bibtex():
    from backend.services.zotero_export import synth_bibtex
    bib = synth_bibtex(_paper())
    assert bib.startswith("@article{alice2023_test,")
    assert "author = {Alice Bob and Carol Dan}" in bib
    assert "journal = {Nature}" in bib
    assert "doi = {10.1/x}" in bib
    # 特殊字符转义
    bib2 = synth_bibtex(_paper(title="A_B & C"))
    assert "A\\_B \\& C" in bib2


def test_synth_bibtex_backslash_title():
    """含反斜杠标题：\\textbackslash{} 自带的 {} 不得被二次转义成 \\textbackslash\\{\\}。"""
    from backend.services.zotero_export import _bib_escape, synth_bibtex
    assert _bib_escape("A \\B {C}") == "A \\textbackslash{}B \\{C\\}"
    bib = synth_bibtex(_paper(title="Study of \\alpha_{x}"))
    assert "\\textbackslash{}alpha\\_\\{x\\}" in bib
    assert "\\textbackslash\\{\\}" not in bib


def test_export_from_db(temp_db):
    from backend.services.zotero_export import export_ris, export_bibtex
    conn = get_conn()
    try:
        conn.execute(
            """INSERT INTO papers (dir_name, year, first_author, keyword, title, authors, journal, doi, pdf_path)
               VALUES ('a2023_x', 2023, 'alice', 'test', 'T1', '["Alice"]', 'J', '10.1/x', '2023/a/a.pdf')""")
        conn.execute(
            """INSERT INTO papers (dir_name, year, first_author, keyword, title, pdf_path)
               VALUES ('b2024_y', 2024, 'bob', 'kw', 'T2', NULL)""")
    finally:
        conn.close()

    ris, n = export_ris()
    assert n == 2
    assert "TI  - T1" in ris and "TI  - T2" in ris
    # T1 有 pdf_path -> 带 L2；T2 无 -> 不带
    assert "L2  - file:pdf/2023/a/a.pdf" in ris

    bib, n2 = export_bibtex()
    assert n2 == 2
    assert "@article{alice2023_test," in bib
    assert "@article{bob2024_kw," in bib


def test_synth_ris_url_when_no_arxiv():
    from backend.services.zotero_export import synth_ris
    ris = synth_ris(_paper(arxiv_id=None, doi=None,
                           url="https://openreview.net/forum?id=bcTjW5kS4W"))
    assert "UR  - https://openreview.net/forum?id=bcTjW5kS4W" in ris


def test_synth_ris_arxiv_beats_url():
    from backend.services.zotero_export import synth_ris
    ris = synth_ris(_paper(arxiv_id="2401.12345", url="https://example.org/paper"))
    assert "UR  - https://arxiv.org/abs/2401.12345" in ris
    assert "example.org" not in ris


def test_synth_bibtex_url_field():
    from backend.services.zotero_export import synth_bibtex
    bib = synth_bibtex(_paper(url="https://openreview.net/forum?id=bcTjW5kS4W"))
    assert "url = {https://openreview.net/forum?id=bcTjW5kS4W}" in bib
