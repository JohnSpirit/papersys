"""Test: EmbedWorker（mock 托管 embed）+ 两阶段语义检索 + pack/unpack。"""
import pytest

from backend.db.conn import get_conn


def _fake_vec(text: str, dim: int = 8) -> list[float]:
    """确定性伪向量：按首字符 hash 定方向，便于构造相似/不相似对。"""
    seed = sum(ord(c) for c in text[:20]) % dim
    v = [0.1] * dim
    v[seed % dim] = 1.0
    return v


@pytest.fixture(autouse=True)
def _isolate_library(tmp_path, monkeypatch):
    """把 library_root 指到 tmp_path，防止测试污染真实数据目录的 library。"""
    from backend import config as cfg
    monkeypatch.setitem(cfg.CONFIG["paths"], "library_root", str(tmp_path / "library"))
    yield


@pytest.fixture
def mock_hosted(monkeypatch):
    """mock HostedRetrievalClient.embed（__init__ 只读配置不发请求，保留真实逻辑）。"""
    from backend.external import hosted_retrieval as h_mod
    calls: list[list[str]] = []

    def fake_embed(self, texts, timeout=None):
        calls.append(list(texts))
        return [_fake_vec(t) for t in texts]

    monkeypatch.setattr(h_mod.HostedRetrievalClient, "embed", fake_embed)
    return calls


@pytest.fixture
def mock_rerank(monkeypatch):
    """mock HostedRetrievalClient.rerank，返回 (calls, 行为控制器)。"""
    from backend.external import hosted_retrieval as h_mod
    state = {"calls": [], "fn": None}

    def fake_rerank(self, query, documents, timeout=None):
        state["calls"].append((query, list(documents)))
        if state["fn"]:
            return state["fn"](query, documents)
        # 默认：原序返回，分数递减且高于阈值
        return [{"index": i, "score": 0.9 - i * 0.01} for i in range(len(documents))]

    monkeypatch.setattr(h_mod.HostedRetrievalClient, "rerank", fake_rerank)
    return state


def _insert_paper(parsed_text: str, *, dir_name="emb_t") -> int:
    from backend.config import library_root
    # 写入 library 布局以走 resolve_path 相对路径
    lib = library_root() / "2023" / dir_name
    lib.mkdir(parents=True, exist_ok=True)
    (lib / "full.md").write_text(parsed_text, encoding="utf-8")

    conn = get_conn()
    try:
        cur = conn.execute(
            """INSERT INTO papers (dir_name, year, first_author, title, parsed_path, status_parsed)
               VALUES (?, 2023, 'test', ?, ?, 'ok')""",
            (dir_name, dir_name, f"2023/{dir_name}/full.md"),
        )
        return cur.lastrowid
    finally:
        conn.close()


class _Ctx:
    def __init__(self, pid):
        self.task_id, self.paper_id = 0, pid
        self.msgs: list[tuple[str, str]] = []
    def info(self, m): self.msgs.append(("info", m))
    def warn(self, m): self.msgs.append(("warn", m))
    def error(self, m): self.msgs.append(("error", m))
    def progress(self, v): pass


def test_embed_worker_writes_vectors(temp_db, mock_hosted):
    from backend.workers.embed_worker import embed_worker
    pid = _insert_paper("# T\n\n## A\n\nalpha beta gamma\n\n## B\n\ndelta epsilon\n", dir_name="emb_w1")
    ctx = _Ctx(pid)
    embed_worker(ctx, {"paper_id": pid})

    conn = get_conn()
    try:
        rows = conn.execute("SELECT chunk_index, text, dim FROM embeddings WHERE paper_id = ? ORDER BY chunk_index", (pid,)).fetchall()
        st = conn.execute("SELECT status_embedded FROM papers WHERE id = ?", (pid,)).fetchone()
    finally:
        conn.close()
    assert len(rows) >= 1
    assert all(r["dim"] == 8 for r in rows)
    assert st["status_embedded"] == "ok"


def test_embed_worker_client_failure_sets_error(temp_db, monkeypatch):
    """HostedRetrievalClient 构造抛异常 -> status_embedded=error（不卡 processing）。"""
    from backend.external import hosted_retrieval as h_mod
    from backend.workers.embed_worker import embed_worker

    pid = _insert_paper("# T\n\nalpha beta\n", dir_name="emb_err")

    def _boom(self):
        raise RuntimeError("no api key")

    monkeypatch.setattr(h_mod.HostedRetrievalClient, "__init__", _boom)

    with pytest.raises(RuntimeError):
        embed_worker(_Ctx(pid), {"paper_id": pid})

    conn = get_conn()
    try:
        st = conn.execute("SELECT status_embedded FROM papers WHERE id = ?", (pid,)).fetchone()
    finally:
        conn.close()
    assert st["status_embedded"] == "error"


def test_embed_worker_skips_existing(temp_db, mock_hosted):
    from backend.workers.embed_worker import embed_worker
    pid = _insert_paper("# T\n\nalpha beta\n", dir_name="emb_w2")
    embed_worker(_Ctx(pid), {"paper_id": pid})
    n1 = len(mock_hosted)
    assert n1 >= 1
    # 二次跑（无 force）：跳过已有
    mock_hosted.clear()
    embed_worker(_Ctx(pid), {"paper_id": pid})
    assert len(mock_hosted) == 0


