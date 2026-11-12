"""Test: MCP 服务器工具（进程内直调，不走 stdio）。

验证核心工具在真实 DB（temp_db）上的行为与错误约定：
错误一律以 {"error": ...} 字符串返回，永不抛异常（红线 11）。
"""
import json
from pathlib import Path

import pytest

from backend.db.conn import get_conn


def _insert_paper(parsed_path: str | None = None, *, dir_name="mcp_t") -> int:
    conn = get_conn()
    try:
        cur = conn.execute(
            """INSERT INTO papers (dir_name, year, first_author, title, parsed_path, status_parsed)
               VALUES (?, 2023, 'test', 'MCP Test Paper', ?, 'ok')""",
            (dir_name, parsed_path),
        )
        return cur.lastrowid
    finally:
        conn.close()


def _j(s: str) -> dict:
    return json.loads(s)


def test_get_paper_not_found(temp_db):
    from backend.mcp_server import get_paper
    r = _j(get_paper(99999))
    assert "error" in r


def test_get_paper_ok(temp_db):
    from backend.mcp_server import get_paper
    pid = _insert_paper(dir_name="mcp_get")
    r = _j(get_paper(pid))
    assert r["id"] == pid
    assert r["title"] == "MCP Test Paper"
    assert r["status"]["parsed"] == "ok"


def test_search_library(temp_db, tmp_path):
    from backend.mcp_server import search_library
    _insert_paper(dir_name="mcp_search")

    r = _j(search_library(q="MCP Test"))
    assert r["count"] == 1
    assert r["items"][0]["title"] == "MCP Test Paper"
    assert r["matched_by"] == "metadata"

    r2 = _j(search_library(q="不存在的关键字"))
    assert r2["count"] == 0


def _insert_paper_with_fts(parsed_text: str, *, dir_name: str, title: str) -> int:
    """插入论文并同步 papers_fts 行（search_library 回退路径依赖 FTS 索引）。"""
    from backend.db.repo import update_fts
    pid = _insert_paper(dir_name=dir_name)
    conn = get_conn()
    try:
        cur = conn.execute("UPDATE papers SET title = ? WHERE id = ?", (title, pid))
        assert cur.rowcount == 1
        update_fts(conn, pid, title=title, abstract="", parsed_text=parsed_text)
    finally:
        conn.close()
    return pid


def test_search_library_fulltext_fallback(temp_db):
    """q 只出现在正文（parsed_text）时自动回退 FTS，matched_by 标记来源。"""
    from backend.mcp_server import search_library
    pid = _insert_paper_with_fts(
        "We introduce the Neuroscroll probe for brain-wide recordings.",
        dir_name="mcp_fts", title="High-density neural probe",
    )

    r = _j(search_library(q="Neuroscroll"))
    assert r["count"] == 1
    assert r["matched_by"] == "fulltext"
    assert r["items"][0]["id"] == pid


def test_search_library_fallback_no_hit(temp_db):
    """q 元数据与正文都无命中：count=0，不产生误报。"""
    from backend.mcp_server import search_library
    _insert_paper_with_fts("unrelated body text", dir_name="mcp_fts2", title="Some Title")

    r = _j(search_library(q="Neuroscroll"))
    assert r["count"] == 0
    assert r["matched_by"] == "metadata"


def test_search_library_fulltext_param_disables_fallback(temp_db):
    """fulltext 显式提供时不回退：q 与 fulltext 是 AND 关系，q 无命中即 0。"""
    from backend.mcp_server import search_library
    _insert_paper_with_fts(
        "We introduce the Neuroscroll probe.", dir_name="mcp_fts3", title="High-density probe",
    )

    r = _j(search_library(q="zzz-no-match", fulltext="Neuroscroll"))
    assert r["count"] == 0


