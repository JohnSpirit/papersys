"""存量论文 abstract 一次性回填。

对 abstract 为空的论文依次尝试 OpenAlex（DOI / openalex_id）、Crossref（DOI）、
Europe PMC（DOI）；三源皆空且无标识符时，用 Europe PMC 打分制标题反查（标题
相似度 >= 0.90 + 年份 ±1 + 第一作者姓出现在 authorString，任一不满足即弃）
补 abstract——OpenAlex/Crossref 对 Springer Nature / Cell Press 系期刊普遍无
abstract，Europe PMC 对同批 DOI 有覆盖。
标题反查只写 abstract，不写 DOI/arxiv_id（红线 6 的允许来源不含 Europe PMC）。
命中则写 papers.abstract 并同步 papers_fts。断点续传：重跑自动跳过已回填。

用法（项目 Python 3.12 环境）:
    python3 scripts/backfill_abstracts.py --dry-run       # 只打印来源与长度，不写库
    python3 scripts/backfill_abstracts.py                 # 全量回填
    python3 scripts/backfill_abstracts.py --limit 20      # 只处理前 20 篇
    python3 scripts/backfill_abstracts.py --ids <id...>   # 指定 paper id

限频由 client 自身保证（OpenAlex 5 req/s、Crossref polite pool、Europe PMC 3 req/s，
24h 磁盘缓存）。
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.db.conn import get_conn  # noqa: E402
from backend.db.repo import update_fts  # noqa: E402
from backend.external.crossref import CrossrefClient  # noqa: E402
from backend.external.europepmc import EuropePmcClient  # noqa: E402
from backend.external.openalex import OpenAlexClient  # noqa: E402


def _fetch_abstract(paper, oac: OpenAlexClient, cc: CrossrefClient,
                    epmc: EuropePmcClient) -> tuple[str, str]:
    """多源尝试取 abstract。返回 (abstract, source)；未取到时 abstract 为空串。

    库内无 DOI 的论文借用 OpenAlex work 对象自带的 DOI 走 Crossref / Europe PMC
    第二、三源。
    """
    work = {}
    if paper["doi"]:
        work = oac.get_work(paper["doi"])
    if not work and paper["openalex_id"]:
        work = oac.get_work_by_openalex_id(paper["openalex_id"])
    doi = paper["doi"]
    if work:
        info = oac.extract_work_info(work)
        if info.get("abstract"):
            return info["abstract"], "openalex"
        doi = doi or info.get("doi")
    if doi:
        ab = cc.fetch_abstract(doi)
        if ab:
            return ab, "crossref"
        ab = epmc.fetch_abstract(doi)
        if ab:
            return ab, "europepmc"
    if not (paper["doi"] or paper["openalex_id"]):
        # 三源皆空且无任何标识符：Europe PMC 打分制标题反查（只写 abstract）
        work = epmc.search_work_by_title(
            paper["title"] or "", year=paper["year"], author=paper["first_author"],
        )
        if work:
            ab = epmc.extract_abstract(work)
            if ab:
                return ab, "europepmc_title"
    return "", ""


def backfill(dry_run=False, limit=None, ids=None):
    if ids:
        placeholders = ",".join("?" * len(ids))
        where = f"id IN ({placeholders}) AND (abstract IS NULL OR abstract = '')"
        params = list(ids)
    else:
        where = "abstract IS NULL OR abstract = ''"
        params = []
    conn = get_conn()
    try:
        rows = conn.execute(
            f"SELECT id, dir_name, doi, openalex_id, title, year, first_author"
            f" FROM papers WHERE {where} ORDER BY id",
            params,
        ).fetchall()
    finally:
        conn.close()
    if limit:
        rows = rows[:limit]
    if not rows:
        print("0 paper(s) with empty abstract")
        return

    print(f"{len(rows)} paper(s) with empty abstract")
    oac = OpenAlexClient()
    cc = CrossrefClient()
    epmc = EuropePmcClient()
    ok = miss = 0
    for r in rows:
        try:
            abstract, source = _fetch_abstract(r, oac, cc, epmc)
        except Exception as e:  # noqa: BLE001 — 单篇失败不中断整批
            abstract, source = "", ""
            print(f"ERROR #{r['id']} {r['dir_name']}: {e}")
        if not abstract:
            miss += 1
            print(f"MISS  #{r['id']} {r['dir_name']} (no abstract from openalex/crossref/europepmc)")
            continue
        ok += 1
        prefix = "DRYOK " if dry_run else "OK    "
        print(f"{prefix}#{r['id']} {r['dir_name']} <- {source} ({len(abstract)} chars)")
        if dry_run:
            continue
        conn = get_conn()
        try:
            conn.execute("BEGIN")
            conn.execute(
                "UPDATE papers SET abstract = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (abstract, r["id"]),
            )
            update_fts(conn, r["id"], title=r["title"] or "", abstract=abstract)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
    print(f"\n{ok} backfilled, {miss} still empty (of {len(rows)})")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="只打印来源与长度，不写库")
    ap.add_argument("--limit", type=int, help="最多处理 N 篇")
    ap.add_argument("--ids", nargs="*", type=int, help="指定 paper id（默认全部空 abstract 论文）")
    args = ap.parse_args()
    backfill(dry_run=args.dry_run, limit=args.limit, ids=args.ids)


if __name__ == "__main__":
    main()
