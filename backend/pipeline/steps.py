"""投递与流水线的默认步骤（CLI / HTTP API / MCP 共用）。"""

# 投递新 PDF 时默认顺序执行的步骤。translate 不默认：翻译面向人工阅读、
# 依赖 LLM 且需要复核，作为入库后的可选步骤单独触发。
FEED_STEPS = ["ingest", "parse", "enrich", "embed", "summary"]

# 已入库论文的一键流水线默认步骤（pipeline worker 的 DEFAULT_STEPS）。
PAPER_STEPS = ["parse", "enrich", "embed", "translate", "summary"]

KNOWN_STEPS = frozenset(FEED_STEPS) | {"translate"}


def normalize_feed_steps(steps=None) -> list[str]:
    """校验投递步骤并返回列表；None 或空值使用 FEED_STEPS。

    约定：首个步骤必须是 ingest（pipeline feed 模式要求），未知步骤报错。
    字符串按逗号分隔，便于 CLI 与 multipart 表单直接传入。
    """
    if steps is None:
        return list(FEED_STEPS)
    if isinstance(steps, str):
        steps = steps.split(",")
    cleaned = [s.strip() for s in steps if s and s.strip()]
    if not cleaned:
        return list(FEED_STEPS)
    if cleaned[0] != "ingest":
        raise ValueError("feed steps must start with 'ingest'")
    unknown = [s for s in cleaned if s not in KNOWN_STEPS]
    if unknown:
        raise ValueError(f"unknown steps: {unknown}")
    return cleaned
