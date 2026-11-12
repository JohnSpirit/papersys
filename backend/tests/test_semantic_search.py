"""Test: semantic_search 按论文去重 + 单篇模式不去重。

不访问网络：monkeypatch hosted_retrieval_config 与 HostedRetrievalClient，
用 db/vectors.pack 直接种入可控向量（二维，余弦值手工可算）。
"""
import pytest

from backend.db.conn import get_conn
from backend.db.vectors import pack


# 文本 -> 精排分数（假 rerank 按内容打分，不依赖候选顺序）
_RERANK_SCORES = {"alpha": 0.9, "alpha2": 0.85, "beta": 0.8, "beta2": 0.75, "gamma": 0.7}


class _FakeHostedClient:
    embed_vec = [1.0, 0.0]

    def embed(self, texts):
        return [list(self.embed_vec) for _ in texts]

    def rerank(self, query, docs):
        scored = [{"index": i, "score": _RERANK_SCORES[d]} for i, d in enumerate(docs)]
        scored.sort(key=lambda x: -x["score"])
        return scored


def _fake_hrc():
    return ({"rerank_enabled": True, "candidate_multiplier": 10,
             "rerank_threshold": 0.0, "search_max_embeddings": 200000}, None)


@pytest.fixture
def fake_retrieval(monkeypatch):
    monkeypatch.setattr("backend.services.semantic_search.hosted_retrieval_config",
                        _fake_hrc)
    monkeypatch.setattr("backend.external.hosted_retrieval.HostedRetrievalClient",
                        _FakeHostedClient)


def _insert_paper(dir_name: str) -> int:
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO papers (dir_name, year, first_author, title) VALUES (?, 2024, 'a', ?)",
            (dir_name, dir_name),
        )
        return cur.lastrowid
    finally:
        conn.close()


def _insert_chunk(paper_id: int, chunk_index: int, text: str, vec: list[float]) -> None:
    conn = get_conn()
    try:
        conn.execute(
            """INSERT INTO embeddings (paper_id, chunk_id, chunk_index, text, vector, dim, model)
               VALUES (?, ?, ?, ?, ?, ?, 'test')""",
            (paper_id, f"{paper_id}:{chunk_index}", chunk_index,
             text, pack(vec), len(vec)),
        )
    finally:
        conn.close()


def _seed_library() -> tuple[int, int, int]:
    """3 篇论文 5 个 chunk：A/B 各 2 个（验证去重），C 1 个。

    查询向量 [1,0]：A 的 chunk 余弦 1.0，B 约 0.707，C 为 0。
    """
    a = _insert_paper("ss_dedup_a")
    b = _insert_paper("ss_dedup_b")
    c = _insert_paper("ss_dedup_c")
    _insert_chunk(a, 0, "alpha", [1.0, 0.0])
    _insert_chunk(a, 1, "alpha2", [1.0, 0.0])
    _insert_chunk(b, 0, "beta", [0.70710678, 0.70710678])
    _insert_chunk(b, 1, "beta2", [0.70710678, 0.70710678])
    _insert_chunk(c, 0, "gamma", [0.0, 1.0])
    return a, b, c


def test_dedupe_by_paper_pure():
    """纯函数：按 paper_id 保序去重，保留首个（分数最高）条目。"""
    from backend.services.semantic_search import _dedupe_by_paper
    items = [
        {"paper_id": 1, "score": 0.9},
        {"paper_id": 2, "score": 0.8},
        {"paper_id": 1, "score": 0.7},
        {"paper_id": 3, "score": 0.6},
    ]
    out = _dedupe_by_paper(items)
    assert [it["paper_id"] for it in out] == [1, 2, 3]
    assert out[0]["score"] == 0.9


def test_semantic_search_dedupes_by_paper(temp_db, fake_retrieval):
    """跨库检索去重：无去重时 top-3 为 alpha/alpha2/beta（2 篇），
    去重后覆盖 3 篇且每篇保留最高分片段。"""
    from backend.services.semantic_search import semantic_search
    _seed_library()

    r = semantic_search("query", topk=3)
    assert r["stage"] == "rerank"
    assert r["count"] == 3
    pids = [it["paper_id"] for it in r["items"]]
    assert len(set(pids)) == 3
    # A 论文保留分数最高的 alpha 而非 alpha2
    assert r["items"][0]["text"] == "alpha"
    assert r["items"][0]["chunk_index"] == 0


def test_semantic_search_single_paper_keeps_chunks(temp_db, fake_retrieval):
    """单篇检索（paper_id 锁定）不去重：同论文多片段都返回。"""
    from backend.services.semantic_search import semantic_search
    a, _, _ = _seed_library()

    r = semantic_search("query", topk=5, paper_id=a)
    assert r["count"] == 2
    assert all(it["paper_id"] == a for it in r["items"])


def test_dedupe_by_title_pure():
    """纯函数：同标题（归一化压空白）不同 paper_id 只保留首个（分数最高）。"""
    from backend.services.semantic_search import _dedupe_by_paper
    items = [
        {"paper_id": 65, "title": "Same Title Here", "score": 0.99},
        {"paper_id": 743, "title": "Same  Title Here", "score": 0.98},
        {"paper_id": 9, "title": "Other Title", "score": 0.7},
    ]
    out = _dedupe_by_paper(items)
    assert [it["paper_id"] for it in out] == [65, 9]


