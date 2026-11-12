"""Test: attachment_kind 列——SI 附件标记的入库写入、回填幂等、下游四处排除。

覆盖 20261015 migration 及其消费方：
- detect_si 三信号（投递文件名 / pdf_title 模板标题 / 解析文本头部）
- migration 幂等 + 不覆盖人工修正值
- enrich：SI 行低分只记 info；同标题重复预警排除 SI 行
- semantic_search：去重按「标题+附件类别」分桶，SI 与正文互不挤占
- zotero_export：SI 行不导出
"""
import importlib.util
import json
from pathlib import Path

from backend.db.conn import get_conn


def _load_migration():
    from backend import config as cfg
    spec = importlib.util.spec_from_file_location(
        "attach_kind_migration",
        Path(cfg.__file__).parent / "db/migrations/20261015_paper_attachment_kind.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── detect_si 纯函数 ──────────────────────────────────────

def test_detect_si_filename_signals():
    m = _load_migration()
    assert m.detect_si("SI_Author 等 - 2015 - xxx", "", "")
    assert m.detect_si("41586_2006_BFnature01234_MOESM1_ESM", "", "")
    assert m.detect_si("science.aabb1234_sm", "", "")
    assert m.detect_si("author2015_xxx", "", "") is False


def test_detect_si_pdf_title_signal():
    m = _load_migration()
    assert m.detect_si("", "Supporting Online Material for\nExampleProbe 2.0", "")
    assert m.detect_si("", "Supplementary Information", "")
    assert m.detect_si("", "Microsoft Word - 1234", "") is False  # 模板垃圾标题但非 SI 首词


def test_detect_si_parsed_head_signal():
    m = _load_migration()
    head = "Supporting Online Material for\nExampleProbe 2.0 probe\nFigure S1"
    assert m.detect_si("", "", head)
    assert m.detect_si("", "", "Abstract\nWe recorded neurons from cortex") is False


def test_detect_si_parsed_head_false_positives_from_production():
    """回填实测的 3 例假阳性：正文首页指向 SI 的 boilerplate 不算命中。"""
    m = _load_migration()
    # MIT Press 页脚
    mit = ("\n\ndoi: https://doi.org/10.1234/example.2020.07\n\n"
           "supporting information: https://doi.org/10.1234/example.2020.07")
    assert m.detect_si("", "", mit) is False
    # Science 页脚
    sci = ("of this article at: http://www.sciencemag.org/content/000/0000/000\n\n"
           "supporting online material can be found at: http://www.sciencemag.")
    assert m.detect_si("", "", sci) is False
    # ACS 导航栏 + 同页 Abstract
    acs = ("www.acsami.org\n\nResearch Article\n\n"
           "\\*sı supporting information\n\nabstract: a wide range of biomedical d")
    assert m.detect_si("", "", acs) is False
    # 真 SI 标题形态
    si = "# Supplementary Materials for\n\n# ExampleProbe 2.0: A miniaturized high-"
    assert m.detect_si("", "", si) is True
    # Elsevier/Cell Press "Supplemental Information"（2026-10-17 sample2015 mmc1 实测形态）
    assert m.detect_si("", "", "Neuron, Volume 87\nSupplemental Information\nTitle") is True


# ── migration 幂等与回填 ──────────────────────────────────

def _insert_paper(conn, dir_name, *, pdf_title=None, attachment_kind=None,
                  parsed_path=None) -> int:
    cur = conn.execute(
        "INSERT INTO papers (dir_name, year, first_author, pdf_title, parsed_path,"
        " attachment_kind) VALUES (?, 2023, 'a', ?, ?, ?)",
        (dir_name, pdf_title, parsed_path, attachment_kind),
    )
    return cur.lastrowid


def _insert_ingest_task(conn, paper_id, inbox_name) -> None:
    conn.execute(
        "INSERT INTO tasks (kind, status, payload_json, paper_id)"
        " VALUES ('ingest', 'ok', ?, ?)",
        (json.dumps({"inbox_path": f"/inbox/{inbox_name}"}), paper_id),
    )


def test_migration_backfill_and_idempotent(temp_db):
    from backend.db.conn import get_conn
    from backend import config as cfg
    mod = _load_migration()

    md = cfg.CONFIG["paths"]["data_dir"]
    Path(md).mkdir(parents=True, exist_ok=True)
    parsed = Path(md) / "si_parsed_head_full.md"
    parsed.write_text("Supplementary Information\nFigure S1. Recordings\n",
                      encoding="utf-8")

    conn = get_conn()
    try:
        p_stem = _insert_paper(conn, "author2015_si_stem")           # 信号=投递文件名
        _insert_ingest_task(conn, p_stem, "SI_Author 等 - 2015 - probe")
        p_title = _insert_paper(conn, "wang2020_si_title",
                                pdf_title="Supplementary Information")  # 信号=pdf_title
        p_head = _insert_paper(conn, "zhao2018_si_head",
                               parsed_path=str(parsed))           # 信号=解析文本头部
        p_main = _insert_paper(conn, "chen2021_main")
        p_manual = _insert_paper(conn, "qian2019_manual",
                                 attachment_kind="si")            # 人工已修正
        conn.commit()

        r1 = mod.migrate(conn)
        assert r1["marked_si"] == 3
        kinds = dict(conn.execute("SELECT dir_name, attachment_kind FROM papers").fetchall())
        assert kinds["author2015_si_stem"] == "si"
        assert kinds["wang2020_si_title"] == "si"
        assert kinds["zhao2018_si_head"] == "si"
        assert kinds["chen2021_main"] is None
        assert kinds["qian2019_manual"] == "si"

        # 二次执行：无新标记，且不覆盖已标记行
        r2 = mod.migrate(conn)
        assert r2["marked_si"] == 0
        kinds2 = dict(conn.execute("SELECT dir_name, attachment_kind FROM papers").fetchall())
        assert kinds2 == kinds
    finally:
        conn.close()


# ── enrich 排除 ───────────────────────────────────────────
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


_ABSTRACT_OK = " ".join(f"word{i}" for i in range(1, 41))
_ABSTRACT_BAD = " ".join(f"other{i}" for i in range(1, 41))
_PARSED = f"# Title\n\n{_ABSTRACT_OK}\n\n## Introduction\nbody\n"


def _fake_work(title: str, abstract: str) -> dict:
    return {
        "id": "https://openalex.org/W0000000001",
        "doi": None,
        "title": title,
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


def _insert_enrich_paper(dir_name, *, attachment_kind=None, parsed_path=None,
                         title="Test Paper On Neurons") -> int:
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO papers (dir_name, year, first_author, title, parsed_path,"
            " attachment_kind) VALUES (?, 2023, 'test', ?, ?, ?)",
            (dir_name, title, parsed_path, attachment_kind),
        )
        return cur.lastrowid
    finally:
        conn.close()


def test_enrich_si_low_containment_warns_suppressed(temp_db, monkeypatch, tmp_path):
    from backend.workers.enrich_worker import enrich_worker
    md = tmp_path / "full.md"
    md.write_text(_PARSED, encoding="utf-8")
    pid = _insert_enrich_paper("enr_si_low", attachment_kind="si", parsed_path=str(md))
    _patch_oa(monkeypatch, _fake_work("Test Paper On Neurons", _ABSTRACT_BAD))

    ctx = _Ctx()
    enrich_worker(ctx, {"paper_id": pid})

    assert not ctx.warns
    assert any("expected" in m and "SI" in m for m in ctx.infos)


def test_enrich_same_title_warning_skips_si_pair(temp_db, monkeypatch):
    """正文行与其 SI 附件行同标题：enrich 任一方都不报 same-title 重复。"""
    from backend.workers.enrich_worker import enrich_worker
    main = _insert_enrich_paper("enr_si_pair_main")
    si = _insert_enrich_paper("enr_si_pair_si", attachment_kind="si")
    _patch_oa(monkeypatch, _fake_work("Test Paper On Neurons", _ABSTRACT_OK))

    ctx_main = _Ctx()
    enrich_worker(ctx_main, {"paper_id": main})
    assert not any("same-title" in m for m in ctx_main.warns)

    ctx_si = _Ctx()
    enrich_worker(ctx_si, {"paper_id": si})
    assert not any("same-title" in m for m in ctx_si.warns)


def test_enrich_si_no_metadata_skips_manual_hold(temp_db, monkeypatch):
    """SI 行无 DOI/标题时 enrich 三层回退必失败：不标 manual_hold
    （semantic_search 排除 manual_hold 论文，SI 的价值在于被检索到）。
    2026-10-17 sample2015 mmc1 实测触发此路径。"""
    from backend.workers.enrich_worker import enrich_worker
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO papers (dir_name, year, first_author, attachment_kind)"
            " VALUES ('si2024_nometa', 2024, 'test', 'si')")
        pid = cur.lastrowid
    finally:
        conn.close()

    from backend.external.openalex import OpenAlexClient
    monkeypatch.setattr(OpenAlexClient, "get_work", lambda self, doi: {})
    monkeypatch.setattr(OpenAlexClient, "search_work",
                        lambda self, title, year=None, author=None: {})
    monkeypatch.setattr(OpenAlexClient, "search_work_by_author_year",
                        lambda self, author, year: {})

    ctx = _Ctx()
    enrich_worker(ctx, {"paper_id": pid})

    conn = get_conn()
    try:
        row = conn.execute("SELECT status_summary FROM papers WHERE id = ?",
                           (pid,)).fetchone()
    finally:
        conn.close()
    assert row["status_summary"] == "missing"
    assert not ctx.warns
    assert any("跳过 manual_hold" in m for m in ctx.infos)


def test_enrich_si_adopts_parent_metadata_without_manual_hold(temp_db, monkeypatch):
    """SI 行按母论文 DOI 富化：标题不兼容校验跳过（pdf_title 为 MOESM 文件名
    派生，与母论文标题比对必然失败），元数据直接采纳。2026-10-17 双 MOESM
    实测：sim=0.10 曾被 title mismatch 拒为 manual_hold。"""
    from backend.workers.enrich_worker import enrich_worker
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO papers (dir_name, year, first_author, doi, pdf_title,"
            " attachment_kind) VALUES ('si2026_moesm', 2026, 'unknown',"
            " '10.1234/example.2026.08', '40000_2026_0123_MOESM1_ESM', 'si')")
        pid = cur.lastrowid
    finally:
        conn.close()

    work = _fake_work("Test Paper On Neurons", _ABSTRACT_OK)
    from backend.external.crossref import CrossrefClient
    from backend.external.openalex import OpenAlexClient
    monkeypatch.setattr(OpenAlexClient, "get_work", lambda self, doi: work)
    monkeypatch.setattr(OpenAlexClient, "search_work",
                        lambda self, title, year=None, author=None: {})
    monkeypatch.setattr(OpenAlexClient, "search_work_by_author_year",
                        lambda self, author, year: {})
    monkeypatch.setattr(CrossrefClient, "fetch_bibtex", lambda self, doi: "")

    ctx = _Ctx()
    enrich_worker(ctx, {"paper_id": pid})

    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT title, abstract, status_summary FROM papers WHERE id = ?",
            (pid,)).fetchone()
    finally:
        conn.close()
    assert row["status_summary"] == "missing"
    assert row["title"] == "Test Paper On Neurons"
    assert row["abstract"]
    assert not ctx.warns
    assert any("title compatibility check skipped" in m for m in ctx.infos)