def test_get_paper_rich_fields(temp_db):
    """get_paper 返回 keyword/authors/abstract（abstract 超 800 字符截断）。"""
    from backend.mcp_server import get_paper
    conn = get_conn()
    try:
        cur = conn.execute(
            """INSERT INTO papers (dir_name, year, first_author, title, keyword,
                                   authors, abstract, status_parsed)
               VALUES ('mcp_rich', 2024, 'liu', 'Rich Meta Paper', 'high_density',
                       '["Yuanyuan Liu", "Xiaojie Duan"]', ?, 'ok')""",
            ("A" * 900,),
        )
        pid = cur.lastrowid
    finally:
        conn.close()

    r = _j(get_paper(pid))
    assert r["keyword"] == "high_density"
    assert r["authors"] == ["Yuanyuan Liu", "Xiaojie Duan"]
    assert r["abstract"] == "A" * 800 + "…"

    # search_library 的 items 维持精简形态（不含三新字段）
    from backend.mcp_server import search_library
    r2 = _j(search_library(q="Rich Meta"))
    assert r2["count"] == 1
    assert "abstract" not in r2["items"][0]
    assert "authors" not in r2["items"][0]


def test_read_fulltext_pagination(temp_db, tmp_path):
    from backend.mcp_server import read_fulltext
    body = "A" * 5000 + "\n" + "B" * 5000
    md = tmp_path / "full.md"
    md.write_text(body)
    pid = _insert_paper(str(md), dir_name="mcp_full")

    r1 = _j(read_fulltext(pid, offset=0, limit=4000))
    assert r1["available"] is True
    assert r1["total"] == len(body)
    assert r1["has_more"] is True
    assert r1["text"] == "A" * 4000

    r2 = _j(read_fulltext(pid, offset=len(body) - 10, limit=4000))
    assert r2["has_more"] is False
    assert len(r2["text"]) == 10


def test_read_fulltext_manual_edit_priority(temp_db):
    from backend.mcp_server import read_fulltext
    pid = _insert_paper(dir_name="mcp_ovr")
    conn = get_conn()
    conn.execute(
        "INSERT INTO translations (paper_id, chunk_index, en_text, status, source) VALUES (?, -1, 'REVIEWED VERSION', 'ok', 'manual_edit')",
        (pid,),
    )
    conn.close()
    r = _j(read_fulltext(pid))
    assert r["source"] == "manual_edit"
    assert r["text"] == "REVIEWED VERSION"


def test_read_fulltext_unavailable(temp_db):
    from backend.mcp_server import read_fulltext
    pid = _insert_paper(parsed_path=None, dir_name="mcp_nofull")
    r = _j(read_fulltext(pid))
    assert r["available"] is False
    assert "error" not in r          # 不可用不是错误


def test_read_translation_and_summary(temp_db):
    from backend.mcp_server import read_translation, get_summary
    pid = _insert_paper(dir_name="mcp_zh")

    r = _j(read_translation(pid))
    assert r["available"] is False

    conn = get_conn()
    conn.execute("INSERT INTO translations (paper_id, chunk_index, zh_text, status, source) VALUES (?, 0, '中文一', 'ok', 'auto')", (pid,))
    conn.execute("INSERT INTO summaries (paper_id, content_md, version, source) VALUES (?, '# 总结', 1, 'auto')", (pid,))
    conn.close()

    r2 = _j(read_translation(pid))
    assert r2["available"] is True
    assert r2["text"] == "中文一"

    s = _j(get_summary(pid))
    assert s["available"] is True
    assert s["summary"] == "# 总结"


def test_annotations_agent_source(temp_db):
    from backend.mcp_server import get_annotations, add_annotation
    pid = _insert_paper(dir_name="mcp_anno")

    r = _j(add_annotation(pid, quote="hippocampus", note="注意", anchor="en"))
    aid = r["id"]

    lst = _j(get_annotations(pid))
    assert lst["count"] == 1
    item = lst["items"][0]
    assert item["source"] == "agent"
    assert item["type"] == "comment"


def test_get_task(temp_db):
    from backend.mcp_server import get_task
    from backend.db import tasks_repo
    tid = tasks_repo.create_task(kind="ingest", payload={"inbox_path": "/tmp/x.pdf"})

    r = _j(get_task(tid))
    assert r["id"] == tid
    assert r["status"] == "pending"

    r2 = _j(get_task(99999))
    assert "error" in r2


def test_search_papers_validation(temp_db):
    from backend.mcp_server import search_papers
    r = _j(search_papers(source="bogus", query="x"))
    assert "error" in r