def test_dedupe_by_title_prefers_published_version():
    """同标题双版本：正式版顶替预印本的名额（即便预印本分数更高）。"""
    from backend.services.semantic_search import _dedupe_by_paper
    items = [
        {"paper_id": 739, "title": "Large-scale Recording Paper", "score": 0.99,
         "journal": "bioRxiv (Cold Spring Harbor Laboratory)"},
        {"paper_id": 677, "title": "Large-scale Recording Paper", "score": 0.95,
         "journal": "Nature Neuroscience"},
        {"paper_id": 100, "title": "Unrelated", "score": 0.5, "journal": "Nature"},
    ]
    out = _dedupe_by_paper(items)
    assert [it["paper_id"] for it in out] == [677, 100]


def test_dedupe_by_paper_without_title_keeps_paper_dedup():
    """条目无 title 字段时退化为纯 paper_id 去重（旧调用方兼容）。"""
    from backend.services.semantic_search import _dedupe_by_paper
    items = [
        {"paper_id": 1, "score": 0.9},
        {"paper_id": 1, "score": 0.8},
        {"paper_id": 2, "score": 0.7},
    ]
    assert [it["paper_id"] for it in _dedupe_by_paper(items)] == [1, 2]


def _set_status(paper_id: int, col: str, val: str) -> None:
    conn = get_conn()
    try:
        conn.execute(f"UPDATE papers SET {col} = ? WHERE id = ?", (val, paper_id))
    finally:
        conn.close()


def test_semantic_search_excludes_manual_hold(temp_db, fake_retrieval):
    """内容状态列 manual_hold 的论文不进入跨库语义检索。

    2026-09-08 实测：743 号（65 号的同标题重复入库，status_summary='manual_hold'）
    在 18 次查询中命中 7 次，其中一次与 65 号并列第一。
    """
    from backend.services.semantic_search import semantic_search
    a = _insert_paper("ss_ok_paper")
    h = _insert_paper("ss_held_paper")
    _set_status(h, "status_summary", "manual_hold")
    _insert_chunk(a, 0, "alpha", [1.0, 0.0])
    _insert_chunk(h, 0, "alpha2", [1.0, 0.0])

    r = semantic_search("query", topk=5)
    pids = [it["paper_id"] for it in r["items"]]
    assert a in pids
    assert h not in pids


def test_semantic_search_single_paper_held_returns_empty(temp_db, fake_retrieval):
    """单篇模式检索内容状态 manual_hold 论文：空结果（不报 no embeddings 误导错误）。"""
    from backend.services.semantic_search import semantic_search
    h = _insert_paper("ss_held_single")
    _set_status(h, "status_parsed", "manual_hold")
    _insert_chunk(h, 0, "alpha", [1.0, 0.0])

    r = semantic_search("query", topk=5, paper_id=h)
    assert r.get("count") == 0
    assert r.get("items") == []
    assert "error" not in r


def test_semantic_search_allows_enrich_manual_hold(temp_db, fake_retrieval):
    """status_enrich 只表示元数据补全失败，不排除内容完好的论文。"""
    from backend.services.semantic_search import semantic_search
    paper_id = _insert_paper("ss_enrich_held")
    _set_status(paper_id, "status_enrich", "manual_hold")
    _insert_chunk(paper_id, 0, "alpha", [1.0, 0.0])

    result = semantic_search("query", topk=5)

    assert "error" not in result
    assert paper_id in [item["paper_id"] for item in result["items"]]


def _fake_hrc_strict():
    return ({"rerank_enabled": True, "candidate_multiplier": 10,
             "rerank_threshold": 0.95, "search_max_embeddings": 200000}, None)


def test_scoped_search_bypasses_threshold(temp_db, monkeypatch):
    """单篇定位不做阈值过滤：0.5 阈值按跨库标定，「在这篇里找」的弱相关片段
    也是有效答案；低于阈值的条目标 low_confidence。同条件下跨库检索仍被
    阈值过滤（实测：篇内零命中含原文原词真漏检）。"""
    from backend.services.semantic_search import semantic_search
    monkeypatch.setattr("backend.services.semantic_search.hosted_retrieval_config",
                        _fake_hrc_strict)
    monkeypatch.setattr("backend.external.hosted_retrieval.HostedRetrievalClient",
                        _FakeHostedClient)
    # 单篇 3 chunk、topk=2：候选数 > topk 才进入精排阶段
    a = _insert_paper("ss_scoped_strict")
    _insert_chunk(a, 0, "alpha", [1.0, 0.0])
    _insert_chunk(a, 1, "alpha2", [1.0, 0.0])
    _insert_chunk(a, 2, "beta", [1.0, 0.0])

    r = semantic_search("query", topk=2, paper_id=a)
    assert r["stage"] == "rerank"
    # alpha 0.9 / alpha2 0.85 都低于 0.95：旧行为 count=0，新行为全返回
    assert r["count"] == 2
    assert all(it["low_confidence"] is True for it in r["items"])
    assert r.get("threshold_applied") is False

    r_global = semantic_search("query", topk=2)
    assert r_global["count"] == 0


def test_scoped_no_embeddings_message_points_to_paper(temp_db, fake_retrieval):
    """单篇检索该篇无向量：报错带 paper id 与流水线状态，不复用全库文案。"""
    from backend.services.semantic_search import semantic_search
    pid = _insert_paper("ss_no_vec")

    r = semantic_search("query", topk=5, paper_id=pid)
    assert "error" in r
    assert f"paper {pid} has no embeddings" in r["error"]
    assert "status_parsed" in r["error"]

    # 不存在的 paper_id
    r2 = semantic_search("query", topk=5, paper_id=99999)
    assert r2.get("error") == "paper 99999 not found"
