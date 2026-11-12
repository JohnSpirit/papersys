"""OpenAlex REST API client — 元数据富化 / 引文网络。"""
import json
import re
from difflib import SequenceMatcher
from typing import Optional

from backend.external.base import RateLimitedClient
from backend.config import CONFIG
from backend.pipeline.pdf_classify import is_derivative_literature, normalize_author

# paratext = OA 对勘误/社论/SI 等附属材料的归类；dataset/reference-entry 同样非原论文。
# book-chapter/dissertation 只软惩罚（全量实测书章/学位论文抢位各多例）。
_OA_HARD_SKIP_TYPES = {"paratext", "dataset", "reference-entry", "standard"}
_OA_SOFT_BAD_TYPES = {"book-chapter", "book", "dissertation", "proceedings"}

# 与 crossref.score_item 同一套采纳线（两处独立实现，避免客户端互相依赖）
_ACCEPT_SCORE = 0.75
_MIN_TITLE_SIM = 0.60


def score_work(
    work: dict,
    query_title: str,
    query_author: Optional[str] = None,
    query_year: Optional[int] = None,
) -> Optional[float]:
    """给 OpenAlex 搜索候选打分；应硬跳过的返回 None（纯函数，便于单测）。"""
    t = work.get("title") or ""
    doi = (work.get("doi") or "").replace("https://doi.org/", "")
    typ = work.get("type") or ""
    if not t or typ in _OA_HARD_SKIP_TYPES:
        return None
    if is_derivative_literature(t, doi):
        return None
    sim = SequenceMatcher(None, (query_title or "").lower(), t.lower()).ratio()
    if sim < 0.55:
        return None
    score = sim
    wy = work.get("publication_year")
    if query_year and wy:
        d = abs(wy - query_year)
        score += 0.15 if d == 0 else 0.05 if d == 1 else (-0.25 if d >= 3 else -0.10)
    if query_author:
        fams = set()
        for a in work.get("authorships") or []:
            name = (a.get("author") or {}).get("display_name") or ""
            if name.split():
                fams.add(normalize_author(name.split()[-1]))
        fams.discard("")
        score += 0.15 if query_author in fams else -0.10
    if typ in ("article", "preprint", "journal-article"):
        score += 0.05
    elif typ in _OA_SOFT_BAD_TYPES:
        score -= 0.15
    return score


