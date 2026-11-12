"""文档文本读取：EN/ZH/Summary 覆盖层规则的单一实现。

读取方（reader API / preview API / translate / embed / summary worker / MCP）
此前各自内联同一套规则，现统一在此：

  - EN：`translations.chunk_index=-1` 的 `en_text`（人工复核覆盖）优先，
    其次 `parsed_path` 指向的 MinerU 原文
  - ZH：`chunk_index=-1` 的 `zh_text`（人工编辑覆盖）优先，
    其次 `chunk_index>=0` 按序拼接自动 chunk
  - Summary：`summaries` 表最新 version
"""
from typing import Optional

import re

from backend.config import resolve_path
from backend.db.conn import get_conn

_HEADING_RE = re.compile(r"^(#{1,4})\s+(.+?)\s*#*\s*$")


def extract_toc(text: str, max_entries: int = 50) -> list[dict]:
    """从 markdown 全文提取标题行目录：[{level, title, offset}]。

    offset 是标题行在全文中的字符偏移，可直接作为 read_fulltext /
    read_translation 的 offset 参数跳转（3.1.1：核实任务此前只能线性翻页）。
    只认 ATX 标题（# 至 ####，MinerU 产出形态）；上限 max_entries 防超长目录。
    """
    toc: list[dict] = []
    pos = 0
    for line in text.split("\n"):
        m = _HEADING_RE.match(line)
        if m:
            toc.append({
                "level": len(m.group(1)),
                "title": m.group(2).strip()[:100],
                "offset": pos,
            })
            if len(toc) >= max_entries:
                break
        pos += len(line) + 1
    return toc


def read_en_override(paper_id: int) -> str:
    """读取 translations.chunk_index=-1 的 en_text；空字符串视为未复核。"""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT en_text FROM translations WHERE paper_id = ? AND chunk_index = -1",
            (paper_id,),
        ).fetchone()
        return (row["en_text"] or "") if row else ""
    finally:
        conn.close()


def read_latest_summary(paper_id: int) -> Optional[dict]:
    """读取 summaries 最新 version；无记录返回 None。"""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT content_md, version, source FROM summaries"
            " WHERE paper_id = ? ORDER BY version DESC LIMIT 1",
            (paper_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def read_doc_text(paper: dict, doc_type: str) -> tuple[str, str]:
    """读文档全文，返回 (text, source)。

    source 取值：en -> manual_edit|parsed|missing；zh -> manual_edit|auto|missing；
    summary -> 行内 source（auto|web_edit 等）|missing。
    """
    paper_id = paper["id"]

    if doc_type == "en":
        override = read_en_override(paper_id)
        if override:
            return override, "manual_edit"
        parsed = resolve_path(paper.get("parsed_path"))
        if parsed:
            return parsed.read_text(encoding="utf-8", errors="replace"), "parsed"
        return "", "missing"

    if doc_type == "zh":
        conn = get_conn()
        try:
            row = conn.execute(
                "SELECT zh_text FROM translations WHERE paper_id = ? AND chunk_index = -1",
                (paper_id,),
            ).fetchone()
            if row and row["zh_text"]:
                return row["zh_text"], "manual_edit"
            rows = conn.execute(
                "SELECT zh_text FROM translations WHERE paper_id = ? AND chunk_index >= 0 ORDER BY chunk_index",
                (paper_id,),
            ).fetchall()
        finally:
            conn.close()
        parts = [r["zh_text"] for r in rows if r["zh_text"]]
        return "\n".join(parts), "auto" if parts else "missing"

    if doc_type == "summary":
        latest = read_latest_summary(paper_id)
        if latest:
            return latest["content_md"] or "", latest["source"] or "auto"
        return "", "missing"

    raise ValueError(f"unknown doc_type: {doc_type!r}")


# ── 扫描合订本尾巴页剥离（parse_worker 解析后调用）────────────────
# 老文献的扫描合订本里，第 1 页常是前一篇文章的结尾（多为参考文献条目或
# 半句话），MinerU 会把它解析进 full.md 开头。2026-10-17 用户提出解析后
# 自动删除；判定靠标题锚点，条件不满足时宁可不剥也不误剥正文。

_TAIL_REF_RE = re.compile(r"(?m)^\s*(?:\[\d+\]\s.{10,}|\d+\.\s+[A-Z].{10,})")


def _norm_with_map(text: str) -> tuple[str, list[int]]:
    """小写 + 空白折叠为单空格，返回 (norm_text, norm[i] 对应的原文偏移)。

    HTML 标记（<sub>...</sub> 等，MinerU 在标题里常见）的字符不进 norm，
    使带标点的印刷标题能与纯文本锚点匹配——但映射仍指向原文偏移。
    """
    chars: list[str] = []
    idx: list[int] = []
    in_tag = False
    for i, ch in enumerate(text.lower()):
        if ch == "<":
            in_tag = True
            continue
        if ch == ">":
            in_tag = False
            continue
        if in_tag:
            continue
        if ch.isspace():
            if chars and chars[-1] != " ":
                chars.append(" ")
                idx.append(i)
        else:
            chars.append(ch)
            idx.append(i)
    return "".join(chars), idx


def strip_preceding_tail(text: str, title: str) -> tuple[str, int]:
    """剥掉 full.md 开头的"前一篇文章的末尾页"，返回 (正文, 剥掉的字符数)。

    判定（全部满足才剥，保守取向）：
      1. 标题锚点在文中出现（归一化后大小写不敏感；MinerU 折行不影响）；
      2. 锚点之前有内容（≥ 800 字符）；
      3. 前文呈尾部形态：参考文献条目 ≥ 3 条，或前文占全文 ≥ 20%。
    不剥的情形：标题缺失/找不到锚点（如标题是纯图片且 OCR 未产出）、
    前文太短、前文形态不像别文尾部。重解析幂等（剥后锚点在文首）。
    """
    if not text or not title:
        return text, 0
    norm, idx = _norm_with_map(text)
    anchor = " ".join(title.lower().split())
    for probe in (anchor[:60], anchor[:30]):
        if len(probe) < 12:
            continue
        p = norm.find(probe)
        if p < 0:
            continue
        orig_off = idx[p]
        if orig_off < 800:
            return text, 0
        pre = text[:orig_off]
        # 同篇补充材料不剥：合并版 PDF（正文+自己的 Supplementary 在前部）
        # 的锚点前文带 SI 标记，剥掉会删掉本论文的内容（实测：18k 字符
        # 是本篇 SI 图注，非别文尾部）
        if re.search(r"(?i)supplementary\s+(information|figure|fig\b|note|material)", pre):
            return text, 0
        n_refs = len(_TAIL_REF_RE.findall(pre))
        if n_refs < 3 and len(pre) < 0.2 * len(text):
            return text, 0
        # 锚点前的同行内容若只是 # 标题标记（MinerU 把标题渲成
        # "# {标题}"），把切点扩到行首，保住标题的 markdown 标记
        line_start = text.rfind("\n", 0, orig_off) + 1
        if re.fullmatch(r"\s*#{1,6}\s*", text[line_start:orig_off]):
            orig_off = line_start
        return text[orig_off:].lstrip("\n"), orig_off
    return text, 0
