"""Test: translate_orchestrator 优先使用 EN override；force 保留 chunk_index=-1 行。"""
from pathlib import Path

import pytest

from backend.db.conn import get_conn


class _FakeCtx:
    def __init__(self):
        self.task_id = 0
        self.paper_id = None
        self.messages: list[tuple[str, str]] = []
    def info(self, msg): self.messages.append(("info", msg))
    def warn(self, msg): self.messages.append(("warn", msg))
    def error(self, msg): self.messages.append(("error", msg))
    def progress(self, value): pass


def _insert_paper(parsed_path: str | None = None, *, dir_name="tr2023_x") -> int:
    conn = get_conn()
    try:
        cur = conn.execute(
            """INSERT INTO papers (dir_name, year, first_author, title, parsed_path, status_parsed)
               VALUES (?, ?, ?, ?, ?, 'ok')""",
            (dir_name, 2023, "test", "T", parsed_path),
        )
        return cur.lastrowid
    finally:
        conn.close()


def _write_parsed(tmp_path: Path, body: str) -> Path:
    parsed_dir = tmp_path / "parsed"
    parsed_dir.mkdir()
    md = parsed_dir / "full.md"
    md.write_text(body, encoding="utf-8")
    return md


def _insert_override(paper_id: int, *, en_text: str = "", zh_text: str = "") -> None:
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO translations (paper_id, chunk_index, en_text, zh_text, status, source)"
            " VALUES (?, -1, ?, ?, 'ok', 'manual_edit')",
            (paper_id, en_text, zh_text),
        )
    finally:
        conn.close()


def _patch_llm(monkeypatch, captured: list[str]):
    """Mock LLMClient.chat → 记录 user_msg，返回固定中文。"""
    from backend.external import llm as v_mod

    monkeypatch.setattr(v_mod.LLMClient, "__init__", lambda self: None)

    def fake_chat(self, system_prompt, user_msg, timeout=180):
        captured.append(user_msg)
        return "[中文翻译] " + user_msg[:30]

    monkeypatch.setattr(v_mod.LLMClient, "chat", fake_chat)


def _list_chunks(paper_id: int):
    conn = get_conn()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT chunk_index, en_text, zh_text, status, source FROM translations"
            " WHERE paper_id = ? ORDER BY chunk_index",
            (paper_id,),
        ).fetchall()]
    finally:
        conn.close()


# ── A6 主体 ───────────────────────────────────────────


def test_translator_reads_override_when_present(temp_db, monkeypatch, tmp_path):
    from backend.workers.translate_orchestrator import translate_worker

    md = _write_parsed(tmp_path, "# Original\n\nOriginal body content from parsed.\n")
    pid = _insert_paper(parsed_path=str(md), dir_name="tr2023_ovr")

    # EN override：完全不同的 EN 文本，包含特征词
    _insert_override(pid, en_text="# Edited\n\nThis content was REVIEWED_BY_HUMAN.\n")

    captured: list[str] = []
    _patch_llm(monkeypatch, captured)

    translate_worker(_FakeCtx(), {"paper_id": pid})

    # 至少有一次 chunk 翻译入参里含复核版特征词
    joined = " ".join(captured)
    assert "REVIEWED_BY_HUMAN" in joined
    assert "Original body content" not in joined


def test_translator_falls_back_to_parsed(temp_db, monkeypatch, tmp_path):
    from backend.workers.translate_orchestrator import translate_worker

    md = _write_parsed(tmp_path, "# Header\n\nFallback content marker XYZZY.\n")
    pid = _insert_paper(parsed_path=str(md), dir_name="tr2023_fb")

    captured: list[str] = []
    _patch_llm(monkeypatch, captured)

    translate_worker(_FakeCtx(), {"paper_id": pid})

    joined = " ".join(captured)
    assert "XYZZY" in joined


def test_translator_force_preserves_override_row(temp_db, monkeypatch, tmp_path):
    from backend.workers.translate_orchestrator import translate_worker

    md = _write_parsed(tmp_path, "# H\n\nP1.\n\n## H2\n\nP2.\n")
    pid = _insert_paper(parsed_path=str(md), dir_name="tr2023_force")
    _insert_override(pid, en_text="# Reviewed\n\nReviewed body.\n", zh_text="人工中文")

    # 预先插一些 chunk_index>=0 行作为旧自动翻译
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO translations (paper_id, chunk_index, en_text, zh_text, status, source)"
            " VALUES (?, 0, 'old en 0', 'old zh 0', 'ok', 'auto')",
            (pid,),
        )
        conn.execute(
            "INSERT INTO translations (paper_id, chunk_index, en_text, zh_text, status, source)"
            " VALUES (?, 1, 'old en 1', 'old zh 1', 'ok', 'auto')",
            (pid,),
        )
    finally:
        conn.close()

    captured: list[str] = []
    _patch_llm(monkeypatch, captured)

    translate_worker(_FakeCtx(), {"paper_id": pid, "force": True})

    rows = _list_chunks(pid)
    # chunk_index=-1 行必须仍然存在，且内容未被改
    override_row = next((r for r in rows if r["chunk_index"] == -1), None)
    assert override_row is not None
    assert override_row["en_text"] == "# Reviewed\n\nReviewed body.\n"
    assert override_row["zh_text"] == "人工中文"
    assert override_row["source"] == "manual_edit"

    # chunk_index>=0 行重新生成（不再是旧的 'old en X'）
    new_chunks = [r for r in rows if r["chunk_index"] >= 0]
    assert len(new_chunks) >= 1
    for r in new_chunks:
        assert r["en_text"] != "old en 0"
        assert r["en_text"] != "old en 1"


def test_translator_client_failure_sets_error(temp_db, monkeypatch, tmp_path):
    """LLMClient 构造抛异常 -> status_translated=error（不卡 processing）。"""
    from backend.external import llm as v_mod
    from backend.workers.translate_orchestrator import translate_worker

    md = _write_parsed(tmp_path, "# H\n\nBody.\n")
    pid = _insert_paper(parsed_path=str(md), dir_name="tr2023_err")

    def _boom(self):
        raise RuntimeError("no api key")

    monkeypatch.setattr(v_mod.LLMClient, "__init__", _boom)

    with pytest.raises(RuntimeError):
        translate_worker(_FakeCtx(), {"paper_id": pid})

    conn = get_conn()
    try:
        st = conn.execute(
            "SELECT status_translated FROM papers WHERE id = ?", (pid,)
        ).fetchone()
    finally:
        conn.close()
    assert st["status_translated"] == "error"


def test_translator_override_empty_falls_back(temp_db, monkeypatch, tmp_path):
    """override 行存在但 en_text='' → 应走 parsed_path。"""
    from backend.workers.translate_orchestrator import translate_worker

    md = _write_parsed(tmp_path, "# H\n\nFROM_PARSED_PATH content.\n")
    pid = _insert_paper(parsed_path=str(md), dir_name="tr2023_emptyovr")
    _insert_override(pid, en_text="", zh_text="只是 ZH 复核版")

    captured: list[str] = []
    _patch_llm(monkeypatch, captured)

    translate_worker(_FakeCtx(), {"paper_id": pid})

    joined = " ".join(captured)
    assert "FROM_PARSED_PATH" in joined
