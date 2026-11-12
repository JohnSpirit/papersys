"""翻译流水线：预处理 → 分块 → 占位符提取 → LLM 翻译 → 后处理 → 入库。

设计要点（继承自旧 translate_paper.py，已修复历史踩坑）：
  * 占位符末尾不追加碎片（步骤 6b 改为只警告）
  * 进程内 import，不调子 shell
  * 每 chunk 调用 timeout=180
  * 参考文献整段不翻译（detect `# References` 或回退正则）
  * 超长 chunk 按段落边界二次切分（防 LLM 输出截断）
"""
import re
from dataclasses import dataclass
from typing import Optional

from backend.config import CONFIG
from backend.db import glossary_repo
from backend.external.llm import LLMClient


MAX_CHUNK_CHARS = int(CONFIG.get("workers", {}).get("max_chunk_chars", 6000))


# ── 翻译规则（system prompt 固定部分） ──
TRANSLATION_RULES = """翻译规则：
1. 保持 Markdown 格式结构完整（标题层级、列表等）
2. 所有【PLACEHOLDER】格式的标记原样保留，不要修改
3. 所有参考文献引用格式（如 "(Britten et al., 1992)"）原样保留
4. 数字、统计值、单位原样保留
5. 翻译要流畅自然，符合中文学术写作习惯
6. 长句可以适当拆分，保持原意完整
7. 输出只包含翻译后的内容，不要添加任何解释"""


REFERENCES_RE = re.compile(r"\n(#{1,6})\s*References?\b", re.IGNORECASE)
REFERENCES_FALLBACK_RE = re.compile(r"\n(?:#\s*)?1\.\s+[A-Z]", re.MULTILINE)


def build_system_prompt() -> str:
    glossary_text = glossary_repo.render_for_prompt()
    parts = ["你是一个专业的神经科学论文翻译助手。请将以下英文论文段落翻译成中文。\n"]
    if glossary_text:
        parts += [glossary_text, ""]
    parts.append(TRANSLATION_RULES)
    return "\n".join(parts)


# ── 占位符提取 ────────────────────────────────────────


def extract_placeholders(text: str) -> tuple[str, dict[str, str]]:
    mapping: dict[str, str] = {}
    counter = [0]

    def make_replacer(prefix: str):
        def replacer(m: re.Match) -> str:
            counter[0] += 1
            key = f"【{prefix}_{counter[0]:03d}】"
            mapping[key] = m.group(0)
            return key
        return replacer

    # 顺序：显示公式 → 图片 → 代码块 → HTML → 行内公式
    text = re.sub(r"\$\$(.*?)\$\$", make_replacer("DM"), text, flags=re.DOTALL)
    text = re.sub(r"!\[.*?\]\(.*?\)", make_replacer("IMG"), text)
    text = re.sub(r"```[\s\S]*?```", make_replacer("CODE"), text)
    text = re.sub(
        r"<(details|summary|table|tr|td|th)[^>]*>[\s\S]*?</\1>",
        make_replacer("HTML"),
        text,
    )
    text = re.sub(r"\$(.+?)\$", make_replacer("IM"), text)
    return text, mapping


def restore_placeholders(text: str, mapping: dict[str, str]) -> str:
    for key in sorted(mapping.keys(), key=len, reverse=True):
        text = text.replace(key, mapping[key])
    return text


# ── 分章节 & 切块 ──────────────────────────────────────


@dataclass
class Chunk:
    index: int
    heading: str
    en_text: str
    is_references: bool = False


def split_references(text: str) -> tuple[str, str]:
    """把 references 之后的内容切出来。返回 (body, references_or_empty)。

    标题命中（H1-H6）时 refs 段止于下一个同级或更高级标题——arXiv 式
    「References 之后还有附录」的文档，附录留在 body 里继续切块翻译；
    无标题时走回退正则，行为不变（命中处到文末）。
    """
    m = REFERENCES_RE.search(text)
    if not m:
        m = REFERENCES_FALLBACK_RE.search(text)
        if not m:
            return text, ""
        return text[: m.start()], text[m.start():]
    level = len(m.group(1))
    end_m = re.compile(r"\n#{1,%d}\s" % level).search(text, m.end())
    end = end_m.start() if end_m else len(text)
    return text[: m.start()] + text[end:], text[m.start(): end]


