"""Migration v3（存储本地化）：

  1. 路径字段改写：papers.pdf_path / parsed_path 中指向旧存储归档的绝对路径
     统一改写为 "legacy:<原路径>"；本地路径改写为相对 library_root 的相对路径。
     读取方（config.resolve_path）遇到 legacy: 且本地不存在时视为缺失。
  2. summary_path 列内容清空（旧存储总结文件已由 summaries 表取代）。

幂等：重复执行无副作用（已改写的值不再匹配旧存储前缀）。
新库（无历史数据）执行为空操作。

用法：
    PYTHONPATH=. python3 backend/db/migrations/20260901_localize_paths.py
"""
import sys
import sqlite3
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from backend.config import CONFIG, library_root, LEGACY_PREFIX  # noqa: E402


def _is_relative_to(p: Path, base: Path) -> bool:
    try:
        p.resolve().relative_to(base.resolve())
        return True
    except (ValueError, OSError):
        return False


def migrate(conn: sqlite3.Connection) -> dict:
    root = library_root()
    nas_base = Path(CONFIG["paths"].get("nas_base", "") or "/nonexistent").expanduser()

    rows = conn.execute(
        "SELECT id, pdf_path, parsed_path, summary_path FROM papers"
    ).fetchall()

    n_legacy = n_rewritten = n_cleared_summary = 0
    for r in rows:
        pid = r["id"]
        updates: dict[str, str | None] = {}

        for col in ("pdf_path", "parsed_path"):
            v = r[col]
            if not v or v.startswith(LEGACY_PREFIX):
                continue
            p = Path(v)
            if _is_relative_to(p, nas_base):
                updates[col] = f"{LEGACY_PREFIX}{v}"
                n_legacy += 1
            elif p.is_absolute() and _is_relative_to(p, root):
                try:
                    updates[col] = str(p.resolve().relative_to(root.resolve()))
                except ValueError:
                    pass
                n_rewritten += 1

        if r["summary_path"]:
            updates["summary_path"] = None
            n_cleared_summary += 1

        if updates:
            sets = ", ".join(f"{k} = ?" for k in updates)
            conn.execute(
                f"UPDATE papers SET {sets} WHERE id = ?",
                [*updates.values(), pid],
            )

    conn.commit()
    return {
        "papers_total": len(rows),
        "marked_legacy": n_legacy,
        "rewritten_relative": n_rewritten,
        "cleared_summary_path": n_cleared_summary,
    }


def main() -> None:
    from backend.db.conn import get_conn

    conn = get_conn()
    try:
        result = migrate(conn)
    finally:
        conn.close()
    print("v3 storage migration done:")
    for k, v in result.items():
        print(f"  {k:22} {v}")


if __name__ == "__main__":
    main()
