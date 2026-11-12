"""Test: ingest 的 arXiv 水印 DOI 与 OpenAlex 校验联动（3.1.0b4 修复回归）。

背景：Step 1c 从水印拼出的 10.48550/arxiv.* DOI 此前不在 Step 2 的
"校验失败即丢弃"来源集合里——水印误匹配（引用他人 arXiv id）时错误 DOI
直接入库。现在 OpenAlex 标题/年份否决时 arxiv 来源的 DOI 一并清除。

两个用例互为对照：
  * 匹配场景证明水印提取本身生效（否则"清除"用例会空过）
  * 不匹配场景检查 DOI 被清除
"""
from pathlib import Path

import fitz

from backend.db.conn import get_conn


class _Ctx:
    def __init__(self):
        self.task_id, self.paper_id = 0, None

    def info(self, m): pass
    def warn(self, m): pass
    def error(self, m): pass
    def progress(self, v): pass


def _make_arxiv_pdf(path: Path) -> None:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Neural Decoding of Memory Traces")
    page.insert_text((72, 100), "arXiv:2401.12345v2")
    doc.set_metadata({"title": "Neural Decoding of Memory Traces"})
    doc.save(str(path))
    doc.close()


def _run_ingest(tmp_path: Path, monkeypatch, oa_title: str, oa_year: int) -> None:
    """跑一次 ingest_worker；OpenAlex 返回指定标题/年份的记录，Crossref 不出数据。"""
    from backend.external.openalex import OpenAlexClient
    from backend.workers import ingest_worker as iw

    pdf = tmp_path / "Smith_2020_Neural Decoding.pdf"
    _make_arxiv_pdf(pdf)

    class _FakeOA(OpenAlexClient):
        def __init__(self):
            pass  # 跳过 RateLimitedClient.__init__（不建真实 cache 目录/session）

        def get_work(self, doi: str) -> dict:
            return {"id": "W1", "title": oa_title, "publication_year": oa_year}

        def extract_work_info(self, work: dict) -> dict:
            return {
                "title": work["title"],
                "pub_year": work["publication_year"],
                "authors": "[]",
            }

    class _FakeCR:
        def get_work(self, doi: str):
            return None

        def search_best(self, q: str, **kw):
            return None

    monkeypatch.setattr(iw, "OpenAlexClient", _FakeOA)
    monkeypatch.setattr(iw, "CrossrefClient", _FakeCR)
    iw.ingest_worker(_Ctx(), {"inbox_path": str(pdf)})


def _paper_doi() -> str | None:
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT doi FROM papers WHERE dir_name LIKE 'smith2020_%'"
        ).fetchone()
        return row["doi"] if row else "__no_row__"
    finally:
        conn.close()


def test_arxiv_doi_kept_when_oa_matches(temp_db, tmp_path, monkeypatch):
    """OpenAlex 命中同标题同年份：arXiv DOI 保留（证明水印提取路径生效）。"""
    _run_ingest(tmp_path, monkeypatch, "Neural Decoding of Memory Traces", 2020)
    assert _paper_doi() == "10.48550/arxiv.2401.12345"


def test_arxiv_doi_cleared_when_oa_mismatches(temp_db, tmp_path, monkeypatch):
    """OpenAlex 命中无关标题+年份差 30：arXiv 来源 DOI 必须被清除。"""
    _run_ingest(tmp_path, monkeypatch, "Completely Unrelated Topic", 1990)
    assert _paper_doi() is None
