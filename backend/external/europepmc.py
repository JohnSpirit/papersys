"""Europe PMC REST client（abstract 第三源）。

背景（2026-09-08 实测）：OpenAlex 与 Crossref 对 Springer Nature / Cell Press 系
期刊普遍无 abstract（15/15 抽样双源皆空，原始 API 确认字段缺失，非客户端问题）；
Europe PMC（PubMed 的开放 REST）对同批 DOI 全部有 abstractText。enrich worker 与
scripts/backfill_abstracts.py 在 OpenAlex -> Crossref 之后追加本源。

端点：GET /webservices/rest/search?query=...&format=json&resultType=core
（resultType=core 才含 abstractText）。免 key；官方无硬性配额，礼貌限速见
config.toml [external].europepmc_rps。标题反查仅用于补 abstract，不写
DOI/arxiv_id（红线 6 的允许来源为 Crossref/OpenAlex 反查与手动录入）。
"""
from difflib import SequenceMatcher
from typing import Optional
from urllib.parse import quote

from backend.config import CONFIG
from backend.external.base import RateLimitedClient
from backend.external.crossref import strip_jats

_MIN_TITLE_SIM = 0.90


def _title_similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, (a or "").lower(), (b or "").lower()).ratio()


class EuropePmcClient(RateLimitedClient):
    service_name = "europepmc"
    rps = CONFIG["external"]["europepmc_rps"]

    BASE = "https://www.ebi.ac.uk/europepmc/webservices/rest"

    def _search(self, query: str, page_size: int = 5) -> list[dict]:
        url = (f"{self.BASE}/search?query={quote(query)}"
               f"&format=json&resultType=core&pageSize={page_size}")
        resp = self.get(url)
        if resp.status_code != 200:
            return []
        return resp.json().get("resultList", {}).get("result") or []

    def get_work_by_doi(self, doi: str) -> dict:
        """按 DOI 精确查询，返回首个结果（无命中返回空 dict）。"""
        doi = (doi or "").strip()
        if not doi:
            return {}
        rows = self._search(f'DOI:"{doi}"', page_size=1)
        return rows[0] if rows else {}

    def fetch_abstract(self, doi: str) -> str:
        """按 DOI 取 abstract（剥残留标记还原纯文本；无 DOI/无命中/无摘要返回空串）。"""
        work = self.get_work_by_doi(doi)
        if not work:
            return ""
        return strip_jats(work.get("abstractText") or "")

    def search_work_by_title(self, title: str, year: Optional[int] = None,
                             author: Optional[str] = None) -> dict:
        """打分制标题反查（供无标识符论文补 abstract）。

        采纳条件：标题相似度 >= 0.90，且（年份都已知时差值 <= 1），且
        （作者都已知时第一作者姓出现在 authorString）。任一不满足则试下一候选；
        全部不满足返回空 dict——宁可漏配不错配（abstract 错配会污染 LIKE 检索）。
        """
        title = (title or "").strip()
        if not title:
            return {}
        for w in self._search(f'TITLE:"{title}"', page_size=5):
            if _title_similarity(title, w.get("title") or "") < _MIN_TITLE_SIM:
                continue
            if year and w.get("pubYear"):
                try:
                    if abs(int(w["pubYear"]) - int(year)) > 1:
                        continue
                except (TypeError, ValueError):
                    pass
            if author and w.get("authorString"):
                if author.lower() not in w["authorString"].lower():
                    continue
            return w
        return {}

    @staticmethod
    def extract_abstract(work: dict) -> str:
        """从 work 对象提取纯文本 abstract（无返回空串）。"""
        return strip_jats((work or {}).get("abstractText") or "")