def test_get_citations(temp_db):
    from backend.mcp_server import get_citations
    pid = _insert_paper(dir_name="mcp_cite_src")
    conn = get_conn()
    conn.execute(
        "INSERT INTO citations (src_paper_id, dst_openalex_id, dst_title, dst_year) VALUES (?, 'W123', 'Cited Work', 2020)",
        (pid,),
    )
    conn.close()

    r = _j(get_citations(pid, direction="refs"))
    assert r["count"] == 1
    assert r["items"][0]["dst_title"] == "Cited Work"

    r2 = _j(get_citations(pid, direction="cited_by"))
    assert r2["count"] == 0


def test_get_citations_invalid_direction(temp_db):
    from backend.mcp_server import get_citations
    r = _j(get_citations(1, direction="bogus"))
    assert "error" in r


def test_add_annotation_paper_not_found(temp_db):
    """不存在的 paper_id：返回 error JSON 而非抛异常（conn 开了 foreign_keys，
    无校验时 INSERT 会直接 IntegrityError）。"""
    from backend.mcp_server import add_annotation
    r = _j(add_annotation(99999, quote="x"))
    assert "error" in r


def test_add_annotation_invalid_anchor(temp_db):
    from backend.mcp_server import add_annotation
    pid = _insert_paper(dir_name="mcp_anno_anchor")
    r = _j(add_annotation(pid, quote="x", anchor="bogus"))
    assert "error" in r


def test_ingest_by_url_rejects_fake_pdf(temp_db, tmp_path, monkeypatch):
    """下载内容不是真 PDF（>1KB 的 HTML）：拒绝入库、清理文件、不建 task。"""
    from backend import config as cfg
    from backend.mcp_server import ingest_by_url

    monkeypatch.setitem(cfg.CONFIG["paths"], "inbox_dir", str(tmp_path / "inbox"))

    class _FakeResp:
        status_code = 200
        headers: dict = {}

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def raise_for_status(self):
            pass

        def iter_content(self, _size):
            yield b"<html>this is not a pdf</html>" * 200

    # 8.8.8.8 是公网 IP 字面量：_validate_url 无需 DNS 即通过，测试不依赖网络
    monkeypatch.setattr("requests.get", lambda *a, **kw: _FakeResp())
    r = _j(ingest_by_url("https://8.8.8.8/paper.pdf"))
    assert "error" in r
    assert "not a real PDF" in r["error"]

    inbox = tmp_path / "inbox"
    assert list(inbox.glob("*.pdf")) == []
    conn = get_conn()
    try:
        n = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    finally:
        conn.close()
    assert n == 0


def test_ingest_local_file_creates_pipeline_task(temp_db, tmp_path, monkeypatch):
    """本地 PDF 复制进 inbox 并创建 pipeline task，默认继续解析/富化/嵌入/总结。"""
    from backend import config as cfg
    from backend.mcp_server import ingest_local_file

    monkeypatch.setitem(cfg.CONFIG["paths"], "inbox_dir", str(tmp_path / "inbox"))
    src = tmp_path / "paper.pdf"
    src.write_bytes(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n" + b"x" * 2000)

    r = _j(ingest_local_file(str(src)))
    assert "error" not in r
    assert r["size"] == src.stat().st_size
    assert r["steps"] == ["ingest", "parse", "enrich", "embed", "summary"]
    target = Path(r["path"])
    assert target.parent == tmp_path / "inbox"
    assert target.read_bytes() == src.read_bytes()

    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT kind, payload_json FROM tasks WHERE id = ?", (r["task_id"],)
        ).fetchone()
    finally:
        conn.close()
    assert row["kind"] == "pipeline"
    payload = json.loads(row["payload_json"])
    assert payload["inbox_path"] == str(target)
    assert payload["steps"] == ["ingest", "parse", "enrich", "embed", "summary"]


def test_ingest_local_file_custom_steps(temp_db, tmp_path, monkeypatch):
    """steps 可收窄为只入库 + 解析，payload 原样传给 pipeline worker。"""
    from backend import config as cfg
    from backend.mcp_server import ingest_local_file

    monkeypatch.setitem(cfg.CONFIG["paths"], "inbox_dir", str(tmp_path / "inbox"))
    src = tmp_path / "paper.pdf"
    src.write_bytes(b"%PDF-1.7\n" + b"x" * 2000)

    r = _j(ingest_local_file(str(src), steps=["ingest", "parse"]))
    assert "error" not in r
    assert r["steps"] == ["ingest", "parse"]

    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT payload_json FROM tasks WHERE id = ?", (r["task_id"],)
        ).fetchone()
    finally:
        conn.close()
    assert json.loads(row["payload_json"])["steps"] == ["ingest", "parse"]