def _split_by_chars(text: str, heading_line: str, max_chars: int) -> list[str]:
    """超长 chunk 按段落 / 行边界递归切分，每片不超过 max_chars。

    保留 heading_line 作为后续片段的标题前缀（便于 LLM 上下文）。
    优先在 \\n\\n 处切；其次 \\n；最后硬切。
    """
    if len(text) <= max_chars:
        return [text]

    pieces: list[str] = []
    remainder = text
    is_first = True
    while len(remainder) > max_chars:
        window = remainder[:max_chars]
        split_pos = window.rfind("\n\n")
        if split_pos < max_chars // 5:
            split_pos = window.rfind("\n")
        if split_pos < max_chars // 5:
            split_pos = max_chars
        pieces.append(remainder[:split_pos].rstrip())
        rest = remainder[split_pos:].lstrip("\n")
        if not is_first or not heading_line:
            remainder = rest
        else:
            remainder = (heading_line + "\n" + rest) if heading_line else rest
        is_first = False
    if remainder.strip():
        pieces.append(remainder)
    return pieces


def split_into_chunks(text: str, max_chars: Optional[int] = None) -> list[Chunk]:
    cap = max_chars if max_chars is not None else MAX_CHUNK_CHARS
    body, refs = split_references(text)
    chunks: list[Chunk] = []
    cur_lines: list[str] = []
    heading = "HEADER"
    heading_line = ""

    def flush() -> None:
        if not cur_lines:
            return
        raw = "\n".join(cur_lines)
        if not raw.strip():
            return
        for piece in _split_by_chars(raw, heading_line, cap):
            if piece.strip():
                chunks.append(Chunk(
                    index=len(chunks),
                    heading=heading,
                    en_text=piece,
                    is_references=False,
                ))

    for line in body.splitlines():
        if re.match(r"^#{1,2} ", line):
            flush()
            heading = line.lstrip("#").strip()
            heading_line = line
            cur_lines = [line]
        else:
            cur_lines.append(line)
    flush()

    if refs.strip():
        for piece in _split_by_chars(refs, "", cap):
            if piece.strip():
                chunks.append(Chunk(
                    index=len(chunks),
                    heading="References",
                    en_text=piece,
                    is_references=True,
                ))
    # 重新编号确保连续
    for i, c in enumerate(chunks):
        c.index = i
    return chunks


# ── 单 chunk 翻译 ──────────────────────────────────────


def translate_chunk(
    chunk: Chunk,
    *,
    client: Optional[LLMClient] = None,
    system_prompt: Optional[str] = None,
    context_tail: Optional[str] = None,
    timeout: int = 180,
) -> tuple[str, dict]:
    """翻译单个 chunk，返回 (zh_text, meta)。

    若 chunk.is_references 则直接返回原文（不翻译）。
    """
    if chunk.is_references:
        return chunk.en_text, {"status": "skipped", "reason": "references"}

    if client is None:
        client = LLMClient()
    if system_prompt is None:
        system_prompt = build_system_prompt()

    # 占位符替换
    masked, mapping = extract_placeholders(chunk.en_text)

    chunk_info = f"chunk #{chunk.index}: {chunk.heading[:60]}"
    user_msg_parts = []
    if context_tail:
        user_msg_parts.append(f"以下是上一段的最后一句，请保持上下文连贯：\n{context_tail}\n")
    user_msg_parts.append(f"请翻译以下论文段落（{chunk_info}）：\n\n{masked}")
    user_msg = "\n".join(user_msg_parts)

    zh = client.chat(system_prompt, user_msg, timeout=timeout)
    # 恢复占位符
    zh_restored = restore_placeholders(zh, mapping)

    # 完整性校验：只 emit 警告，不修改文件（旧坑）
    warnings = []
    for key in mapping:
        if key not in zh and key not in zh_restored:
            warnings.append(f"placeholder lost: {key}")

    return zh_restored, {
        "status": "ok",
        "placeholders": len(mapping),
        "en_len": len(chunk.en_text),
        "zh_len": len(zh_restored),
        "warnings": warnings,
    }


def extract_tail_sentence(text: str, max_chars: int = 300) -> str:
    """取末尾一句作为上下文。"""
    # 移除占位符行
    clean = re.sub(r"【[A-Z]+_\d{3}】", "", text).strip()
    if not clean:
        return ""
    # 按句号/换行倒切
    sents = re.split(r"(?<=[。！？.!?])\s+", clean)
    last = sents[-1] if sents else clean
    return last[-max_chars:]


# ── 整文档拼接 ────────────────────────────────────────


def assemble(chunks_with_zh: list[tuple[Chunk, str]]) -> str:
    """按 chunk.index 排序后拼接 zh_text。"""
    ordered = sorted(chunks_with_zh, key=lambda x: x[0].index)
    return "\n".join(zh for _, zh in ordered)