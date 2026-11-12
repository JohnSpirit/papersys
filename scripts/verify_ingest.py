"""入库准确率验证：DB 记录 vs Zotero RDF ground truth。

用法：
    PYTHONPATH=. python3 scripts/verify_ingest.py groundtruth.json <task_id下界> [task_id上界]

join 链：tasks.payload.inbox_path basename -> ground truth basename（抽样时已排除冲突）
        tasks.paper_id -> papers 行
逐字段比对 DOI / year / first_author / title（相似度），并汇总流水线各状态列。
"""
import json
import os
import re
import sys
from difflib import SequenceMatcher

from backend.db.conn import get_conn


def norm_doi(d: str) -> str:
    d = (d or "").lower().strip()
    d = re.sub(r"^https?://(dx\.)?doi\.org/", "", d)
    return d


def doi_same(a: str, b: str) -> bool:
    """DOI 等价判定：除精确相等外，'_' 与 '/' 分隔符变体视为同一 DOI
    （如 10.1088/0954-898x/10/4/303 vs 10.1088/0954-898x_10_4_303）。"""
    a, b = norm_doi(a), norm_doi(b)
    return a == b or a.replace("_", "/") == b.replace("_", "/")


def title_sim(a: str, b: str) -> float:
    a = re.sub(r"\W+", " ", (a or "").lower()).strip()
    b = re.sub(r"\W+", " ", (b or "").lower()).strip()
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def main() -> None:
    gt_path, lo = sys.argv[1], int(sys.argv[2])
    hi = int(sys.argv[3]) if len(sys.argv) > 3 else lo
    gt = {os.path.basename(k): v for k, v in json.load(open(gt_path)).items()}

    conn = get_conn()
    tasks = conn.execute(
        "SELECT id, status, payload_json, paper_id FROM tasks WHERE id BETWEEN ? AND ?",
        (lo, hi)).fetchall()

    rows = []
    seen_files: set[str] = set()
    for t in reversed(tasks):  # 同一文件多次投喂时只保留最新一次 task
        payload = json.loads(t["payload_json"])
        inbox_path = payload.get("inbox_path", "")
        base = os.path.basename(inbox_path)
        # feed 撞名时 inbox 文件会加 _<hex> 后缀，剥掉再匹配
        base = re.sub(r"_[0-9a-f]{8}\.pdf$", ".pdf", base)
        if base in seen_files:
            continue
        seen_files.add(base)
        g = gt.get(base)
        p = None
        if t["paper_id"]:
            p = conn.execute("SELECT * FROM papers WHERE id = ?",
                             (t["paper_id"],)).fetchone()
            p = dict(p) if p else None
        rows.append({"task": t["id"], "task_status": t["status"],
                     "file": base, "gt": g, "paper": p})

    # ── 逐字段统计 ──
    # details 元素：(file, kind, gt值, db值, paper_id, dir_name)
    stats = {"doi": [0, 0, 0, 0],  # match / mismatch / db_missing / gt_missing
             "year": [0, 0], "author": [0, 0], "title_ok": 0}
    details = []
    status_count = {}
    for r in rows:
        g, p = r["gt"], r["paper"]
        for col in ("status_pdf", "status_parsed", "status_enrich", "status_embedded", "status_summary"):
            v = (p or {}).get(col) or ("task_" + r["task_status"])
            status_count[(col, v)] = status_count.get((col, v), 0) + 1
        pid = (p or {}).get("id")
        dname = (p or {}).get("dir_name") or ""
        if not g or not p:
            details.append((r["file"], "NO_GT" if not g else "NO_PAPER", "", "", pid, dname))
            continue
        gd, pd_ = norm_doi(g["doi"]), norm_doi(p.get("doi") or "")
        if gd and pd_:
            same = doi_same(gd, pd_)
            stats["doi"][0 if same else 1] += 1
            if not same:
                details.append((r["file"], "DOI_MISMATCH", gd, pd_, pid, dname))
        elif not gd:
            stats["doi"][3] += 1
            if pd_:
                details.append((r["file"], "DOI_EXTRA_IN_DB", "", pd_, pid, dname))
        else:
            stats["doi"][2] += 1
            details.append((r["file"], "DOI_MISSING_IN_DB", gd, "", pid, dname))
        stats["year"][0 if g["year"] == p.get("year") else 1] += 1
        if g["year"] != p.get("year"):
            details.append((r["file"], "YEAR", str(g["year"]), str(p.get("year")), pid, dname))
        ga = (g["first_author"] or "").lower()
        pa = re.sub(r"[^a-z]", "", (p.get("first_author") or "").lower())
        stats["author"][0 if ga and ga == pa else 1] += 1
        if ga != pa:
            details.append((r["file"], "AUTHOR", ga, pa, pid, dname))
        sim = title_sim(g["title"], p.get("title") or "")
        if sim >= 0.8:
            stats["title_ok"] += 1
        else:
            details.append((r["file"], f"TITLE sim={sim:.2f}",
                            g["title"], (p.get("title") or ""), pid, dname))

    n = len(rows)
    print(f"\n══ 试点验证报告（tasks {lo}-{hi}，共 {n} 个）══")
    print("\n─ 流水线状态分布 ─")
    for (col, v), cnt in sorted(status_count.items()):
        print(f"  {col:18s} {v:12s} × {cnt}")
    dm, dmm, dmiss, dgt = stats["doi"]
    both = dm + dmm
    print("\n─ 元数据准确率 ─")
    print(f"  DOI:   双方都有 {both} 条，一致 {dm}（{dm/max(both,1):.0%}），冲突 {dmm}；"
          f"DB 缺失 {dmiss}；GT 缺失 {dgt}")
    # 冲突细分：与 markdown 报告共用 _classify_doi_mismatch 口径
    # （SI 后缀派生 / 预印本↔正式版 / 疑似真错配）
    mm = [d for d in details if d[1] == "DOI_MISMATCH" and d[2] and d[3]]
    buckets = {"si": 0, "version": 0, "wrong": 0}
    for d in mm:
        buckets[_classify_doi_mismatch(d[2], d[3])] += 1
    print(f"         冲突细分：SI 后缀派生 {buckets['si']} 条（文件本身是附件，DB DOI 对文件正确）；"
          f"预印本↔正式版 {buckets['version']} 条；疑似真错配 {buckets['wrong']} 条")
    print(f"  year:  一致 {stats['year'][0]}/{sum(stats['year'])}"
          f"（±1  epub/print 差异 {sum(1 for d in details if d[1]=='YEAR' and d[2] and d[3] and abs(int(d[2])-int(d[3]))==1)} 条）")
    print(f"  作者:  一致 {stats['author'][0]}/{sum(stats['author'])}")
    print(f"  title: 相似度≥0.8 计 {stats['title_ok']}/{n}")
    if details:
        print("\n─ 异常明细 ─")
        for d in details:
            f, kind, a, b = d[0], d[1], d[2], d[3]
            print(f"  [{kind}] {f}")  # 完整文件名（重驱脚本按此解析，勿截断）
            if a or b:
                print(f"      Zotero: {a}\n      入库:   {b}")

    # ── Markdown 详细报告（可选） ──
    if "--md" in sys.argv:
        md_path = sys.argv[sys.argv.index("--md") + 1]
        _write_markdown(md_path, rows, details, stats, status_count, lo, hi)
        print(f"\nMarkdown 报告 -> {md_path}")


