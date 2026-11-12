"""Test: SummaryWorker 异常路径状态落 error（不卡 processing）。"""
import pytest

from backend.db.conn import get_conn


class _Ctx:
    def __init__(self):
        self.task_id, self.paper_id = 0, None
        self.infos: list[str] = []
        self.warns: list[str] = []

    def info(self, m): self.infos.append(m)
    def warn(self, m): self.warns.append(m)
    def error(self, m): pass
    def progress(self, v): pass


def _insert_paper(parsed_path: str, *, dir_name="sum_err") -> int:
    conn = get_conn()
    try:
        cur = conn.execute(
            """INSERT INTO papers (dir_name, year, first_author, title, parsed_path, status_parsed)
               VALUES (?, 2023, 'test', 'T', ?, 'ok')""",
            (dir_name, parsed_path),
        )
        return cur.lastrowid
    finally:
        conn.close()


def test_summary_worker_client_failure_sets_error(temp_db, monkeypatch, tmp_path):
    """LLMClient 构造抛异常 -> status_summary=error。"""
    from backend.external import llm as v_mod
    from backend.workers.summary_worker import summary_worker

    md = tmp_path / "full.md"
    md.write_text("# T\n\nbody\n", encoding="utf-8")
    pid = _insert_paper(str(md))

    def _boom(self):
        raise RuntimeError("no api key")

    monkeypatch.setattr(v_mod.LLMClient, "__init__", _boom)

    with pytest.raises(RuntimeError):
        summary_worker(_Ctx(), {"paper_id": pid})

    conn = get_conn()
    try:
        st = conn.execute("SELECT status_summary FROM papers WHERE id = ?", (pid,)).fetchone()
    finally:
        conn.close()
    assert st["status_summary"] == "error"


def test_summary_worker_chat_failure_sets_error(temp_db, monkeypatch, tmp_path):
    """chat() 抛异常 -> status_summary=error。"""
    from backend.external import llm as v_mod
    from backend.workers.summary_worker import summary_worker

    md = tmp_path / "full.md"
    md.write_text("# T\n\nbody\n", encoding="utf-8")
    pid = _insert_paper(str(md), dir_name="sum_err2")

    monkeypatch.setattr(v_mod.LLMClient, "__init__", lambda self: None)

    def _boom_chat(self, system_prompt, user_msg, timeout=180):
        raise RuntimeError("llm 5xx")

    monkeypatch.setattr(v_mod.LLMClient, "chat", _boom_chat)

    with pytest.raises(RuntimeError):
        summary_worker(_Ctx(), {"paper_id": pid})

    conn = get_conn()
    try:
        st = conn.execute("SELECT status_summary FROM papers WHERE id = ?", (pid,)).fetchone()
    finally:
        conn.close()
    assert st["status_summary"] == "error"


def test_summary_worker_uses_en_override_when_present(temp_db, monkeypatch, tmp_path):
    """存在 EN 覆盖层（chunk_index=-1）时，总结输入取覆盖层而非 parsed_path。"""
    from backend.external import llm as v_mod
    from backend.workers.summary_worker import summary_worker

    md = tmp_path / "full.md"
    md.write_text("# T\n\nPARSED_BODY\n", encoding="utf-8")
    pid = _insert_paper(str(md), dir_name="sum_ovr")
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO translations (paper_id, chunk_index, en_text, zh_text, status, source)"
            " VALUES (?, -1, 'REVIEWED_BODY', NULL, 'ok', 'manual_edit')",
            (pid,),
        )
    finally:
        conn.close()

    captured = {}

    monkeypatch.setattr(v_mod.LLMClient, "__init__", lambda self: None)

    def _fake_chat(self, system_prompt, user_msg, timeout=180):
        captured["user_msg"] = user_msg
        return "# 总结"

    monkeypatch.setattr(v_mod.LLMClient, "chat", _fake_chat)

    summary_worker(_Ctx(), {"paper_id": pid})

    assert "REVIEWED_BODY" in captured["user_msg"]
    assert "PARSED_BODY" not in captured["user_msg"]


# ── 2026-10-17 输入处理：SI 跳过 / 按内容剥参考文献 / 边界截断 ──


def _insert_paper_extra(*, dir_name, attachment_kind=None, parsed_path=None) -> int:
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO papers (dir_name, year, first_author, title, parsed_path,"
            " status_parsed, attachment_kind) VALUES (?, 2023, 'test', 'T', ?, 'ok', ?)",
            (dir_name, parsed_path, attachment_kind),
        )
        return cur.lastrowid
    finally:
        conn.close()


def _patch_llm_capture(monkeypatch, captured: dict) -> None:
    from backend.external import llm as v_mod

    monkeypatch.setattr(v_mod.LLMClient, "__init__", lambda self: None)

    def _fake_chat(self, system_prompt, user_msg, timeout=180):
        captured["user_msg"] = user_msg
        return "# 总结"

    monkeypatch.setattr(v_mod.LLMClient, "chat", _fake_chat)