def test_ingest_local_file_rejects_steps_without_ingest(temp_db, tmp_path):
    """feed 模式要求首步为 ingest，否则 pipeline worker 无法拿到 paper_id。"""
    from backend.mcp_server import ingest_local_file
    r = _j(ingest_local_file(str(tmp_path / "paper.pdf"), steps=["parse"]))
    assert "must start with 'ingest'" in r["error"]


def test_ingest_local_file_rejects_non_pdf(temp_db, tmp_path, monkeypatch):
    """扩展名 .pdf 但文件头不是 %PDF-：拒绝且不建 task（不入库垃圾文件）。"""
    from backend import config as cfg
    from backend.mcp_server import ingest_local_file

    monkeypatch.setitem(cfg.CONFIG["paths"], "inbox_dir", str(tmp_path / "inbox"))
    src = tmp_path / "fake.pdf"
    src.write_bytes(b"<html>this is not a pdf</html>" * 100)

    r = _j(ingest_local_file(str(src)))
    assert "not a real PDF" in r["error"]

    conn = get_conn()
    try:
        n = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    finally:
        conn.close()
    assert n == 0


def test_ingest_local_file_rejects_url(temp_db):
    """误把 URL 传给本地入口：明确报错并指向 ingest_by_url。"""
    from backend.mcp_server import ingest_local_file
    r = _j(ingest_local_file("https://example.com/paper.pdf"))
    assert "ingest_by_url" in r["error"]


def _insert_paper_with_doi(doi: str, *, dir_name: str, title: str) -> int:
    conn = get_conn()
    try:
        cur = conn.execute(
            """INSERT INTO papers (dir_name, year, first_author, title, doi, keyword, authors)
               VALUES (?, 2023, 'example', ?, ?, 'probe', '["Example Author"]')""",
            (dir_name, title, doi),
        )
        return cur.lastrowid
    finally:
        conn.close()


def test_get_paper_by_doi_ok(temp_db):
    from backend.mcp_server import get_paper_by_doi
    pid = _insert_paper_with_doi("10.1234/example.2015.06", dir_name="mcp_doi_ok", title="ExampleGrid")
    r = _j(get_paper_by_doi("10.1234/example.2015.06"))
    assert r["count"] == 1
    assert r["items"][0]["id"] == pid
    assert r["items"][0]["keyword"] == "probe"     # rich 形态（同 get_paper）
    assert r["items"][0]["authors"] == ["Example Author"]
    assert "error" not in r


def test_get_paper_by_doi_not_in_library(temp_db):
    """不在库是正常结果：count=0 且无 error 键（agent 可转 ingest_by_url）。"""
    from backend.mcp_server import get_paper_by_doi
    r = _j(get_paper_by_doi("10.9999/not.in.library"))
    assert r["count"] == 0
    assert r["items"] == []
    assert "error" not in r


def test_get_paper_by_doi_duplicates(temp_db):
    """同一 DOI 重复入库：全部返回，按 id 升序，凭 title 区分。"""
    from backend.mcp_server import get_paper_by_doi
    pid1 = _insert_paper_with_doi(
        "10.1234/example.2015.06", dir_name="mcp_doi_dup1", title="Example Probe 2.0")
    pid2 = _insert_paper_with_doi(
        "10.1234/example.2015.06", dir_name="mcp_doi_dup2", title="Example Probe 2.0 copy")
    r = _j(get_paper_by_doi("10.1234/example.2015.06"))
    assert r["count"] == 2
    assert [it["id"] for it in r["items"]] == [pid1, pid2]