class OpenAlexClient(RateLimitedClient):
    service_name = "openalex"
    rps = CONFIG["external"]["openalex_rps"]

    def __init__(self) -> None:
        super().__init__()
        self._api_key = CONFIG["secrets"].get("openalex_api_key", "")

    def _url(self, path: str) -> str:
        """构造完整 URL，并将 api_key 合并到 query string。"""
        if not self._api_key:
            return f"https://api.openalex.org{path}"
        sep = "&" if "?" in path else "?"
        return f"https://api.openalex.org{path}{sep}api_key={self._api_key}"

    def get_work(self, doi: str) -> dict:
        """通过 DOI 查询论文；404 表示无记录，其他非 200 响应抛出错误。"""
        resp = self.get(self._url(f"/works/doi:{doi}"))
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code == 404:
            return {}
        raise RuntimeError(f"OpenAlex {resp.status_code}: {resp.text[:200]}")

    def get_work_by_openalex_id(self, openalex_id: str) -> dict:
        """通过 OpenAlex W-id 查询。"""
        wid = openalex_id.replace("https://openalex.org/", "")
        resp = self.get(self._url(f"/works/{wid}"))
        if resp.status_code == 200:
            return resp.json()
        return {}

    def get_references_batch(self, work_ids: list[str]) -> dict[str, dict]:
        """批量查询 W-id → 详细信息（仅取关键字段）。每次最多 50。"""
        result: dict[str, dict] = {}
        i = 0
        while i < len(work_ids):
            batch = work_ids[i : i + 50]
            ids_str = "|".join(b.replace("https://openalex.org/", "") for b in batch)
            path = (
                f"/works?filter=openalex_id:{ids_str}"
                f"&per_page=50&select=id,doi,title,publication_year,cited_by_count,authorships"
            )
            resp = self.get(self._url(path), timeout=30)
            if resp.status_code != 200:
                i += 50
                continue
            for w in resp.json().get("results") or []:
                result[w["id"]] = w
            i += 50
        return result

    def get_cited_by_dois_batch(self, dois: list[str]) -> dict[str, dict]:
        """批量按 DOI 查 cited_by_count（db audit-citations 对账用）。

        返回 {小写 doi: {"openalex_id": str, "cited_by_count": int}}。
        OpenAlex 对批内查不到的 DOI 不返回条目——调用方据此识别库内坏 DOI。
        每批 50 个，超出自动分批；任何非 200 响应都直接抛出，避免把请求失败
        误报成 oa_missing。
        """
        from urllib.parse import quote
        result: dict[str, dict] = {}
        cleaned = [d.strip() for d in dois if d and d.strip()]
        i = 0
        while i < len(cleaned):
            chunk = cleaned[i : i + 50]
            # OpenAlex 管道过滤语法：filter 键只出现一次，值用 | 连接
            # （doi:a|doi:b 会报 400 "OR query between filters is not supported"）
            filt = "doi:" + "|".join(quote(d, safe="/") for d in chunk)
            path = f"/works?filter={filt}&per_page=50&select=id,doi,cited_by_count"
            resp = self.get(self._url(path), timeout=30)
            if resp.status_code != 200:
                raise RuntimeError(f"OpenAlex {resp.status_code}: {resp.text[:200]}")
            for w in resp.json().get("results") or []:
                doi = (w.get("doi") or "").replace("https://doi.org/", "").lower()
                if doi:
                    result[doi] = {
                        "openalex_id": w.get("id", ""),
                        "cited_by_count": w.get("cited_by_count", 0),
                    }
            i += 50
        return result

    def search_work(
        self,
        title: str,
        year: Optional[int] = None,
        author: Optional[str] = None,
    ) -> dict:
        """打分制标题搜索：top-10 候选逐一打分（标题+年份+作者+类型），
        过采纳线才返回，否则空 dict。year/author 是软信号（epub/print 年差常见），
        不再做硬 filter——硬过滤曾把正式版挡在门外、让同名衍生记录上位。"""
        q = title.replace(" ", "+")
        url = self._url(f"/works?search={q}&per_page=10")
        resp = self.get(url)
        if resp.status_code != 200:
            return {}
        results = resp.json().get("results", [])
        best, best_score = None, 0.0
        for w in results:
            s = score_work(w, title, author, year)
            if s is not None and s > best_score:
                best, best_score = w, s
        if best is None or best_score < _ACCEPT_SCORE:
            return {}
        if self.title_similarity(title, best.get("title") or "") < _MIN_TITLE_SIM:
            return {}
        return best

    def search_work_by_author_year(self, author: str, year: int) -> dict:
        q = author.replace(" ", "+")
        url = self._url(f"/works?filter=authorships.author.display_name:{q},publication_year:{year}&per_page=5")
        resp = self.get(url)
        if resp.status_code != 200:
            return {}
        data = resp.json()
        results = data.get("results", [])
        return results[0] if results else {}

    def search_papers(
        self,
        query: str,
        *,
        per_page: int = 20,
        year_from: Optional[int] = None,
        year_to: Optional[int] = None,
    ) -> list[dict]:
        """搜索论文列表，用于 Discover 页。"""
        parts = [f"/works?search={query.replace(' ', '+')}&per_page={min(per_page, 50)}"]
        if year_from or year_to:
            yf = year_from or 1900
            yt = year_to or 2100
            parts.append(f"&filter=publication_year:{yf}-{yt}")
        url = self._url("".join(parts))
        resp = self.get(url, timeout=30)
        if resp.status_code != 200:
            return []
        data = resp.json()
        return [_work_to_search_record(w) for w in (data.get("results") or [])]

    @staticmethod
    def extract_work_info(work: dict) -> dict:
        """从 OpenAlex work 对象提取精简字段。"""
        if not work:
            return {}
        primary = work.get("primary_location") or {}
        source = primary.get("source") or {}
        # 源文档链接（OpenReview 论坛页 / arXiv abs 页等）。doi.org 跳转链与 doi
        # 字段重复不存（RIS UR 优先级 arxiv_id -> url -> doi，存了也恒被覆盖）；
        # http 统一升 https
        landing = (primary.get("landing_page_url") or "").strip()
        if landing.startswith(("https://doi.org/", "http://doi.org/")):
            landing = ""
        elif landing.startswith("http://"):
            landing = "https://" + landing[len("http://"):]
        oa = work.get("open_access") or {}
        concepts = [
            {"name": c["display_name"], "level": c["level"], "score": c["score"]}
            for c in (work.get("concepts") or [])
        ]
        authors = [
            a.get("author", {}).get("display_name", "") for a in work.get("authorships") or []
        ]
        doi = (work.get("doi") or "").replace("https://doi.org/", "")

        # arXiv ID: 从 locations[].landing_page_url 提取
        arxiv_id = None
        for loc in work.get("locations") or []:
            lp_url = (loc.get("landing_page_url") or "").lower()
            m = re.search(r"arxiv\.org/abs/(\d{4}\.\d{4,5}(?:v\d+)?)", lp_url)
            if m:
                arxiv_id = m.group(1)
                break

        return {
            "doi": doi,
            "arxiv_id": arxiv_id,
            "openalex_id": work.get("id", ""),
            "url": landing or None,
            "title": work.get("title", ""),
            "authors": json.dumps(authors, ensure_ascii=False),
            "journal": source.get("display_name") if source else None,
            "pub_type": work.get("type", "article"),
            "oa_status": oa.get("oa_status", "unknown"),
            "cited_by_count": work.get("cited_by_count", 0),
            "concepts": json.dumps(concepts, ensure_ascii=False),
            "abstract": _reconstruct_abstract(work.get("abstract_inverted_index")),
            "pub_year": work.get("publication_year"),
        }

    @staticmethod
    def title_similarity(a: str, b: str) -> float:
        """简易标题相似度。"""
        import difflib
        return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _work_to_search_record(w: dict) -> dict:
    """OpenAlex /works 列表项 → 统一 search 记录格式。"""
    if not w:
        return {}
    source = (w.get("primary_location") or {}).get("source") or {}
    doi = (w.get("doi") or "").replace("https://doi.org/", "")
    authors = [a.get("author", {}).get("display_name", "") for a in (w.get("authorships") or [])]

    # arXiv ID 提取（与 enrich worker 一致）
    arxiv_id = None
    for loc in w.get("locations") or []:
        url = (loc.get("landing_page_url") or "").lower()
        m = re.search(r"arxiv\.org/abs/(\d{4}\.\d{4,5}(?:v\d+)?)", url)
        if m:
            arxiv_id = re.sub(r"v\d+$", "", m.group(1))
            break

    # PDF 链接（优先 OA）
    pdf_url = None
    for loc in w.get("locations") or []:
        if loc.get("is_oa") and loc.get("pdf_url"):
            pdf_url = loc["pdf_url"]
            break

    return {
        "source":         "openalex",
        "id":             w.get("id", "").replace("https://openalex.org/", ""),
        "title":          w.get("title") or "",
        "authors":        authors,
        "year":           w.get("publication_year"),
        "abstract":       _reconstruct_abstract(w.get("abstract_inverted_index")),
        "doi":            doi or None,
        "arxiv_id":       arxiv_id,
        "url":            doi and f"https://doi.org/{doi}" or w.get("id"),
        "pdf_url":        pdf_url,
        "journal":        source.get("display_name") if source else None,
        "cited_by_count": w.get("cited_by_count"),
        "oa_status":      (w.get("open_access") or {}).get("oa_status"),
    }


def _reconstruct_abstract(inverted_index: Optional[dict]) -> str:
    """OpenAlex 摘要存为 inverted index，重组为文本。"""
    if not inverted_index:
        return ""
    positions: list[tuple[int, str]] = []
    for word, idxs in inverted_index.items():
        for i in idxs:
            positions.append((i, word))
    positions.sort(key=lambda x: x[0])
    return " ".join(w for _, w in positions)