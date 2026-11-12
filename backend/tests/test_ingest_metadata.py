"""入库元数据抽取：首页标题、页面 DOI、作者与年份回退。"""
from pathlib import Path

import fitz

from backend.db.conn import get_conn
from backend.pipeline.pdf_classify import (
    extract_title_from_text,
    filename_author_year,
    first_author_from_pdf_metadata,
    is_junk_title,
)


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


def _cr_record(title: str, doi: str, year: int, family: str) -> dict:
    return {
        "title": [title], "DOI": doi, "type": "journal-article",
        "issued": {"date-parts": [[year]]},
        "author": [{"family": family}],
    }


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


def _run_ingest(tmp_path: Path, monkeypatch, pdf: Path) -> _Ctx:
    from backend.workers import ingest_worker as iw
    monkeypatch.setattr(iw, "CrossrefClient", _FakeCR)
    monkeypatch.setattr(iw, "OpenAlexClient", _FakeOA)
    ctx = _Ctx()
    iw.ingest_worker(ctx, {"inbox_path": str(pdf)})
    return ctx


def _paper(dir_name: str) -> dict:
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM papers WHERE dir_name = ?", (dir_name,)).fetchone()
        return dict(row) if row else {}
    finally:
        conn.close()


def test_printed_doi_survives_unusable_pdf_title(temp_db, tmp_path, monkeypatch):
    """只有排版标题和 DOI 时，按页面标识采纳 Crossref 记录，不再报 year unknown。"""
    doi = "10.1234/example.2006.01"
    title = "Population dynamics in association cortex during a perceptual decision task"
    _FakeCR.record = _cr_record(title, doi, 2006, "Example")
    pdf = tmp_path / "example2006.pdf"
    _make_pdf(pdf, "SBJNL_EX_0042 101..109\n" + doi + "\n1\n2\n3\n",
              title="SBJNL_EX_0042 101..109")

    ctx = _run_ingest(tmp_path, monkeypatch, pdf)

    p = _paper("example2006_population_dynamics")
    assert p["year"] == 2006
    assert p["first_author"] == "example"
    assert p["title"] == title
    assert p["doi"] == doi
    assert any("按页面标识采纳" in w for w in ctx.warns)


def test_page_doi_keeps_openalex_year_over_preprint_filename(temp_db, tmp_path, monkeypatch):
    """页面 DOI 自指校验通过后，文件名里的预印本年份不得否决 OpenAlex 年份。"""
    from backend.workers import ingest_worker as iw

    doi = "10.1234/sample.2026.05"
    title = ("Inferring network interactions using data-constrained"
             " recurrent models")
    _FakeCR.record = _cr_record(title, doi, 2026, "Sample")

    class _FakeOAPublished(_FakeOA):
        @staticmethod
        def title_similarity(a, b):
            from difflib import SequenceMatcher
            return SequenceMatcher(None, a.lower(), b.lower()).ratio()

        def get_work(self, doi_arg):
            assert doi_arg.lower() == doi.lower()
            return {"title": title, "publication_year": 2026}

        def extract_work_info(self, work):
            return {
                "title": work["title"],
                "pub_year": work["publication_year"],
                "authors": '["Alice B. Sample"]',
            }

    monkeypatch.setattr(iw, "CrossrefClient", _FakeCR)
    monkeypatch.setattr(iw, "OpenAlexClient", _FakeOAPublished)
    pdf = tmp_path / (
        "Sample 等 - 2020 - Inferring network interactions using"
        " data-constrained recurrent models.pdf"
    )
    _make_pdf(pdf, title + "\n" + doi)

    ctx = _Ctx()
    iw.ingest_worker(ctx, {"inbox_path": str(pdf)})

    p = _paper("sample2026_inferring_network")
    assert p["year"] == 2026
    assert p["doi"] == doi
    assert p["title"] == title
    assert any("年份与文件名不一致" in w for w in ctx.warns), ctx.warns


def test_first_page_title_and_crossref_year_fill_metadata(temp_db, tmp_path, monkeypatch):
    """PDF metadata 为空时，首页标题用于校验，Crossref 提供年份和作者。"""
    doi = "10.1234/sample.1992.04"
    title = "Microstimulation in visual cortex during a detection task"
    _FakeCR.record = _cr_record(title, doi, 1992, "Sample")
    pdf = tmp_path / "sample1992.pdf"
    _make_pdf(pdf, title + "\nSample, Coauthor\n" + doi + "\n")

    _run_ingest(tmp_path, monkeypatch, pdf)

    p = _paper("sample1992_microstimulation_visual")
    assert p["year"] == 1992
    assert p["first_author"] == "sample"
    assert p["title"] == title
    assert p["doi"] == doi


def test_pdf_author_metadata_used_for_dir_name(temp_db, tmp_path, monkeypatch):
    """Overleaf 模板标题被判垃圾，PDF 内嵌作者在一作为空时回退到目录名。"""
    from backend.workers import ingest_worker as iw
    title = "Distinct population dynamics in prefrontal and premotor cortex during flexible decisions"
    pdf = tmp_path / "2026_example_population_dynamics.pdf"
    _make_pdf(pdf, title + "\nTian Example; Alex Sample\n",
              title="Overleaf Example", authors="Tian Example; Alex Sample")

    class _FakeOAWithWork(_FakeOA):
        def search_work(self, title, year=None, author=None):
            return {"title": title, "publication_year": 2026}

        def extract_work_info(self, work):
            return {"title": work["title"], "pub_year": 2026, "authors": "[]"}

    _FakeCR.record = None
    monkeypatch.setattr(iw, "CrossrefClient", _FakeCR)
    monkeypatch.setattr(iw, "OpenAlexClient", _FakeOAWithWork)
    iw.ingest_worker(_Ctx(), {"inbox_path": str(pdf)})

    p = _paper("example2026_distinct_population")
    assert p["first_author"] == "example"
    assert p["title"] == title


def test_compact_filename_author_year():
    assert filename_author_year("sample2005") == ("sample", 2005)
    assert filename_author_year("sample1992") == ("sample", 1992)
    assert filename_author_year("Example2006_Population") == ("example", 2006)
    assert filename_author_year("ex0042") == (None, None)
    assert filename_author_year("no year in this name") == (None, None)


def test_junk_title_production_and_template_variants():
    assert is_junk_title("Untitled-63")
    assert is_junk_title("Overleaf Example")
    assert is_junk_title("ELIFE04677 1..21")
    assert is_junk_title("jcn.a.54 2295..2312")
    assert is_junk_title("SBJNL_EX_0042 101..109")
    assert not is_junk_title("Neural population dynamics during reaching")


def test_first_author_from_pdf_metadata():
    assert first_author_from_pdf_metadata("Tian Example; Alex Sample") == "example"
    assert first_author_from_pdf_metadata("Example, Alice B.; Sample, Bob C.") == "example"
    assert first_author_from_pdf_metadata("joe") is None


def test_extract_title_from_text_skips_production_headers():
    text = (
        "J Example Physiol 102: 614-635, 2009\n"
        "Single-Trial Analysis of Neural Population Activity "
        "Using Latent Factor Models\n"
        "Alice Example, Bob Sample, Carol Extra, Dan Probe\n"
        "Department of Example Neuroscience, Sample University\n"
    )
    assert extract_title_from_text(text) == (
        "Single-Trial Analysis of Neural Population Activity "
        "Using Latent Factor Models"
    )
