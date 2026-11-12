"""集成测试：自建公示层（reader API）全链路。

覆盖：
  - GET doc：en 覆盖层优先 / fallback parsed；zh 覆盖层 / chunk 拼接
  - PUT doc：写 chunk_index=-1 覆盖层（en/zh 互不覆盖）；修订史记录
  - 翻译消费：translate_orchestrator 使用 EN 覆盖层（复现原 test_review_translate_flow 的核心断言）
  - revisions：列表 / 详情含内容 / 回滚
  - annotations：创建 / 列表 / 删除
"""
import json
from pathlib import Path

import pytest

from backend.db.conn import get_conn


@pytest.fixture
def client(temp_db):
    from backend.app import create_app
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


def _insert_paper(parsed_path: str | None = None, *, dir_name="r3_test") -> int:
    conn = get_conn()
    try:
        cur = conn.execute(
            """INSERT INTO papers (dir_name, year, first_author, title, parsed_path, status_parsed)
               VALUES (?, ?, ?, ?, ?, 'ok')""",
            (dir_name, 2023, "test", "Hippocampus Study", parsed_path),
        )
        return cur.lastrowid
    finally:
        conn.close()


def _write_parsed(tmp_path: Path, body: str) -> Path:
    parsed = tmp_path / "parsed"
    parsed.mkdir(exist_ok=True)
    md = parsed / "full.md"
    md.write_text(body, encoding="utf-8")
    return md


def _db_row(paper_id: int, sql: str) -> dict | None:
    conn = get_conn()
    try:
        return dict(conn.execute(sql, (paper_id,)).fetchone() or {})
    finally:
        conn.close()


# ── GET doc ──────────────────────────────────────────────


def test_doc_en_fallback_parsed(client, tmp_path):
    md = _write_parsed(tmp_path, "# Parsed\n\nORIGINAL_BODY.\n")
    pid = _insert_paper(str(md), dir_name="r3_get_en")
    r = client.get(f"/api/papers/{pid}/doc/en")
    assert r.status_code == 200
    body = r.get_json()
    assert body["available"] is True
    assert body["source"] == "parsed"
    assert "ORIGINAL_BODY" in body["text"]


def test_doc_en_prefers_override(client, tmp_path):
    md = _write_parsed(tmp_path, "# Parsed\n\nORIGINAL.\n")
    pid = _insert_paper(str(md), dir_name="r3_get_ovr")
    conn = get_conn()
    conn.execute(
        "INSERT INTO translations (paper_id, chunk_index, en_text, status, source) VALUES (?, -1, '# Reviewed\n\nEDITED.', 'ok', 'manual_edit')",
        (pid,),
    )
    conn.close()
    r = client.get(f"/api/papers/{pid}/doc/en")
    body = r.get_json()
    assert body["source"] == "manual_edit"
    assert "EDITED" in body["text"]


def test_doc_zh_chunks_concat(client, tmp_path):
    pid = _insert_paper(dir_name="r3_get_zh")
    conn = get_conn()
    conn.execute("INSERT INTO translations (paper_id, chunk_index, zh_text, status, source) VALUES (?, 0, '第一段', 'ok', 'auto')", (pid,))
    conn.execute("INSERT INTO translations (paper_id, chunk_index, zh_text, status, source) VALUES (?, 1, '第二段', 'ok', 'auto')", (pid,))
    conn.close()
    r = client.get(f"/api/papers/{pid}/doc/zh")
    body = r.get_json()
    assert "第一段" in body["text"] and "第二段" in body["text"]
    assert body["source"] == "auto"


# ── PUT doc（覆盖层 + 修订史）────────────────────────────


def test_put_doc_en_writes_override_and_revision(client, tmp_path):
    md = _write_parsed(tmp_path, "# Parsed\n\nORIGINAL.\n")
    pid = _insert_paper(str(md), dir_name="r3_put_en")
    r = client.put(f"/api/papers/{pid}/doc/en", json={"text": "# Edited\n\nHIPPOCAMPUS edit.\n"})
    assert r.status_code == 200
    body = r.get_json()
    assert body["changed"] is True
    assert body["suggest_retranslate"] is True
    assert body["revision_id"] is not None

    row = _db_row(pid, "SELECT en_text, zh_text, source FROM translations WHERE paper_id = ? AND chunk_index = -1")
    assert "HIPPOCAMPUS" in row["en_text"]
    assert row["zh_text"] is None            # EN 保存不触碰 ZH 列
    assert row["source"] == "web_edit"

    # 修订史
    r2 = client.get(f"/api/papers/{pid}/revisions?doc_type=en")
    assert r2.get_json()["count"] == 1