def test_get_paper_by_doi_prefix_and_case(temp_db):
    """前缀（https://doi.org/）+ 与库内大小写不同的输入命中。"""
    from backend.mcp_server import get_paper_by_doi
    pid = _insert_paper_with_doi(
        "10.1234/EXAMPLE.2013.17", dir_name="mcp_doi_case", title="Spiking Example Neurons")
    r = _j(get_paper_by_doi("https://doi.org/10.1234/example.2013.17"))
    assert r["count"] == 1
    assert r["items"][0]["id"] == pid


def test_search_library_doi_no_fts_mismatch(temp_db):
    """回归（2026-09-05 实测错配）：论文 A 的 doi=X、论文 B 正文引用 X。
    q=X 必须元数据命中 A（matched_by=metadata），不得 FTS 回退错配到 B。"""
    from backend.mcp_server import search_library
    pid_a = _insert_paper_with_doi(
        "10.1234/example.1994.18", dir_name="mcp_doi_a", title="Example correlated discharge")
    _insert_paper_with_fts(
        "References cite https://doi.org/10.1234/example.1994.18 extensively.",
        dir_name="mcp_doi_b", title="Unrelated citing paper",
    )
    r = _j(search_library(q="10.1234/example.1994.18"))
    assert r["matched_by"] == "metadata"
    assert [it["id"] for it in r["items"]] == [pid_a]


# ── 3.1.1：检索降噪 / bm25 排序 / 组合零结果诊断 / toc / 参数透传 ──────────


def test_semantic_search_tool_passthrough(temp_db, monkeypatch):
    """MCP 工具层透传 paper_id 与 include_context（钳制 [1, 6000]）。"""
    from backend.mcp_server import semantic_search
    captured = {}

    def fake_ss(query, topk=5, paper_id=None, include_context=200):
        captured.update(query=query, topk=topk, paper_id=paper_id,
                        include_context=include_context)
        return {"count": 0, "items": []}

    monkeypatch.setattr("backend.services.semantic_search.semantic_search", fake_ss)

    _j(semantic_search("locate bin width", topk=3, paper_id=75, include_context=1500))
    assert captured == {"query": "locate bin width", "topk": 3,
                        "paper_id": 75, "include_context": 1500}

    _j(semantic_search("q2", include_context=99999))
    assert captured["include_context"] == 6000
    _j(semantic_search("q3", include_context=-5))
    assert captured["include_context"] == 1
    assert captured["paper_id"] is None


def test_search_library_excludes_held_and_dup(temp_db):
    """浏览模式：manual_hold 论文与同标题重复被过滤，excluded 计数透明。"""
    from backend.mcp_server import search_library
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO papers (dir_name, year, first_author, title, status_summary)"
            " VALUES ('mcp_dup_a', 2020, 'a', 'Duplicate Title Paper', 'ok')")
        pid_a = cur.lastrowid
        conn.execute(
            "INSERT INTO papers (dir_name, year, first_author, title, status_summary)"
            " VALUES ('mcp_dup_b', 2019, 'b', 'Duplicate  Title Paper', 'ok')")
        conn.execute(
            "INSERT INTO papers (dir_name, year, first_author, title, status_summary)"
            " VALUES ('mcp_held', 2018, 'c', 'Held Title Paper', 'manual_hold')")
    finally:
        conn.close()

    r = _j(search_library())
    assert r["count"] == 1
    assert r["items"][0]["id"] == pid_a
    assert r["excluded"] == {"held": 1, "dup_title": 1}


def test_search_library_q_path_also_filters(temp_db):
    """q 元数据命中路径同样过滤 manual_hold。"""
    from backend.mcp_server import search_library
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO papers (dir_name, year, first_author, title, status_summary)"
            " VALUES ('mcp_q_ok', 2020, 'a', 'Unique Alpha Paper', 'ok')")
        pid_ok = cur.lastrowid
        conn.execute(
            "INSERT INTO papers (dir_name, year, first_author, title, status_summary)"
            " VALUES ('mcp_q_held', 2019, 'b', 'Unique Beta Paper', 'manual_hold')")
    finally:
        conn.close()

    r = _j(search_library(q="Unique"))
    assert [it["id"] for it in r["items"]] == [pid_ok]
    assert r["excluded"] == {"held": 1, "dup_title": 0}


