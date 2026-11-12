"""Crossref API — 用于 PDF metadata 反查 DOI。"""
import html
import logging
import re
from difflib import SequenceMatcher
from typing import Optional
from urllib.parse import quote

from backend.external.base import RateLimitedClient
from backend.pipeline.pdf_classify import is_derivative_literature, normalize_author

log = logging.getLogger(__name__)

# 反查硬跳过的 Crossref type：component=SI/图表附件（relation is-component-of 母记录）、
# reference-book/entry=工具书条目（全量实测 Benezit 画家词典抢位 "Tetrode technology"、
# SAGE 百科全书抢位 "Deep learning"）、dataset/database 等。原论文只会是
# journal-article / preprint / proceedings-article / book-chapter（软惩罚）。
_HARD_SKIP_TYPES = {
    "component", "dataset", "database", "reference-entry", "reference-book",
    "journal-issue", "journal-volume", "book-set", "report-component",
    "standard", "journal",
}
_SOFT_BAD_TYPES = {"book-chapter", "book", "proceedings-article", "other"}

# 采纳线：总分 >= 0.75 且标题相似度 >= 0.60。同名勘误/来信与原论文 sim≈1.0，
# 靠作者/年份信号分出胜负；无作者/年份线索时则要求标题足够像。
_ACCEPT_SCORE = 0.75
_MIN_TITLE_SIM = 0.60


def _item_title(item: dict) -> str:
    t = (item or {}).get("title")
    return t[0] if isinstance(t, list) and t else (t or "")


def _item_year(item: dict) -> Optional[int]:
    for k in ("published-print", "published-online", "issued", "created"):
        dp = ((item.get(k) or {}).get("date-parts") or [[None]])[0]
        if dp and dp[0]:
            return dp[0]
    return None


def title_sim(a: str, b: str) -> float:
    return SequenceMatcher(None, (a or "").lower(), (b or "").lower()).ratio()


def score_item(
    item: dict,
    query_title: str,
    query_author: Optional[str] = None,
    query_year: Optional[int] = None,
) -> Optional[float]:
    """给 Crossref 候选打分；应硬跳过的返回 None（纯函数，便于单测）。

    信号 = 标题相似度（主）+ 年份一致 + 一作姓一致 + 文献类型。
    首 hit 直接接受 + 单纯标题阈值是历史错配的根因，故改打分制。
    """
    t = _item_title(item)
    doi = item.get("DOI", "")
    typ = item.get("type", "")
    if typ in _HARD_SKIP_TYPES:
        return None
    rel = item.get("relation") or {}
    if "is-component-of" in rel:
        return None
    if is_derivative_literature(t, doi):
        return None
    sim = title_sim(query_title, t)
    if sim < 0.55:
        return None
    score = sim
    iy = _item_year(item)
    if query_year and iy:
        d = abs(iy - query_year)
        score += 0.15 if d == 0 else 0.05 if d == 1 else (-0.25 if d >= 3 else -0.10)
    if query_author:
        fams = {
            normalize_author(a.get("family", ""))
            for a in (item.get("author") or [])
        }
        fams.discard("")
        score += 0.15 if query_author in fams else -0.10
    if typ == "journal-article":
        score += 0.05
    elif typ in _SOFT_BAD_TYPES:
        score -= 0.15
    return score


def strip_jats(text: Optional[str]) -> str:
    """剥掉 Crossref abstract 的 JATS XML 标签（<jats:p> 等），还原纯文本。

    Crossref 的 abstract 字段是 JATS XML 片段，直接入库会把标签暴露给
    LIKE 检索与阅读器；剥标签 + html 反转义 + 空白折叠后才是可检索文本。
    """
    text = re.sub(r"<[^>]+>", " ", text or "")
    return " ".join(html.unescape(text).split())


class CrossrefClient(RateLimitedClient):
    service_name = "crossref"
    rps = 5.0  # polite pool

    def get_work(self, doi: str) -> Optional[dict]:
        """按 DOI 取 Crossref 记录（用于校验 PDF 首页/PII 抽到的候选 DOI）。"""
        resp = self.get(f"https://api.crossref.org/works/{quote(doi, safe='')}")
        if resp.status_code != 200:
            return None
        return resp.json().get("message") or None

    def search_best(
        self,
        title: str,
        author: Optional[str] = None,
        year: Optional[int] = None,
        rows: int = 10,
    ) -> Optional[tuple[dict, float]]:
        """打分制标题反查：top-N 候选逐一打分，返回 (item, score) 或 None。

        作者/年份（通常来自 Zotero 文件名）同时注入查询（改善排序）与事后打分
        （同名勘误/来信只能靠它们与原论文区分）。
        """
        params = {"query.bibliographic": title, "rows": rows}
        if author:
            params["query.author"] = author
        resp = self.get("https://api.crossref.org/works", params=params)
        if resp.status_code != 200:
            # 静默返 None 会让"本可命中的反查"看起来像是"没查到"（AUTHOR 2001
            # 全量导入时实测：并发高峰期偶发非 200，单篇重放即成功），必须留痕。
            log.warning("search_best HTTP %s (title=%.60r)", resp.status_code, title)
            return None
        items = (resp.json().get("message") or {}).get("items") or []
        best, best_score = None, 0.0
        for item in items:
            s = score_item(item, title, author, year)
            if s is not None and s > best_score:
                best, best_score = item, s
        if best is None or best_score < _ACCEPT_SCORE:
            return None
        if title_sim(title, _item_title(best)) < _MIN_TITLE_SIM:
            return None
        return best, best_score

    @staticmethod
    def extract_doi(item: dict) -> Optional[str]:
        return (item or {}).get("DOI")

    def fetch_abstract(self, doi: str) -> str:
        """按 DOI 取 Crossref abstract（JATS 剥标签还原纯文本）。

        复用 get_work 的限频与磁盘缓存；无 DOI / 查询失败 / 无 abstract
        一律返回空串（调用方按"未取到"降级，不区分原因）。
        """
        doi = (doi or "").strip()
        if not doi:
            return ""
        work = self.get_work(doi)
        if not work:
            return ""
        return strip_jats(work.get("abstract") or "")

    def fetch_bibtex(self, doi: str) -> Optional[str]:
        """通过 Crossref content negotiation 获取 BibTeX 纯文本。

        GET https://api.crossref.org/works/{doi}/transform/application/x-bibtex
        返回形如 ``@article{Smith_2023, doi = {10...}, ...}`` 的纯文本。

        走独立文本缓存（包装为 JSON {"bibtex": "..."}，复用 base 的 TTL 机制），
        不经过 base._request 的 JSON 缓存路径（纯文本响应无法被 resp.json() 缓存）。
        """
        doi = (doi or "").strip()
        if not doi:
            return None
        cache_key = self._cache_key("bibtex", doi)
        cached = self._cache_get(cache_key)
        if cached is not None and cached.get("bibtex"):
            return cached["bibtex"]

        url = f"https://api.crossref.org/works/{doi}/transform/application/x-bibtex"
        self._rate_limit()
        resp = self._session.get(
            url, timeout=30, headers={"Accept": "application/x-bibtex"}
        )
        if not resp.ok:
            return None
        text = (resp.text or "").strip()
        # 校验：合法 BibTeX 应以 @ 开头（防误把错误页 HTML 入库）
        if not text or not text.startswith("@"):
            return None
        self._cache_set(cache_key, {"bibtex": text})
        return text