def test_put_doc_zh_does_not_touch_en(client, tmp_path):
    pid = _insert_paper(dir_name="r3_put_zh")
    conn = get_conn()
    conn.execute(
        "INSERT INTO translations (paper_id, chunk_index, en_text, status, source) VALUES (?, -1, 'EN_TEXT', 'ok', 'manual_edit')",
        (pid,),
    )
    conn.close()

    r = client.put(f"/api/papers/{pid}/doc/zh", json={"text": "人工编辑中文"})
    assert r.status_code == 200
    row = _db_row(pid, "SELECT en_text, zh_text FROM translations WHERE paper_id = ? AND chunk_index = -1")
    assert row["en_text"] == "EN_TEXT"       # ZH 保存不触碰 EN 列
    assert row["zh_text"] == "人工编辑中文"


def test_put_doc_unchanged_no_new_revision(client, tmp_path):
    md = _write_parsed(tmp_path, "ORIGINAL.\n")
    pid = _insert_paper(str(md), dir_name="r3_put_same")
    r1 = client.put(f"/api/papers/{pid}/doc/en", json={"text": "EDITED.\n"})
    r2 = client.put(f"/api/papers/{pid}/doc/en", json={"text": "EDITED.\n"})
    assert r1.get_json()["changed"] is True
    assert r2.get_json()["changed"] is False
    assert r2.get_json()["revision_id"] is None
    assert client.get(f"/api/papers/{pid}/revisions").get_json()["count"] == 1


def test_put_doc_summary_creates_version(client):
    pid = _insert_paper(dir_name="r3_put_sum")
    r = client.put(f"/api/papers/{pid}/doc/summary", json={"text": "# 总结 v1"})
    assert r.status_code == 200
    r2 = client.put(f"/api/papers/{pid}/doc/summary", json={"text": "# 总结 v2"})
    assert r2.status_code == 200
    row = _db_row(pid, "SELECT content_md, version, source FROM summaries WHERE paper_id = ? ORDER BY version DESC LIMIT 1")
    assert row["content_md"] == "# 总结 v2"
    assert row["version"] == 2
    assert row["source"] == "web_edit"


def test_put_doc_validation(client):
    pid = _insert_paper(dir_name="r3_put_bad")
    assert client.put(f"/api/papers/{pid}/doc/bad", json={"text": "x"}).status_code == 400
    assert client.put(f"/api/papers/{pid}/doc/en", json={}).status_code == 400
    assert client.put(f"/api/papers/99999/doc/en", json={"text": "x"}).status_code == 404


# ── EN 复核版变更 -> 自动重嵌 ──────────────────────────


def test_put_doc_en_triggers_reembed_when_embedded(client, tmp_path):
    """EN 覆盖层变更且 status_embedded=ok：置 missing + 入队 force embed（连续保存只入队一次）。"""
    md = _write_parsed(tmp_path, "ORIGINAL.\n")
    pid = _insert_paper(str(md), dir_name="r3_reembed")
    conn = get_conn()
    conn.execute("UPDATE papers SET status_embedded = 'ok' WHERE id = ?", (pid,))
    conn.close()

    r1 = client.put(f"/api/papers/{pid}/doc/en", json={"text": "EDITED v1.\n"})
    tid = r1.get_json()["reembed_task_id"]
    assert tid is not None
    row = _db_row(pid, "SELECT status_embedded FROM papers WHERE id = ?")
    assert row["status_embedded"] == "missing"     # 向量已过期标记

    conn = get_conn()
    task = dict(conn.execute(
        "SELECT kind, paper_id, payload_json, status FROM tasks WHERE id = ?", (tid,),
    ).fetchone())
    n_tasks = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE kind = 'embed' AND paper_id = ?", (pid,),
    ).fetchone()[0]
    conn.close()
    assert task["kind"] == "embed" and task["paper_id"] == pid
    assert json.loads(task["payload_json"])["force"] is True

    # 连续保存（status 已是 missing）：不再重复入队
    r2 = client.put(f"/api/papers/{pid}/doc/en", json={"text": "EDITED v2.\n"})
    assert r2.get_json()["reembed_task_id"] is None
    conn = get_conn()
    n_after = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE kind = 'embed' AND paper_id = ?", (pid,),
    ).fetchone()[0]
    conn.close()
    assert n_tasks == 1 and n_after == 1


