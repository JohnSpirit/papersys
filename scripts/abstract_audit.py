"""存量库摘要↔解析文本重合审计：全库分布 + 低分/灰区清单，标定 abstract_check 阈值。

papers.abstract 由 enrich 落库（OpenAlex -> Crossref -> Europe PMC 三源，
backend/workers/enrich_worker.py），解析文本走 backend/services/doc_text.py
的 read_doc_text（EN 覆盖层优先，其次 MinerU full.md）。重合比例低的四类
成因见 backend/services/abstract_check.py 模块注释：SI 附件、预印本↔正式版
措辞差异、graphical abstract / preview 版式、解析失败或 DOI 错配。清单里
的 title_hit 信号用于区分两类：标题在而摘要不在 -> 摘要版本差异或错配到
标题相近的衍生记录（勘误/评论）；标题也不在 -> SI 附件、不同论文或解析失败。

只读，不写库（--csv 只写指定文件）。

用法（项目 Python 3.12 环境）:
    python3 scripts/abstract_audit.py                        # 全量审计
    python3 scripts/abstract_audit.py --low 0.3 --gray 0.8   # 覆盖分档线
    python3 scripts/abstract_audit.py --ids <id...>           # 只审计指定 paper
    python3 scripts/abstract_audit.py --csv /tmp/audit.csv   # 全量明细写 CSV
"""
import argparse
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.db.conn import get_conn  # noqa: E402
from backend.pipeline.pdf_classify import filename_suggests_si  # noqa: E402
from backend.services.abstract_check import (  # noqa: E402
    GRAY_THRESHOLD, LOW_THRESHOLD, SHINGLE_SIZE,
    normalize_for_match, shingle_containment,
)
from backend.services.doc_text import read_doc_text  # noqa: E402


def _inbox_stems_by_paper() -> dict[int, str]:
    """paper_id -> 原始投递文件名 stem（tasks.payload.inbox_path；同 paper 取最新 task）。"""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id, paper_id, payload_json FROM tasks"
            " WHERE paper_id IS NOT NULL ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    stems: dict[int, str] = {}
    for t in rows:
        try:
            payload = json.loads(t["payload_json"])
        except (TypeError, ValueError):
            continue
        inbox = (payload or {}).get("inbox_path")
        if inbox:
            stems[t["paper_id"]] = Path(inbox).stem
    return stems


def _title_in_text(title: str, norm_text: str) -> bool:
    t = normalize_for_match(title)
    return len(t) >= 12 and t in norm_text


def audit(ids=None, low=None, gray=None, csv_path=None) -> None:
    low_t = LOW_THRESHOLD if low is None else low
    gray_t = GRAY_THRESHOLD if gray is None else gray

    conn = get_conn()
    try:
        sql = ("SELECT id, dir_name, doi, arxiv_id, title, year, first_author,"
               " abstract, parsed_path, parsed_chars FROM papers")
        if ids:
            sql += " WHERE id IN (%s) ORDER BY id" % ",".join("?" * len(ids))
            rows = conn.execute(sql, list(ids)).fetchall()
        else:
            rows = conn.execute(sql + " ORDER BY id").fetchall()
    finally:
        conn.close()

    stems = _inbox_stems_by_paper()

    n_no_abs = n_no_text = n_short = 0
    records: list[dict] = []
    for r in rows:
        paper = dict(r)
        abstract = paper.get("abstract") or ""
        if not abstract.strip():
            n_no_abs += 1
            continue
        text, source = read_doc_text(paper, "en")
        if not text.strip():
            n_no_text += 1
            continue
        ratio = shingle_containment(abstract, text)
        if ratio is None:
            n_short += 1
            continue
        norm_text = normalize_for_match(text)
        stem = stems.get(paper["id"]) or ""
        records.append({
            "id": paper["id"],
            "dir_name": paper["dir_name"],
            "doi": paper.get("doi") or "",
            "ratio": ratio,
            "abstract_words": len(normalize_for_match(abstract).split()),
            "title_hit": _title_in_text(paper.get("title") or "", norm_text),
            "si_file": bool(stem and filename_suggests_si(stem)),
            "preprint_doi": (paper.get("doi") or "").lower().startswith("10.1101/"),
            "has_arxiv": bool(paper.get("arxiv_id")),
            "parsed_chars": paper.get("parsed_chars") or 0,
            "text_source": source,
        })

    _report(records, total=len(rows), n_no_abs=n_no_abs, n_no_text=n_no_text,
            n_short=n_short, low=low_t, gray=gray_t)
    if csv_path:
        _write_csv(csv_path, records)
        print(f"\nCSV 明细 -> {csv_path}")


def _report(records: list[dict], *, total: int, n_no_abs: int, n_no_text: int,
            n_short: int, low: float, gray: float) -> None:
    n = len(records)
    print(f"\n══ 摘要重合审计（{total} 篇：可校验 {n}，跳过"
          f" 无摘要 {n_no_abs} / 无解析文本 {n_no_text} / 摘要过短 {n_short}）══")

    print(f"\n─ 重合比例分布（{SHINGLE_SIZE} 连词片段）─")
    buckets = [0] * 10
    for r in records:
        buckets[min(int(r["ratio"] * 10), 9)] += 1
    peak = max(1, max(buckets))
    for i, c in enumerate(buckets):
        bar = "█" * round(c * 30 / peak)
        print(f"  [{i / 10:.1f},{(i + 1) / 10:.1f})  {bar} {c}")

    matched = [r for r in records if r["ratio"] >= gray]
    gray_l = [r for r in records if low <= r["ratio"] < gray]
    low_l = [r for r in records if r["ratio"] < low]
    print(f"\n─ 分档（low < {low:.2f} <= gray < {gray:.2f} <= match）─")
    print(f"  match {len(matched)} / gray {len(gray_l)} / low {len(low_l)}"
          f"（of {n}）")

    for label, items in (("低分", low_l), ("灰区", gray_l)):
        print(f"\n─ {label}清单（{len(items)}，按比例升序）─")
        for r in sorted(items, key=lambda x: x["ratio"]):
            flags = [f"title={'Y' if r['title_hit'] else 'N'}"]
            if r["si_file"]:
                flags.append("si_fn")
            if r["preprint_doi"]:
                flags.append("preprint_doi")
            if r["has_arxiv"]:
                flags.append("arxiv_id")
            if r["text_source"] == "manual_edit":
                flags.append("en_override")
            print(f"  #{r['id']} ratio={r['ratio']:.2f} words={r['abstract_words']}"
                  f" chars={r['parsed_chars']} [{' '.join(flags)}]")
            print(f"      {r['dir_name']}  doi={r['doi'] or '-'}")


def _write_csv(path: str, records: list[dict]) -> None:
    if not records:
        return
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(records[0].keys()))
        w.writeheader()
        w.writerows(records)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ids", nargs="*", type=int, help="只审计指定 paper id")
    ap.add_argument("--low", type=float, help="低分线（默认取 abstract_check.LOW_THRESHOLD）")
    ap.add_argument("--gray", type=float, help="通过线（默认取 abstract_check.GRAY_THRESHOLD）")
    ap.add_argument("--csv", help="全量明细写 CSV（路径）")
    args = ap.parse_args()
    audit(ids=args.ids, low=args.low, gray=args.gray, csv_path=args.csv)


if __name__ == "__main__":
    main()
