"""语义检索 service：两阶段（cosine 召回 -> 托管精排 + 阈值过滤）。

独立实现，流程设计参考 BrainPilot 的两阶段方案（全托管形态，无本地模型）：
  1. 查询向量化（与建库同模型同维度，hosted /embeddings）
  2. numpy 余弦召回 top-(topk × candidate_multiplier)
  3. 托管精排（/rerank）+ relevance_score 阈值过滤（阈值见 config.toml [retrieval]）
     阈值仅作用于跨库检索；单篇定位（paper_id 限定）不过滤，低于阈值的
     条目标 low_confidence。
短路：候选数 <= topk 或 rerank_enabled=false 时跳过精排，直接按余弦分数返回
（库小时省每次约 1~2s 的网络往返）。

错误约定：检索工具永不抛异常，错误以 {"error": ...} 返回。
量级假设：数百篇 × ~100 chunks = 数万向量，numpy 矩阵乘法毫秒级。
"""
from typing import Optional

from backend.config import hosted_retrieval_config
from backend.db.conn import get_conn


def _dedupe_by_paper(items: list[dict]) -> list[dict]:
    """按 paper_id 与「归一化标题 + 附件类别」双重去重：各保留首个（分数最高）
    片段，保持顺序。

    paper_id 去重提升固定 topk 下的论文覆盖数（实测 top-5 可被 2 篇论文
    占满只覆盖 3 篇）；标题去重处理同标题重复入库（同标题双份会以近似分数
    并列占用名额）。同标题双版本（preprint/正式版）并存时正式版顶替预印本
    的名额（is_preprint_record 判定）。要求输入已按分数降序。

    附件类别（attachment_kind：'si' = SI 附件）参与去重键：SI 行经母论文 DOI
    富化后与母论文同标题是正常形态（attachment_kind 引入分桶），
    SI 与正文互不挤占名额，同类别内部行为不变。
    """
    from backend.db.repo import is_preprint_record
    seen_ids: set = set()
    kept_by_title: dict[str, dict] = {}
    out: list[dict] = []
    for it in items:
        pid = it["paper_id"]
        if pid in seen_ids:
            continue
        title = " ".join((it.get("title") or "").lower().split())
        if not title:
            seen_ids.add(pid)
            out.append(it)
            continue
        kind = "si" if (it.get("attachment_kind") or "") == "si" else "doc"
        key = title + "\x00" + kind
        prev = kept_by_title.get(key)
        if prev is None:
            kept_by_title[key] = it
            seen_ids.add(pid)
            out.append(it)
            continue
        if is_preprint_record(prev.get("journal"), None) and \
                not is_preprint_record(it.get("journal"), None):
            for i, x in enumerate(out):
                if x is prev:
                    out[i] = it
                    break
            seen_ids.discard(prev["paper_id"])
            seen_ids.add(pid)
            kept_by_title[key] = it
    return out


def _chunk_offsets(full_text: str, chunk_text: str) -> tuple[Optional[int], Optional[int]]:
    """返回 chunk 在 full.md 中的字符区间；定位失败时返回 (None, None)。"""
    if not full_text or not chunk_text:
        return None, None
    index = full_text.find(chunk_text)
    if index >= 0:
        return index, min(len(full_text), index + len(chunk_text))
    probe = chunk_text[:200].strip()
    if probe:
        index = full_text.find(probe)
        if index >= 0:
            return index, min(len(full_text), index + len(chunk_text))
    return None, None


def _attach_offsets(items: list[dict], rows: list) -> list[dict]:
    """给检索结果附 full.md 字符偏移，便于 read_fulltext 直接跳转。"""
    if not items:
        return items
    from backend.db.repo import get_paper
    from backend.services.doc_text import read_doc_text

    chunks = {(r["paper_id"], r["chunk_index"]): r["text"] for r in rows}
    documents: dict[int, str] = {}
    for item in items:
        paper_id = item["paper_id"]
        if paper_id not in documents:
            try:
                conn = get_conn()
                try:
                    paper = get_paper(conn, paper_id)
                finally:
                    conn.close()
                documents[paper_id] = read_doc_text(paper, "en")[0] if paper else ""
            except Exception:
                documents[paper_id] = ""
        start, end = _chunk_offsets(
            documents[paper_id], chunks.get((paper_id, item["chunk_index"]), ""),
        )
        item["char_start"] = start
        item["char_end"] = end
    return items