def test_put_doc_en_no_reembed_when_not_embedded(client, tmp_path):
    """从未嵌过向量（status_embedded=missing）时 EN 变更不触发重嵌。"""
    md = _write_parsed(tmp_path, "ORIGINAL.\n")
    pid = _insert_paper(str(md), dir_name="r3_no_reembed")
    r = client.put(f"/api/papers/{pid}/doc/en", json={"text": "EDITED.\n"})
    assert r.get_json()["reembed_task_id"] is None
    conn = get_conn()
    n = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE kind = 'embed' AND paper_id = ?", (pid,),
    ).fetchone()[0]
    conn.close()
    assert n == 0


def test_put_doc_rolls_back_when_revision_fails(client, monkeypatch, tmp_path):
    """修订记录失败时，覆盖层写入一并回滚（三段写在同一事务）。"""
    md = _write_parsed(tmp_path, "ORIGINAL.\n")
    pid = _insert_paper(str(md), dir_name="r3_rollback")
    from backend.db import doc_revisions_repo

    def _boom(*a, **kw):
        raise RuntimeError("revision failed")

    monkeypatch.setattr(doc_revisions_repo, "record_revision", _boom)

    r = client.put(f"/api/papers/{pid}/doc/en", json={"text": "EDITED.\n"})
    assert r.status_code == 500
    row = _db_row(pid, "SELECT COUNT(*) AS n FROM translations WHERE paper_id = ? AND chunk_index = -1")
    assert row["n"] == 0


def test_record_revision_cleans_snapshot_on_insert_failure(temp_db, tmp_path):
    """DB 插入失败时清理刚写的快照文件，不留孤儿文件。"""
    import sqlite3 as _sq
    from backend.db import doc_revisions_repo

    class _BadConn:
        def execute(self, *a, **kw):
            raise _sq.OperationalError("boom")

    with pytest.raises(_sq.OperationalError):
        doc_revisions_repo.record_revision(1, "en", "content", "web_edit", conn=_BadConn())

    snap_dir = tmp_path / "data" / "doc_snapshots"
    assert not list(snap_dir.glob("*"))


# ── 翻译消费覆盖层（原 outline 流核心断言的复现）──────────


def test_translate_consumes_en_override(client, temp_db, monkeypatch, tmp_path):
    md = _write_parsed(tmp_path, "# T\n\nThe hippocampus is critical.\n")
    pid = _insert_paper(str(md), dir_name="r3_translate")

    # 写入 EN 复核版
    r = client.put(f"/api/papers/{pid}/doc/en", json={"text": "# T\n\nThe HIPPOCAMPUS is critical.\n"})
    assert r.status_code == 200

    # mock LLM，捕获输入
    captured: list[str] = []
    from backend.external import llm as v_mod
    monkeypatch.setattr(v_mod.LLMClient, "__init__", lambda self: None)
    monkeypatch.setattr(
        v_mod.LLMClient, "chat",
        lambda self, system_prompt, user_msg, timeout=180: captured.append(user_msg) or "[ZH] " + user_msg[:30],
    )

    from backend.workers.translate_orchestrator import translate_worker

    class _Ctx:
        task_id, paper_id = 0, pid
        messages: list[tuple[str, str]] = []
        def info(self, m): self.messages.append(("info", m))
        def warn(self, m): self.messages.append(("warn", m))
        def error(self, m): self.messages.append(("error", m))
        def progress(self, v): pass

    ctx = _Ctx()
    translate_worker(ctx, {"paper_id": pid})

    joined = " ".join(captured)
    assert "HIPPOCAMPUS" in joined, "翻译未使用 EN 复核版作为输入"
    info_msgs = " ".join(m for lvl, m in ctx.messages if lvl == "info")
    assert "using EN override" in info_msgs
    row = _db_row(pid, "SELECT status_translated FROM papers WHERE id = ?")
    assert row["status_translated"] == "ok"