def test_search_library_dup_prefers_published_version(temp_db):
    """同标题双版本（preprint/正式版）并存：检索返回正式版（即便预印本年份更新
    排序在前）。正式版 vs 预印本场景。"""
    from backend.mcp_server import search_library
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO papers (dir_name, year, first_author, title, journal, doi)"
            " VALUES ('mcp_pre', 2025, 'a', 'Dual Version Paper',"
            " 'bioRxiv (Cold Spring Harbor Laboratory)', '10.1101/2023.02.01.000001')")
        cur = conn.execute(
            "INSERT INTO papers (dir_name, year, first_author, title, journal, doi)"
            " VALUES ('mcp_pub', 2023, 'b', 'Dual Version Paper',"
            " 'Nature Neuroscience', '10.1234/example.2025.09')")
        pid_pub = cur.lastrowid
    finally:
        conn.close()

    r = _j(search_library(q="Dual Version"))
    assert r["count"] == 1
    assert r["items"][0]["id"] == pid_pub
    assert r["items"][0]["journal"] == "Nature Neuroscience"
    assert r["excluded"] == {"held": 0, "dup_title": 1}


def test_search_library_zero_combo_diagnostics(temp_db):
    """q+fulltext 组合 0 命中时返回各条件单独命中数（判断哪个条件为空）。"""
    from backend.mcp_server import search_library
    _insert_paper_with_fts("body words only", dir_name="mcp_zc", title="Zero Combo Paper")

    r = _j(search_library(q="Zero Combo", fulltext="zzzabsent"))
    assert r["count"] == 0
    assert r["q_matches"] == 1
    assert r["fulltext_matches"] == 0

    # AND 命中路径不带诊断字段
    r2 = _j(search_library(q="Zero Combo", fulltext="body"))
    assert r2["count"] == 1
    assert "q_matches" not in r2


def test_search_library_fts_rank_order(temp_db):
    """fulltext 命中按 bm25 相关度排序（此前按 year DESC 把高相关旧文埋后）。"""
    from backend.mcp_server import search_library
    pid_dense = _insert_paper_with_fts(
        " ".join(["quantum"] * 20), dir_name="mcp_fts_dense", title="Dense Old Match")
    conn = get_conn()
    try:
        conn.execute("UPDATE papers SET year = 2020 WHERE id = ?", (pid_dense,))
    finally:
        conn.close()
    pid_sparse = _insert_paper_with_fts(
        "quantum " + " ".join(["fillerword"] * 50),
        dir_name="mcp_fts_sparse", title="Sparse New Match")
    conn = get_conn()
    try:
        conn.execute("UPDATE papers SET year = 2025 WHERE id = ?", (pid_sparse,))
    finally:
        conn.close()

    r = _j(search_library(fulltext="quantum"))
    assert r["count"] == 2
    assert r["items"][0]["id"] == pid_dense   # 词频高者先出，不按年份


def test_read_fulltext_returns_toc(temp_db, tmp_path):
    """read_fulltext 返回章节目录（标题 + 可跳转的字符偏移）。"""
    from backend.mcp_server import read_fulltext
    body = "# Intro\npara\n\n## Methods\nwe binned spikes\n"
    md = tmp_path / "full_toc.md"
    md.write_text(body)
    pid = _insert_paper(str(md), dir_name="mcp_toc")

    r = _j(read_fulltext(pid, offset=0, limit=10))
    assert r["available"] is True
    toc = r["toc"]
    assert [(e["level"], e["title"]) for e in toc] == [(1, "Intro"), (2, "Methods")]
    assert body[toc[1]["offset"]:].startswith("## Methods")

    # 翻到第二页不再重复携带目录（2026-10-17：翻页响应去 toc 省 token）
    r2 = _j(read_fulltext(pid, offset=10, limit=100))
    assert "toc" not in r2


def test_read_fulltext_large_limit(temp_db, tmp_path):
    """单次 limit 上限 50K（2026-10-17 由 20K 上调：实测整篇阅读需翻页 5-13 次）。"""
    from backend.mcp_server import read_fulltext
    body = "# T\n" + "A" * 60000
    md = tmp_path / "full_big.md"
    md.write_text(body)
    pid = _insert_paper(str(md), dir_name="mcp_big")

    r = _j(read_fulltext(pid, offset=0, limit=50000))
    assert len(r["text"]) == 50000
    assert r["has_more"] is True
    r2 = _j(read_fulltext(pid, offset=50000, limit=50000))
    assert len(r2["text"]) == len(body) - 50000
    assert r2["has_more"] is False


