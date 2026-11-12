"""错配论文重驱：把指定 paper 的 PDF 退回 inbox、清除其 DB 记录与归档目录，
并重新入队 pipeline 任务（ingest+parse+enrich+embed+summary）。

用于元数据错配修复：dir_name 不可变，纠错只能删行重投。

用法：
    python3 scripts/redrive_papers.py <id...>            # 按 paper id
    python3 scripts/redrive_papers.py --file ids.txt     # 每行一个 id（# 开头为注释）
    python3 scripts/redrive_papers.py --dry-run <id>     # 只打印将执行的动作

注意：删除是不可逆操作（library 目录、translations/summaries/embeddings/annotations/
doc_revisions/tasks 全部清除）；快照文件（数据根目录下 doc_snapshots/）保留。
"""
import argparse
import json
import shutil
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.config import CONFIG, resolve_path  # noqa: E402
from backend.db import tasks_repo  # noqa: E402
from backend.db.conn import get_conn  # noqa: E402

REDRIVE_STEPS = ["ingest", "parse", "enrich", "embed", "summary"]


def _inbox_dir() -> Path:
    return Path(CONFIG["paths"]["inbox_dir"])


def _original_filename(conn: sqlite3.Connection, paper_id: int, dir_name: str) -> str:
    """从最近的 pipeline/ingest 任务 payload 找回投递时的原始文件名。"""
    rows = conn.execute(
        "SELECT payload_json FROM tasks WHERE paper_id = ? ORDER BY id DESC",
        (paper_id,),
    ).fetchall()
    for r in rows:
        try:
            p = json.loads(r[0] or "{}")
        except json.JSONDecodeError:
            continue
        ip = p.get("inbox_path")
        if ip:
            return Path(ip).name
    return f"{dir_name}.pdf"


def redrive_one(paper_id: int, *, dry_run: bool = False) -> dict:
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
    if not pdf_abs:
        return {"paper_id": paper_id, "ok": False,
                "msg": f"pdf missing on disk: {paper['pdf_path']}"}
    pdf_abs = Path(pdf_abs)
    library = Path(CONFIG["paths"]["library_root"]).resolve()
    pdf_dir = pdf_abs.parent
    if library not in pdf_dir.resolve().parents:
        return {"paper_id": paper_id, "ok": False,
                "msg": f"pdf dir not under library_root, refuse to remove: {pdf_dir}"}

    conn = get_conn()
    try:
        conn.row_factory = sqlite3.Row
        orig_name = _original_filename(conn, paper_id, paper["dir_name"])
    finally:
        conn.close()

    inbox_dest = _inbox_dir() / orig_name
    if inbox_dest.exists():
        inbox_dest = _inbox_dir() / f"{inbox_dest.stem}_r{paper_id}{inbox_dest.suffix}"

    if dry_run:
        return {"paper_id": paper_id, "ok": True, "dry": True,
                "msg": f"would copy {pdf_abs} -> {inbox_dest}, delete rows + {pdf_dir}, enqueue pipeline"}

    # 1) PDF 退回 inbox（先复制，成功后再删库/删目录）
    _inbox_dir().mkdir(parents=True, exist_ok=True)
    shutil.copy2(pdf_abs, inbox_dest)

    # 2) 清 DB（citations 表无 paper_id 列，不涉及）
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
        inbox_dest.unlink(missing_ok=True)
        raise
    finally:
        conn.close()

    # 3) 删归档目录
    shutil.rmtree(pdf_dir)

    # 4) 重新入队 pipeline
    tid = tasks_repo.create_task(
        kind="pipeline",
        payload={"inbox_path": str(inbox_dest), "steps": REDRIVE_STEPS},
    )
    return {"paper_id": paper_id, "ok": True,
            "msg": f"redriven as task {tid}: {orig_name} (was {paper['dir_name']})"}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("ids", nargs="*", type=int, help="paper ids")
    ap.add_argument("--file", help="每行一个 paper id 的文件（# 开头为注释）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    ids = list(args.ids)
    if args.file:
        for line in Path(args.file).read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                ids.append(int(line))
    if not ids:
        ap.error("no paper ids given")

    ok = fail = 0
    for pid in ids:
        try:
            r = redrive_one(pid, dry_run=args.dry_run)
        except Exception as e:  # noqa: BLE001
            r = {"paper_id": pid, "ok": False, "msg": f"exception: {e}"}
        print(("OK " if r["ok"] else "FAIL") + f" #{pid}: {r['msg']}")
        ok += r["ok"]
        fail += not r["ok"]
    print(f"\n{ok} ok, {fail} failed")
    sys.exit(1 if fail else 0)


if __name__ == "__main__":
    main()