# ── semantic_search 分桶去重 ──────────────────────────────

def test_dedupe_si_and_parent_same_title_both_kept():
    from backend.services.semantic_search import _dedupe_by_paper
    items = [
        {"paper_id": 1, "title": "Same Paper Title", "score": 0.99},
        {"paper_id": 2, "title": "Same Paper Title", "score": 0.98,
         "attachment_kind": "si"},
        {"paper_id": 3, "title": "Other Title", "score": 0.7},
    ]
    out = _dedupe_by_paper(items)
    assert [it["paper_id"] for it in out] == [1, 2, 3]


def test_dedupe_same_bucket_still_dedupes():
    """同类别（均为正文）同标题仍互斥；两个 SI 同标题也只留一篇。"""
    from backend.services.semantic_search import _dedupe_by_paper
    items = [
        {"paper_id": 1, "title": "Dup Title", "score": 0.9},
        {"paper_id": 2, "title": "Dup Title", "score": 0.8},
        {"paper_id": 3, "title": "Si Title", "score": 0.7, "attachment_kind": "si"},
        {"paper_id": 4, "title": "Si Title", "score": 0.6, "attachment_kind": "si"},
    ]
    out = _dedupe_by_paper(items)
    assert [it["paper_id"] for it in out] == [1, 3]