def test_search_library_tokenized_q(temp_db):
    """q 多词逐词 AND：隔词标题也能命中（旧整段连续 LIKE 必然漏配）。"""
    from backend.mcp_server import search_library
    _insert_paper_with_fts(
        "irrelevant body", dir_name="mcp_gap",
        title="Synergistic Coding of Visual Information in Columnar Organization")

    r = _j(search_library(q="synergistic coding columnar"))
    assert r["count"] == 1
    assert r["matched_by"] == "metadata"

    # 任一词不命中则整体不命中（元数据与 FTS 回退都没有）
    r2 = _j(search_library(q="synergistic coding absentword"))
    assert r2["count"] == 0


def test_search_library_fulltext_long_query_or_fallback(temp_db):
    """fulltext 超过 3 词且 AND 落空时降级 OR + bm25（8 词查询旧行为必然为 0）。"""
    from backend.mcp_server import search_library
    pid = _insert_paper_with_fts(
        "The GRAB sensor enables dopamine imaging in vivo.", dir_name="mcp_or",
        title="Unrelated Title Here")

    long_q = "GRAB sensor GPCR activation based dopamine serotonin imaging"
    r = _j(search_library(fulltext=long_q))
    assert r["count"] == 1
    assert r["items"][0]["id"] == pid

    # 短查询不做 OR 降级：AND 落空即 0
    r2 = _j(search_library(fulltext="GRAB absentword"))
    assert r2["count"] == 0


def test_search_papers_doi_routes_to_exact_lookup(temp_db, monkeypatch):
    """query 为裸 DOI 时走 by_doi 精确解析（S2 关键词接口不解析裸 DOI）。"""
    from backend import mcp_server
    calls: dict = {}

    class _FakeSS:
        def search(self, query, *, limit=20, year=None):
            calls["search"] = query
            return []

        def by_doi(self, doi):
            calls["by_doi"] = doi
            return {"source": "semantic_scholar", "title": "Kao NMI", "doi": doi,
                    "authors": [], "year": 2025}

    monkeypatch.setattr("backend.external.semantic_scholar.SemanticScholarClient", _FakeSS)
    r = _j(mcp_server.search_papers("semantic_scholar", "10.1234/example.2025.20"))
    assert calls.get("by_doi") == "10.1234/example.2025.20"
    assert "search" not in calls
    assert r["count"] == 1
    assert r["items"][0]["title"] == "Kao NMI"

    # 带 doi.org 前缀同样识别
    r2 = _j(mcp_server.search_papers(
        "semantic_scholar", "https://doi.org/10.1234/example.2025.20"))
    assert r2["count"] == 1
    assert calls["by_doi"] == "10.1234/example.2025.20"


def test_search_papers_annotates_in_library(temp_db, monkeypatch):
    """外部检索结果按 DOI 比对本地库：命中条目带 in_library paper_id 列表。"""
    from backend import mcp_server
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO papers (dir_name, year, first_author, title, doi)"
            " VALUES ('mcp_inlib', 2024, 'a', 'In Lib Paper', '10.1234/example.2014.21')")
        pid = cur.lastrowid
    finally:
        conn.close()

    class _FakeSS:
        def search(self, query, *, limit=20, year=None):
            return [
                {"source": "semantic_scholar", "title": "In Lib Paper",
                 "doi": "10.1234/example.2014.21", "authors": [], "year": 2024},
                {"source": "semantic_scholar", "title": "Not In Lib",
                 "doi": "10.1038/somewhereelse", "authors": [], "year": 2023},
                {"source": "semantic_scholar", "title": "No Doi", "doi": None,
                 "authors": [], "year": 2022},
            ]

    monkeypatch.setattr("backend.external.semantic_scholar.SemanticScholarClient", _FakeSS)
    r = _j(mcp_server.search_papers("semantic_scholar", "anything"))
    assert r["items"][0]["in_library"] == [pid]
    assert "in_library" not in r["items"][1]
    assert "in_library" not in r["items"][2]