# ── revisions：详情 / 回滚 ────────────────────────────────


def test_revision_detail_and_restore(client, tmp_path):
    md = _write_parsed(tmp_path, "V1\n")
    pid = _insert_paper(str(md), dir_name="r3_restore")
    r1 = client.put(f"/api/papers/{pid}/doc/en", json={"text": "V2 EDITED\n"})
    rev1 = r1.get_json()["revision_id"]

    # 再改一次
    client.put(f"/api/papers/{pid}/doc/en", json={"text": "V3 MORE\n"})

    # 详情含快照内容
    d = client.get(f"/api/revisions/{rev1}").get_json()
    assert d["content"] == "V2 EDITED\n"

    # 回滚到 rev1
    rr = client.post(f"/api/revisions/{rev1}/restore")
    assert rr.status_code == 200
    assert rr.get_json()["restored"] is True

    doc = client.get(f"/api/papers/{pid}/doc/en").get_json()
    assert doc["text"] == "V2 EDITED\n"
    # 回滚也记修订
    assert client.get(f"/api/papers/{pid}/revisions").get_json()["count"] == 3


def test_put_doc_rejects_stale_base_hash_without_overwriting(client, tmp_path):
    md = _write_parsed(tmp_path, "ORIGINAL.\n")
    pid = _insert_paper(str(md), dir_name="r3_base_hash")
    base_hash = client.get(f"/api/papers/{pid}/doc/en").get_json()["hash"]
    assert client.put(
        f"/api/papers/{pid}/doc/en", json={"text": "OTHER TAB.\n"},
    ).status_code == 200

    conflict = client.put(
        f"/api/papers/{pid}/doc/en",
        json={"text": "STALE EDIT.\n", "base_hash": base_hash},
    )
    assert conflict.status_code == 409
    assert conflict.get_json()["current_hash"] != base_hash
    current = client.get(f"/api/papers/{pid}/doc/en").get_json()
    assert current["text"] == "OTHER TAB.\n"

    overwrite = client.put(
        f"/api/papers/{pid}/doc/en",
        json={"text": "INTENTIONAL OVERWRITE.\n", "base_hash": current["hash"]},
    )
    assert overwrite.status_code == 200


def test_put_doc_accepts_null_base_hash_for_missing_document(client):
    pid = _insert_paper(dir_name="r3_empty_hash")
    response = client.put(
        f"/api/papers/{pid}/doc/en",
        json={"text": "FIRST EDIT.\n", "base_hash": None},
    )
    assert response.status_code == 200
    assert client.put(
        f"/api/papers/{pid}/doc/en",
        json={"text": "BAD HASH.\n", "base_hash": 123},
    ).status_code == 400


# ── annotations ──────────────────────────────────────────


def test_annotations_crud(client):
    pid = _insert_paper(dir_name="r3_anno")
    r = client.post(f"/api/papers/{pid}/annotations", json={
        "type": "highlight", "anchor": "en",
        "quote": "hippocampus", "note": "关键术语",
        "position": {"start": 12, "end": 23},
    })
    assert r.status_code == 201
    aid = r.get_json()["id"]

    lst = client.get(f"/api/papers/{pid}/annotations").get_json()
    assert lst["count"] == 1
    payload = json.loads(lst["items"][0]["payload_json"])
    assert payload["quote"] == "hippocampus"
    assert payload["position"] == {"start": 12, "end": 23}
    assert lst["items"][0]["source"] == "web"

    # 校验：highlight 必须有 quote
    r2 = client.post(f"/api/papers/{pid}/annotations", json={"type": "highlight", "anchor": "en"})
    assert r2.status_code == 400
    # 校验：type 枚举
    r3 = client.post(f"/api/papers/{pid}/annotations", json={"type": "weird"})
    assert r3.status_code == 400

    assert client.delete(f"/api/annotations/{aid}").status_code == 200
    assert client.get(f"/api/papers/{pid}/annotations").get_json()["count"] == 0
    assert client.delete(f"/api/annotations/{aid}").status_code == 404