# ── zotero_export 排除 ────────────────────────────────────

def test_export_excludes_si_rows(temp_db):
    from backend.services.zotero_export import export_bibtex, export_ris
    conn = get_conn()
    try:
        conn.execute(
            """INSERT INTO papers (dir_name, year, first_author, keyword, title, doi)
               VALUES ('main2023_x', 2023, 'alice', 'kw', 'Main Paper', '10.1/m')""")
        conn.execute(
            """INSERT INTO papers (dir_name, year, first_author, keyword, title, doi,
                      attachment_kind)
               VALUES ('si2023_x', 2023, 'alice', 'kw', 'Main Paper', '10.1/m', 'si')""")
        conn.commit()
    finally:
        conn.close()

    ris, n = export_ris()
    assert n == 1
    assert "Main Paper" in ris and ris.count("TY  -") == 1

    bib, n2 = export_bibtex()
    assert n2 == 1
    assert "Main Paper" in bib


# ── repo upsert 往返 ──────────────────────────────────────

def test_repo_upsert_attachment_kind_roundtrip(temp_db):
    from backend.db.repo import get_paper, upsert_paper
    conn = get_conn()
    try:
        pid, is_new = upsert_paper(conn, {
            "dir_name": "rt2024_si", "year": 2024, "first_author": "a",
            "attachment_kind": "si",
        })
        assert is_new
        paper = get_paper(conn, pid)
        assert paper["attachment_kind"] == "si"

        # 未指定字段保持原值：正文字段更新不改 attachment_kind
        upsert_paper(conn, {"dir_name": "rt2024_si", "title": "Later Enriched"})
        assert get_paper(conn, pid)["attachment_kind"] == "si"
    finally:
        conn.close()