def test_summary_skipped_for_si_attachment(temp_db, monkeypatch, tmp_path):
    """SI 附件不生成总结：标 skipped、不调 LLM、不写 summaries 行。"""
    from backend.workers.summary_worker import summary_worker
    md = tmp_path / "full.md"
    md.write_text("# T\n\nbody\n", encoding="utf-8")
    pid = _insert_paper_extra(dir_name="sum_si_skip", attachment_kind="si",
                              parsed_path=str(md))

    called = []
    from backend.external import llm as v_mod
    monkeypatch.setattr(v_mod.LLMClient, "__init__", lambda self: None)
    monkeypatch.setattr(v_mod.LLMClient, "chat",
                        lambda self, *a, **k: called.append(1) or "# x")

    ctx = _Ctx()
    summary_worker(ctx, {"paper_id": pid})   # 不抛异常

    conn = get_conn()
    try:
        st = conn.execute("SELECT status_summary FROM papers WHERE id = ?", (pid,)).fetchone()
        n = conn.execute("SELECT COUNT(*) AS c FROM summaries WHERE paper_id = ?", (pid,)).fetchone()
    finally:
        conn.close()
    assert st["status_summary"] == "skipped"
    assert not called                    # LLM 未被调用
    assert n["c"] == 0                   # summaries 表无新增
    assert any("skipped" in m for m in ctx.infos)


def test_summary_strips_references_by_heading(temp_db, monkeypatch, tmp_path):
    """有显式 References 标题时，参考文献段不进入总结输入；附录留在输入里。"""
    from backend.workers.summary_worker import summary_worker
    md = tmp_path / "full.md"
    md.write_text(
        "# Title\n\nMain body text here.\n\n## References\n\n[1] Ref A.\n[2] Ref B.",
        encoding="utf-8")
    pid = _insert_paper(str(md), dir_name="sum_strip_refs")

    captured = {}
    _patch_llm_capture(monkeypatch, captured)
    summary_worker(_Ctx(), {"paper_id": pid})

    assert "Main body text here." in captured["user_msg"]
    assert "[1] Ref A." not in captured["user_msg"]


def test_summary_keeps_headingless_numbered_body(temp_db, monkeypatch, tmp_path):
    """无 References 标题时保留全文——编号列表回退正则会误截肢正文，宁可带文献。"""
    from backend.workers.summary_worker import summary_worker
    md = tmp_path / "full.md"
    md.write_text("# Title\n\nWe list:\n\n1. Kelso JS. Dynamic Patterns.\n2. Other.\n",
                  encoding="utf-8")
    pid = _insert_paper(str(md), dir_name="sum_no_heading")

    captured = {}
    _patch_llm_capture(monkeypatch, captured)
    ctx = _Ctx()
    summary_worker(ctx, {"paper_id": pid})

    assert "1. Kelso JS." in captured["user_msg"]
    assert not any("references stripped" in m for m in ctx.infos)


def test_summary_truncates_at_paragraph_boundary(temp_db, monkeypatch, tmp_path):
    """超长输入在段落边界（\\n\\n）截断，不切断半句；短行边界退化顺序不变。"""
    from backend.workers.summary_worker import summary_worker
    paras = [f"PARAGRAPH_{i} " + "x" * 180 for i in range(8)]
    md = tmp_path / "full.md"
    md.write_text("# Title\n\n" + "\n\n".join(paras), encoding="utf-8")
    pid = _insert_paper(str(md), dir_name="sum_trunc_boundary")

    captured = {}
    _patch_llm_capture(monkeypatch, captured)
    ctx = _Ctx()
    summary_worker(ctx, {"paper_id": pid, "max_input_chars": 500})

    original_part = captured["user_msg"].split("请基于以下原文生成总结：\n\n", 1)[1]
    assert len(original_part) <= 500
    assert original_part.endswith("x")            # 停在完整段落的尾部
    assert "PARAGRAPH_" not in original_part[-12:]  # 没有半个词被切断
    assert any("boundary" in m for m in ctx.warns)


def test_summary_default_limit_comes_from_llm_config(temp_db, monkeypatch, tmp_path):
    """payload 未给 max_input_chars 时按 config.toml [llm].max_input_chars 截断——
    2026-10-17 实测全库正文 p50=6.9 万字符，旧硬编码 30000 对 91% 论文过紧。"""
    from backend.config import CONFIG
    from backend.workers.summary_worker import summary_worker
    cfg_limit = int(CONFIG["llm"]["max_input_chars"])
    assert cfg_limit > 30000

    paras = [f"P{i} " + "y" * 200 for i in range(60)]   # ~12k+ chars，超 30000 需更多
    body = "\n\n".join(paras)
    body = (body * (40000 // len(body) + 1))[:40000]    # 约 4 万字符 < 配置上限
    md = tmp_path / "full.md"
    md.write_text("# T\n\n" + body, encoding="utf-8")
    pid = _insert_paper(str(md), dir_name="sum_cfg_limit")

    captured = {}
    _patch_llm_capture(monkeypatch, captured)
    ctx = _Ctx()
    summary_worker(ctx, {"paper_id": pid})   # 无 payload 覆盖

    # 4 万字符 < 配置上限（15 万）-> 不触发截断 warn；若仍硬编码 30000 则会 warn
    assert not ctx.warns
    assert "P0 " in captured["user_msg"]