def semantic_search(
    query: str,
    topk: int = 5,
    paper_id: Optional[int] = None,
    include_context: int = 200,
) -> dict:
    """语义检索库内论文 chunk。返回 dict（错误为 {"error": ...}，永不抛异常）。

    跨库检索（未指定 paper_id）的结果按论文去重：每篇论文只保留最高分片段，
    提升固定 topk 下的论文覆盖数；单篇检索保留同论文多片段。内容状态列为
    manual_hold 的论文不参与检索；status_enrich 只表示元数据补全状态，不参与
    排除（重复入库等待人工处理的记录不进入结果，2026-09-08 实测 743 号重复
    记录在 18 次查询中命中 7 次）。

    Args:
        query: 自然语言查询
        topk: 返回条数
        paper_id: 限定单篇论文内检索（可选）
        include_context: 每条正文回显字符数
    """
    import numpy as np
    from backend.db.vectors import unpack
    from backend.external.hosted_retrieval import HostedRetrievalClient

    if not query or not query.strip():
        return {"error": "query required"}

    try:
        cfg, err = hosted_retrieval_config()
        if err:
            return {"error": err}

        conn = get_conn()
        cap = cfg["search_max_embeddings"]
        # 排除只看内容状态列：enrich 失败是元数据层问题（解析内容不受影响，
        # 回填实证 enrich-hold 含 example2008 等经典论文），
        # status_enrich 的 manual_hold 经 get_paper/徽章/stats 暴露，不排除检索
        not_held = ("'manual_hold' NOT IN (p.status_pdf, p.status_parsed, p.status_translated,"
                    " p.status_summary, p.status_outline, p.status_embedded)")
        held_filtered = False
        try:
            if paper_id is not None:
                rows = conn.execute(
                    "SELECT e.paper_id, e.chunk_index, e.text, e.vector, e.dim, p.title, p.first_author, p.year, p.journal, p.attachment_kind"
                    " FROM embeddings e JOIN papers p ON p.id = e.paper_id"
                    f" WHERE e.paper_id = ? AND {not_held} LIMIT ?", (paper_id, cap)).fetchall()
                if not rows:
                    # 区分「该论文无向量」（保留 no embeddings 错误，既有约定）与
                    # 「有向量但被 manual_hold 过滤」（空结果，不误导）
                    held_filtered = bool(conn.execute(
                        "SELECT 1 FROM embeddings e JOIN papers p ON p.id = e.paper_id"
                        " WHERE e.paper_id = ? LIMIT 1", (paper_id,)).fetchone())
            else:
                rows = conn.execute(
                    "SELECT e.paper_id, e.chunk_index, e.text, e.vector, e.dim, p.title, p.first_author, p.year, p.journal, p.attachment_kind"
                    " FROM embeddings e JOIN papers p ON p.id = e.paper_id"
                    f" WHERE {not_held} LIMIT ?", (cap,)).fetchall()
        finally:
            conn.close()

        if not rows:
            if paper_id is not None:
                if held_filtered:
                    # 该论文有向量但全部被 manual_hold 过滤：空结果
                    return {"query": query, "count": 0, "items": []}
                # 该篇无向量：文案指向这一篇的流水线状态（实测：parse
                # 失败的论文触发全库级文案，agent 被误导去重建全库向量）
                conn2 = get_conn()
                try:
                    prow = conn2.execute(
                        "SELECT status_parsed, status_embedded FROM papers WHERE id = ?",
                        (paper_id,)).fetchone()
                finally:
                    conn2.close()
                if not prow:
                    return {"error": f"paper {paper_id} not found"}
                return {"error": f"paper {paper_id} has no embeddings "
                                 f"(status_parsed={prow['status_parsed']}, "
                                 f"status_embedded={prow['status_embedded']}); "
                                 f"该篇尚未完成解析/向量化，先跑 parse/embed"}
            return {"error": "no embeddings in library (run `paper embed --all` first)"}
        # 达到上限 -> 结果被截断（库规模已超出内存全量扫描的假设，见 config.toml search_max_embeddings）
        truncated: dict = {"truncated": True} if len(rows) >= cap else {}

        client = HostedRetrievalClient()

        # 第一阶段：余弦召回
        qvec = np.array(client.embed([query])[0], dtype="float32")
        qvec /= max(np.linalg.norm(qvec), 1e-9)

        mat = np.stack([np.array(unpack(r["vector"]), dtype="float32") for r in rows])
        norms = np.linalg.norm(mat, axis=1)
        norms[norms == 0] = 1e-9
        mat /= norms[:, None]
        scores = mat @ qvec

        topk = max(1, min(int(topk), 50))
        n_cand = min(topk * cfg["candidate_multiplier"], len(rows))
        cand_order = np.argsort(-scores)[:n_cand]

        def _item(i: int, score: float) -> dict:
            r = rows[i]
            return {
                "paper_id": r["paper_id"],
                "title": r["title"],
                "first_author": r["first_author"],
                "year": r["year"],
                "journal": r["journal"],
                "attachment_kind": r["attachment_kind"],
                "chunk_index": r["chunk_index"],
                "score": round(float(score), 4),
                "text": r["text"][:int(include_context)],
            }

        def _finalize(items: list[dict]) -> list[dict]:
            # 单篇检索（paper_id 锁定）保留同论文多片段；跨库检索按论文去重
            if paper_id is None:
                items = _dedupe_by_paper(items)
            return _attach_offsets(items[:topk], rows)

        # 短路：候选池 <= topk 或未启用精排 -> 纯余弦结果
        if not cfg["rerank_enabled"] or len(cand_order) <= topk:
            items = _finalize([_item(int(i), scores[int(i)]) for i in cand_order])
            return {"query": query, "count": len(items), "stage": "embed",
                    **truncated, "items": items}

        # 第二阶段：托管精排 + 阈值过滤
        cand_idx = [int(i) for i in cand_order]
        threshold = cfg["rerank_threshold"]
        try:
            reranked = client.rerank(query, [rows[i]["text"] for i in cand_idx])
        except Exception as e:  # noqa: BLE001
            # 精排不可用 -> 降级为纯余弦召回（保住 RAG 可用性）
            items = _finalize([_item(int(i), scores[int(i)]) for i in cand_order])
            return {"query": query, "count": len(items), "stage": "embed_fallback",
                    "rerank_error": str(e)[:200], **truncated, "items": items}
        if paper_id is not None:
            # 单篇定位不做阈值过滤：0.5 阈值按跨库检索标定，「在这篇里找」意图
            # 下弱相关片段也是有效答案（实测：篇内零命中含原文原词与跨语言
            # 查询的真漏检，agent 被迫退化为顺序盲读全文）。
            # 低于阈值的条目标 low_confidence，供调用方自行权衡。
            items = _finalize([
                {**_item(cand_idx[r["index"]], r["score"]),
                 **({"low_confidence": True} if r["score"] < threshold else {})}
                for r in reranked
            ])
            return {"query": query, "count": len(items), "stage": "rerank",
                    "threshold_applied": False, **truncated, "items": items}
        items = _finalize([
            _item(cand_idx[r["index"]], r["score"])
            for r in reranked if r["score"] >= threshold
        ])
        return {"query": query, "count": len(items), "stage": "rerank",
                "threshold": threshold, **truncated, "items": items}
    except Exception as e:  # noqa: BLE001
        return {"error": f"semantic search failed: {e}"}
