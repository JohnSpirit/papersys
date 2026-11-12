"""摘要↔解析文本重合校验：纯函数，审计脚本与 enrich 在线校验共用。

papers.abstract 来自 DOI 对应的出版记录（OpenAlex -> Crossref -> Europe PMC
三源回退，见 backend/workers/enrich_worker.py），把它与解析文本（MinerU
full.md / EN 覆盖层）做归一化词组重合比对，一次计算覆盖两个校验目标：
DOI 换回的摘要与 PDF 实际内容是否同一篇论文（元数据校验），以及解析
产物是否完整（解析质量校验）。本模块只产出分数与分档，动作（events
警告等）由调用方决定——SI 附件、预印本↔正式版、graphical abstract /
preview 版式三类场景会合法地低分，不能据此自动 manual_hold。

匹配口径：NFKC + lower + 行末断词连字符合并 + 其余非字母数字折叠为
空格（对齐 scripts/verify_ingest.py title_sim 的归一化先例，另加 NFKC
处理合字/上标、断词合并处理换行连字符）。词组匹配而非逐字子串，是为
了吸收 MinerU 输出的 markdown 符号、上下标、合字与 OCR 噪声。
"""
import re
import unicodedata

SHINGLE_SIZE = 5          # 连续词数：一个片段
MIN_ABSTRACT_WORDS = 25   # 归一化后不足此词数不校验（统计噪声）
LOW_THRESHOLD = 0.30      # < low：疑似 SI 附件 / DOI 错配 / 解析失败
GRAY_THRESHOLD = 0.80     # >= gray 通过；两线之间为灰区（版本差异等）

# 分档线由 scripts/abstract_audit.py 全库标定：0.80 为重合密度断崖（其上方
# 为符号/上下标/OCR 噪声带），0.30 以下多为 SI 附件/DOI 错配/解析失败。低分区
# 多为合法成因（预印本↔正式版措辞差异、书章/短摘要版式等），故在线动作只到
# events 警告，不自动 manual_hold。

_HYPHEN_BREAK_RE = re.compile(r"-\s+")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def normalize_for_match(text: str) -> str:
    """NFKC + lower + 合并断词连字符 + 其余非字母数字折叠为空格。

    "inter-\\nneuronal" -> "interneuronal"（吸收换行断词）；
    "decision-making" -> "decision making"（普通连字符两侧一致折叠）。
    """
    if not text:
        return ""
    s = unicodedata.normalize("NFKC", text).lower()
    s = _HYPHEN_BREAK_RE.sub("", s)
    return _NON_ALNUM_RE.sub(" ", s).strip()


def shingle_containment(abstract: str, fulltext: str,
                        n: int = SHINGLE_SIZE) -> float | None:
    """摘要 n 连词片段出现在归一化全文中的比例；无法校验返回 None。

    None 条件：摘要归一化后不足 MIN_ABSTRACT_WORDS 词，或全文归一化后为空。
    """
    words = normalize_for_match(abstract).split()
    if len(words) < MIN_ABSTRACT_WORDS:
        return None
    f = normalize_for_match(fulltext)
    if not f:
        return None
    total = len(words) - n + 1
    hits = sum(
        1 for i in range(total)
        if " ".join(words[i:i + n]) in f
    )
    return hits / total


def classify_containment(score: float | None) -> str | None:
    """分档："match"（>= GRAY_THRESHOLD）/ "gray"（[LOW, GRAY)）/
    "low"（< LOW_THRESHOLD）；score 为 None 表示无法校验。"""
    if score is None:
        return None
    if score >= GRAY_THRESHOLD:
        return "match"
    if score >= LOW_THRESHOLD:
        return "gray"
    return "low"
