"""按 paper id 删除论文：清 DB 行（含全部关联表）+ 删归档目录，不退回 inbox、不重投。

用于 DOI 重复组去重、无效记录清理。与 redrive_papers.py 的区别：纯删除。

用法：
    python3 scripts/delete_papers.py <id...>        # 按 paper id
    python3 scripts/delete_papers.py --dry-run <id> # 只打印将执行的动作

注意：删除不可逆（library 目录、translations/summaries/embeddings/annotations/
doc_revisions/tasks 全部清除）；快照文件（数据根目录下 doc_snapshots/）保留。
"""
import argparse
import shutil
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.config import CONFIG, resolve_path  # noqa: E402
from backend.db.conn import get_conn  # noqa: E402


def delete_one(paper_id: int, *, dry_run: bool = False) -> dict:
    conn = get_conn()
    try:
        conn.row_factory = sqlite3.Row
        paper = conn.execute(
            "SELECT id, dir_name, pdf_path, doi FROM papers WHERE id = ?",
            (paper_id,),
        ).fetchone()
    finally:
        conn.close()
    if not paper:
        return {"paper_id": paper_id, "ok": False, "msg": "paper not found"}

    pdf_abs = resolve_path(paper["pdf_path"])
    pdf_dir = Path(pdf_abs).parent if pdf_abs else None
    library = Path(CONFIG["paths"]["library_root"]).resolve()
    if pdf_dir is not None:
        if library not in pdf_dir.resolve().parents:
            return {"paper_id": paper_id, "ok": False,
                    "msg": f"pdf dir not under library_root, refuse to remove: {pdf_dir}"}

    if dry_run:
        return {"paper_id": paper_id, "ok": True, "dry": True,
                "msg": f"would delete rows + {pdf_dir} (was {paper['dir_name']}, doi={paper['doi']})"}

    conn = get_conn()
    try:
        conn.execute("BEGIN")
        for table in ("translations", "summaries", "embeddings", "annotations",
                      "doc_revisions", "papers_fts", "tasks"):
            conn.execute(f"DELETE FROM {table} WHERE paper_id = ?", (paper_id,))
        conn.execute("DELETE FROM papers WHERE id = ?", (paper_id,))
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()

    if pdf_dir is not None and pdf_dir.exists():
        shutil.rmtree(pdf_dir)
    return {"paper_id": paper_id, "ok": True,
            "msg": f"deleted (was {paper['dir_name']}, doi={paper['doi']})"}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("ids", nargs="*", type=int, help="paper ids")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if not args.ids:
        ap.error("no paper ids given")

    ok = fail = 0
    for pid in args.ids:
        try:
            r = delete_one(pid, dry_run=args.dry_run)
        except Exception as e:  # noqa: BLE001
            r = {"paper_id": pid, "ok": False, "msg": f"exception: {e}"}
        print(("OK " if r["ok"] else "FAIL") + f" #{pid}: {r['msg']}")
        ok += r["ok"]
        fail += not r["ok"]
    print(f"\n{ok} ok, {fail} failed")
    sys.exit(1 if fail else 0)


if __name__ == "__main__":
    main()