def test_embed_worker_uses_en_override(temp_db, mock_hosted):
    from backend.workers.embed_worker import embed_worker
    pid = _insert_paper("# Parsed\n\nparsed body\n", dir_name="emb_w3")
    conn = get_conn()
    conn.execute(
        "INSERT INTO translations (paper_id, chunk_index, en_text, status, source) VALUES (?, -1, '# Override\n\nOVERRIDE_BODY', 'ok', 'manual_edit')",
        (pid,),
    )
    conn.close()

    embed_worker(_Ctx(pid), {"paper_id": pid})
    assert any("OVERRIDE_BODY" in t for batch in mock_hosted for t in batch)


def test_semantic_search(temp_db, mock_hosted):
    from backend.workers.embed_worker import embed_worker
    from backend.services.semantic_search import semantic_search

    pid1 = _insert_paper("# T\n\nhippocampus memory consolidation theta rhythm\n", dir_name="emb_s1")
    pid2 = _insert_paper("# T2\n\ndeep learning transformer attention mechanism\n", dir_name="emb_s2")
    embed_worker(_Ctx(pid1), {"paper_id": pid1})
    embed_worker(_Ctx(pid2), {"paper_id": pid2})

    # 候选池（2 条）<= topk（2）-> 短路，纯余弦返回
    r = semantic_search("hippocampus memory", topk=2)
    assert r["count"] >= 1
    assert r["stage"] == "embed"
    assert r["items"][0]["paper_id"] == pid1  # 伪向量保证查询与自身 seed 命中

    # 空库错误（限定不存在论文）
    r2 = semantic_search("x", paper_id=99999)
    assert "error" in r2


def test_semantic_search_empty_query(temp_db):
    from backend.services.semantic_search import semantic_search
    assert "error" in semantic_search("")


def test_semantic_search_rerank_path(temp_db, mock_hosted, mock_rerank):
    """候选数 > topk 时走精排；结果顺序服从精排，响应带 stage=rerank + threshold。"""
    from backend.workers.embed_worker import embed_worker
    from backend.services.semantic_search import semantic_search

    pids = [
        _insert_paper(f"# T{i}\n\ntopic number {i} alpha beta\n", dir_name=f"emb_r{i}")
        for i in range(3)
    ]
    for pid in pids:
        embed_worker(_Ctx(pid), {"paper_id": pid})

    # 精排反转候选顺序
    mock_rerank["fn"] = lambda q, docs: [
        {"index": i, "score": 0.9} for i in reversed(range(len(docs)))
    ]
    r = semantic_search("alpha beta", topk=1)
    assert mock_rerank["calls"], "候选数 > topk 时必须调用精排"
    assert r["stage"] == "rerank"
    assert r["threshold"] == 0.5
    assert r["count"] == 1
    assert r["items"][0]["score"] == 0.9


def test_semantic_search_rerank_threshold_filter(temp_db, mock_hosted, mock_rerank):
    """精排分数低于阈值 -> 过滤（阈值层）。"""
    from backend.workers.embed_worker import embed_worker
    from backend.services.semantic_search import semantic_search

    for i in range(2):
        pid = _insert_paper(f"# T{i}\n\ntopic number {i} alpha beta\n", dir_name=f"emb_t{i}")
        embed_worker(_Ctx(pid), {"paper_id": pid})

    mock_rerank["fn"] = lambda q, docs: [
        {"index": i, "score": 0.1} for i in range(len(docs))  # 全低于 0.5 阈值
    ]
    r = semantic_search("alpha beta", topk=1)
    assert r["stage"] == "rerank"
    assert r["count"] == 0
    assert r["items"] == []


def test_semantic_search_rerank_fallback(temp_db, mock_hosted, mock_rerank):
    """精排抛异常 -> 降级为纯余弦召回（stage=embed_fallback），RAG 仍可用。"""
    from backend.workers.embed_worker import embed_worker
    from backend.services.semantic_search import semantic_search

    pids = [
        _insert_paper(f"# T{i}\n\ntopic number {i} alpha beta\n", dir_name=f"emb_f{i}")
        for i in range(3)
    ]
    for pid in pids:
        embed_worker(_Ctx(pid), {"paper_id": pid})

    def boom(q, docs):
        raise RuntimeError("rerank endpoint down")

    mock_rerank["fn"] = boom
    r = semantic_search("alpha beta", topk=1)
    assert r["stage"] == "embed_fallback"
    assert "rerank_error" in r
    assert r["count"] == 1


def test_semantic_search_rerank_disabled(temp_db, mock_hosted, mock_rerank, monkeypatch):
    """rerank_enabled=false -> 短路开关生效，不调用精排。"""
    from backend import config as cfg
    from backend.workers.embed_worker import embed_worker
    from backend.services.semantic_search import semantic_search

    monkeypatch.setitem(cfg.CONFIG["retrieval"], "rerank_enabled", False)

    for i in range(3):
        pid = _insert_paper(f"# T{i}\n\ntopic number {i} alpha beta\n", dir_name=f"emb_d{i}")
        embed_worker(_Ctx(pid), {"paper_id": pid})

    r = semantic_search("alpha beta", topk=1)
    assert not mock_rerank["calls"], "rerank_enabled=false 时不得调用精排"
    assert r["stage"] == "embed"
    assert r["count"] == 1


def test_pack_unpack_roundtrip():
    from backend.db.vectors import pack, unpack
    v = [0.5, -1.25, 3.0, 0.0]
    blob = pack(v)
    assert len(blob) == 16
    out = unpack(blob)
    assert all(abs(a - b) < 1e-6 for a, b in zip(v, out))
