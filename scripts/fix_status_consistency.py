"""status 状态位与内容表一致性审计与修复。

历史迁移曾出现「只搬状态位没搬内容」：status_summary='ok' 但 summaries 表无行，
get_summary 返回 available=false 与状态徽章矛盾。translations 同口径可一并审计。

审计口径：
  - status_summary='ok' 但 summaries 无行     -> 建议重置 'missing'
  - status_translated='ok' 但 translations 无行 -> 建议重置 'missing'
--apply 才写库；重置后需要内容的论文走既有 re-summarize / re-translate 动作重跑。

用法（项目 Python 3.12 环境）:
    python3 scripts/fix_status_consistency.py            # 审计（只读）
    python3 scripts/fix_status_consistency.py --apply    # 重置不实状态
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.db.conn import get_conn  # noqa: E402

_CHECKS = [
    ("status_summary", "summaries"),
    ("status_translated", "translations"),
]


def audit() -> dict[str, list[int]]:
    """返回 {状态列: 不一致 paper id 列表}。"""
    out: dict[str, list[int]] = {}
    conn = get_conn()
    try:
        for status_col, table in _CHECKS:
            rows = conn.execute(
                f"""SELECT p.id FROM papers p
                    WHERE p.{status_col} = 'ok'
                      AND NOT EXISTS (SELECT 1 FROM {table} t WHERE t.paper_id = p.id)
                    ORDER BY p.id"""
            ).fetchall()
            out[status_col] = [r["id"] for r in rows]
    finally:
        conn.close()
    return out


def apply(reset: dict[str, list[int]]) -> int:
    """把不一致状态重置为 missing，返回受影响行数。"""
    n = 0
    conn = get_conn()
    try:
        conn.execute("BEGIN")
        for status_col, ids in reset.items():
            if not ids:
                continue
            placeholders = ",".join("?" * len(ids))
            cur = conn.execute(
                f"UPDATE papers SET {status_col} = 'missing',"
                " updated_at = CURRENT_TIMESTAMP"
                f" WHERE id IN ({placeholders})",
                ids,
            )
            n += cur.rowcount
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
    return n


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="重置不实状态（默认只审计）")
    args = ap.parse_args()

    found = audit()
    total = 0
    for status_col, ids in found.items():
        print(f"{status_col}='ok' 但内容表无行: {len(ids)} 篇")
        if ids:
            wrapped = ", ".join(str(i) for i in ids)
            print(f"  ids: {wrapped}")
        total += len(ids)
    if not args.apply:
        print(f"\n审计完成：共 {total} 处不一致（--apply 重置为 missing）")
        return
    if not total:
        print("\n无不一致，无需重置")
        return
    n = apply(found)
    print(f"\n已重置 {n} 行状态为 missing")


if __name__ == "__main__":
    main()
