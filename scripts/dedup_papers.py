"""同标题重复入库检测与清理建议。

重复组的主要来源：「旧迁移记录无 DOI + 新入库记录有 DOI」造成的同论文双份入库。
检索层已做查询侧过滤（semantic_search 按内容状态排除 manual_hold + 标题去重，
search_library 同），本脚本处理存量数据的物理清理。

保留方规则（组内按元组排序取最大）：
  1. 非 manual_hold 优先（任意状态列 held 即降级）
  2. 非预印本优先（is_preprint_record：journal 含 bioRxiv/arXiv/medRxiv 或
     DOI 前缀 10.1101；同标题 preprint/正式版双版本场景）
  3. 有 DOI 优先
  4. status_parsed='ok' 优先
  5. 低 id 优先（更早入库）
删除安全性：DROP 候选满足内容状态 held / 无 DOI / 与保留方 DOI 相同 之一才可自动删；
status_enrich 的 manual_hold 只表示元数据失败，不单独构成自动删除依据；
组内存在不同非空 DOI（可能是 preprint 与正式版双版本）时整组标 REVIEW，不给
自动删除建议。

用法（项目 Python 3.12 环境）:
    python3 scripts/dedup_papers.py            # 检测 + 建议（不删任何东西）
    python3 scripts/dedup_papers.py --apply    # 删除全部 safe DROP（级联删关联表 + library 目录，不可逆）
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from backend.db.conn import get_conn  # noqa: E402

_STATUS_COLS = ("status_pdf", "status_parsed", "status_enrich", "status_translated",
                "status_summary", "status_outline", "status_embedded")
_AUTO_DROP_HOLD_COLS = tuple(c for c in _STATUS_COLS if c != "status_enrich")


def _norm_title(t: str | None) -> str:
    return " ".join((t or "").lower().split())


def _load_groups() -> list[list[dict]]:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id, dir_name, title, doi, year, journal, status_parsed,"
            " status_pdf, status_enrich, status_translated, status_summary, status_outline, status_embedded"
            " FROM papers ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    groups: dict[str, list[dict]] = {}
    for r in rows:
        t = _norm_title(r["title"])
        if t:
            groups.setdefault(t, []).append(dict(r))
    return [g for g in groups.values() if len(g) > 1]


def _keep_score(p: dict) -> tuple:
    from backend.db.repo import is_preprint_record
    held = any(p[c] == "manual_hold" for c in _STATUS_COLS)
    preprint = is_preprint_record(p.get("journal"), p.get("doi"))
    return (0 if held else 1, 0 if preprint else 1, 1 if p["doi"] else 0,
            1 if p["status_parsed"] == "ok" else 0, -p["id"])


def _safe_to_drop(drop: dict, keep: dict) -> bool:
    if any(drop[c] == "manual_hold" for c in _AUTO_DROP_HOLD_COLS):
        return True
    if not drop["doi"]:
        return True
    return bool(keep["doi"]) and drop["doi"].lower() == keep["doi"].lower()


def analyze() -> list[dict]:
    """返回 [{group: [papers], keep: paper, drops: [(paper, safe)], review: bool}]。"""
    out = []
    for g in _load_groups():
        keep = max(g, key=_keep_score)
        drops = [(p, _safe_to_drop(p, keep)) for p in g if p["id"] != keep["id"]]
        dois = {(p["doi"] or "").lower() for p in g} - {""}
        review = len(dois) > 1 or any(not safe for _, safe in drops)
        out.append({"group": g, "keep": keep, "drops": drops, "review": review})
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="删除全部 safe DROP（不可逆；REVIEW 组不受影响）")
    args = ap.parse_args()

    groups = analyze()
    if not groups:
        print("无同标题重复组")
        return

    safe_ids: list[int] = []
    review_n = 0
    for g in groups:
        keep, drops, review = g["keep"], g["drops"], g["review"]
        review_n += review
        print(f"\n== {keep['title']!r}")
        print(f"  KEEP  #{keep['id']:<4d} doi={keep['doi'] or '-':40s} year={keep['year']}"
              f" parsed={keep['status_parsed']} dir={keep['dir_name']}")
        for p, safe in drops:
            tag = "DROP " if safe and not review else "REVIEW"
            print(f"  {tag}  #{p['id']:<4d} doi={p['doi'] or '-':40s} year={p['year']}"
                  f" parsed={p['status_parsed']} dir={p['dir_name']}")
            if safe and not review:
                safe_ids.append(p["id"])
        if review:
            print("  -> REVIEW：组内存在不同非空 DOI 或无法自动裁决，需人工确认")

    print(f"\n共 {len(groups)} 组；safe DROP {len(safe_ids)} 篇；REVIEW 组 {review_n} 个")
    if not args.apply:
        if safe_ids:
            print(f"确认后执行: python3 scripts/dedup_papers.py --apply"
                  f"（将删除 ids: {safe_ids}）")
        return
    if not safe_ids:
        print("无可自动删除的条目")
        return
    from delete_papers import delete_one
    ok = fail = 0
    for pid in safe_ids:
        try:
            r = delete_one(pid)
        except Exception as e:  # noqa: BLE001
            r = {"ok": False, "msg": f"exception: {e}"}
        print(("OK " if r["ok"] else "FAIL") + f" #{pid}: {r['msg']}")
        ok += r["ok"]
        fail += not r["ok"]
    print(f"\n{ok} deleted, {fail} failed")
    sys.exit(1 if fail else 0)


if __name__ == "__main__":
    main()
