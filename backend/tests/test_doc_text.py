"""Test: services/doc_text 的覆盖层读取规则（EN/ZH/Summary 三文档类型）。"""
from pathlib import Path

from backend.db.conn import get_conn
from backend.services.doc_text import (
    read_doc_text, read_en_override, read_latest_summary,
)


def _insert_paper(parsed_path: str | None = None, *, dir_name="r2023_x") -> int:
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO papers (dir_name, year, first_author, title, parsed_path)"
            " VALUES (?, ?, ?, ?, ?)",
            (dir_name, 2023, "test", "T", parsed_path),
        )
        return cur.lastrowid
    finally:
        conn.close()


def _insert_translation(paper_id: int, chunk_index: int, *,
                        en_text: str = "", zh_text: str = "") -> None:
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO translations (paper_id, chunk_index, en_text, zh_text, status, source)"
            " VALUES (?, ?, ?, ?, 'ok', 'auto')",
            (paper_id, chunk_index, en_text, zh_text),
        )
    finally:
        conn.close()


def _insert_summary(paper_id: int, content: str, version: int, source: str = "auto") -> None:
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO summaries (paper_id, content_md, version, source) VALUES (?, ?, ?, ?)",
            (paper_id, content, version, source),
        )
    finally:
        conn.close()


def _write_parsed(tmp_path: Path, body: str) -> Path:
    parsed = tmp_path / "parsed"
    parsed.mkdir(exist_ok=True)
    md = parsed / "full.md"
    md.write_text(body, encoding="utf-8")
    return md


def _paper(pid: int, parsed_path=None) -> dict:
    return {"id": pid, "dir_name": "r2023_x", "title": "T", "parsed_path": parsed_path}


# ── EN ──────────────────────────────────────────────


def test_en_uses_parsed_when_no_override(temp_db, tmp_path):
    md = _write_parsed(tmp_path, "# Parsed\n\nFROM_PARSED.\n")
    pid = _insert_paper(parsed_path=str(md))
    text, source = read_doc_text(_paper(pid, str(md)), "en")
    assert "FROM_PARSED" in text
    assert source == "parsed"


def test_en_prefers_override(temp_db, tmp_path):
    md = _write_parsed(tmp_path, "ORIGINAL_BODY.\n")
    pid = _insert_paper(parsed_path=str(md))
    _insert_translation(pid, -1, en_text="OVERRIDE_BODY.")
    text, source = read_doc_text(_paper(pid, str(md)), "en")
    assert "OVERRIDE_BODY" in text
    assert "ORIGINAL_BODY" not in text
    assert source == "manual_edit"


def test_en_empty_override_falls_back_to_parsed(temp_db, tmp_path):
    md = _write_parsed(tmp_path, "PARSED_CONTENT.\n")
    pid = _insert_paper(parsed_path=str(md))
    _insert_translation(pid, -1, en_text="", zh_text="中文")
    text, source = read_doc_text(_paper(pid, str(md)), "en")
    assert "PARSED_CONTENT" in text
    assert source == "parsed"


def test_en_missing_parsed_no_override(temp_db):
    pid = _insert_paper(parsed_path=None)
    text, source = read_doc_text(_paper(pid, None), "en")
    assert text == ""
    assert source == "missing"


def test_read_en_override_empty_when_no_row(temp_db):
    pid = _insert_paper(parsed_path=None)
    assert read_en_override(pid) == ""


# ── ZH ──────────────────────────────────────────────


def test_zh_prefers_override_over_chunks(temp_db):
    pid = _insert_paper()
    _insert_translation(pid, 0, zh_text="自动零")
    _insert_translation(pid, 1, zh_text="自动一")
    _insert_translation(pid, -1, zh_text="人工覆盖")
    text, source = read_doc_text(_paper(pid), "zh")
    assert text == "人工覆盖"
    assert source == "manual_edit"


def test_zh_concatenates_chunks_in_order(temp_db):
    pid = _insert_paper()
    _insert_translation(pid, 1, zh_text="一")
    _insert_translation(pid, 0, zh_text="零")
    text, source = read_doc_text(_paper(pid), "zh")
    assert text == "零\n一"
    assert source == "auto"


def test_zh_missing_when_no_translations(temp_db):
    pid = _insert_paper()
    text, source = read_doc_text(_paper(pid), "zh")
    assert text == ""
    assert source == "missing"


# ── Summary ─────────────────────────────────────────


def test_summary_returns_latest_version(temp_db):
    pid = _insert_paper()
    _insert_summary(pid, "旧版", version=1)
    _insert_summary(pid, "新版", version=2, source="web_edit")
    text, source = read_doc_text(_paper(pid), "summary")
    assert text == "新版"
    assert source == "web_edit"

    latest = read_latest_summary(pid)
    assert latest is not None
    assert latest["version"] == 2
    assert latest["content_md"] == "新版"


def test_summary_missing(temp_db):
    pid = _insert_paper()
    text, source = read_doc_text(_paper(pid), "summary")
    assert text == ""
    assert source == "missing"
    assert read_latest_summary(pid) is None


def test_extract_toc_levels_offsets_and_cap():
    """extract_toc：标题层级 + 字符偏移 + 上限（3.1.1：read_fulltext 跳转锚点）。"""
    from backend.services.doc_text import extract_toc
    text = ("# Title\nintro\n\n## Methods\nwe did\n\n### Spike sorting\n"
            "details\n\n## Results\ngood\n\nplain text line\n")
    toc = extract_toc(text)
    assert [(e["level"], e["title"]) for e in toc] == [
        (1, "Title"), (2, "Methods"), (3, "Spike sorting"), (2, "Results"),
    ]
    # 偏移可直接用于切片跳转
    assert text[toc[1]["offset"]:].startswith("## Methods")
    assert text[toc[3]["offset"]:].startswith("## Results")


def test_extract_toc_edge_cases():
    from backend.services.doc_text import extract_toc
    assert extract_toc("") == []
    assert extract_toc("no headings at all\njust text\n") == []
    # 非标题行（# 后无空格、行中 #）不识别；尾部 # 闭合符剥除
    toc = extract_toc("mid # line\n#NoSpace\n## Closed ##\n")
    assert [(e["level"], e["title"]) for e in toc] == [(2, "Closed")]
    # 上限截断
    long_text = "".join(f"## Sec{i}\nbody\n" for i in range(60))
    assert len(extract_toc(long_text)) == 50