def _classify_doi_mismatch(gt_doi: str, db_doi: str) -> str:
    if db_doi.startswith(gt_doi):
        return "si"        # DB DOI = GT DOI + .sNNN 后缀：文件本身是 SI 附件
    if "10.1101/" in gt_doi or "10.1101/" in db_doi:
        return "version"   # 预印本 ↔ 正式发表版
    return "wrong"         # 疑似真错配


def _write_markdown(path: str, rows: list, details: list,
                    stats: dict, status_count: dict, lo: int, hi: int) -> None:
    import datetime
    n = len(rows)
    dm, dmm, dmiss, dgt = stats["doi"]
    both = dm + dmm
    si = [d for d in details if d[1] == "DOI_MISMATCH" and _classify_doi_mismatch(d[2], d[3]) == "si"]
    ver = [d for d in details if d[1] == "DOI_MISMATCH" and _classify_doi_mismatch(d[2], d[3]) == "version"]
    wrong = [d for d in details if d[1] == "DOI_MISMATCH" and _classify_doi_mismatch(d[2], d[3]) == "wrong"]
    miss = [d for d in details if d[1] == "DOI_MISSING_IN_DB"]
    years = [d for d in details if d[1] == "YEAR"]
    authors = [d for d in details if d[1] == "AUTHOR"]
    titles = [d for d in details if d[1].startswith("TITLE")]
    others = [d for d in details if d[1] in ("NO_GT", "NO_PAPER", "DOI_EXTRA_IN_DB")]

    def entry(d, fields=("doi",)) -> str:
        f, _, a, b, pid, dname = d
        loc = f"paper #{pid} `{dname}`" if pid else "（未入库）"
        out = [f"- **{f}**\n  - 位置：{loc}"]
        if a:
            out.append(f"  - Zotero：`{a}`")
        if b:
            out.append(f"  - 入库：`{b}`")
        return "\n".join(out)

    L = []
    L.append("# Zotero 全库导入验证报告")
    L.append(f"\n生成时间：{datetime.datetime.now():%Y-%m-%d %H:%M}；"
             f"范围：tasks {lo}–{hi}，去重后 {n} 个文件；"
             f"基准：Zotero RDF 导出（ground truth）")
    L.append("\n## 总览\n")
    L.append("| 指标 | 结果 |")
    L.append("|---|---|")
    L.append(f"| DOI 精确一致 | {dm}/{both}（{dm/max(both,1):.0%}） |")
    L.append(f"| ├ SI 后缀派生（文件本身是附件） | {len(si)} |")
    L.append(f"| ├ 预印本↔正式版 | {len(ver)} |")
    L.append(f"| └ 疑似真错配 | {len(wrong)} |")
    L.append(f"| DB 缺失 DOI | {dmiss} |")
    L.append(f"| 年份一致 | {stats['year'][0]}/{sum(stats['year'])} |")
    L.append(f"| 作者一致 | {stats['author'][0]}/{sum(stats['author'])} |")
    L.append(f"| 标题相似度≥0.8 | {stats['title_ok']}/{n} |")
    L.append("\n## 流水线状态\n")
    L.append("| 状态列 | 值 | 数量 |")
    L.append("|---|---|---|")
    for (col, v), cnt in sorted(status_count.items()):
        L.append(f"| {col} | {v} | {cnt} |")

    sections = [
        ("疑似真错配（需人工复核 DOI）", wrong),
        ("预印本 ↔ 正式版（同一工作不同版本，通常无需处理）", ver),
        ("SI 附件（文件本身是 Supporting Information，入库 DOI 对文件正确）", si),
        ("DB 缺失 DOI", miss),
        ("年份不一致", years),
        ("作者不一致", authors),
        ("标题低相似", titles),
        ("其他（GT 缺失 / paper 链接残留 / DB 多出 DOI）", others),
    ]
    for i, (title, items) in enumerate(sections, 1):
        if not items:
            continue
        L.append(f"\n## {i}. {title}（{len(items)} 篇）\n")
        for d in items:
            L.append(entry(d))
    from pathlib import Path as _P
    _P(path).write_text("\n".join(L) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
