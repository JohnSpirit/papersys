"""papers 增加独立 enrich 状态列 status_enrich，首次添加列时按事件历史回填。

背景：enrich 失败此前借用 status_summary 标 manual_hold，
后续 summary 任务成功后该标记被覆盖成 ok——批量入库论文
的「OpenAlex 三层回退后仍未匹配」警告因此对 MCP/API 消费方不可见（事件在、状态
被抹掉），检索与 get_paper 都无从提示。独立列补上这一缺口。

回填规则（按事件历史重建）：
  * 有 enrich 成功事件（msg='metadata updated'，enrich_worker 独有）且 events.id
    大于最近一次 manual_hold 警告 → 'ok'
  * 否则有 enrich manual_hold 警告：SI 附件按 enrich_worker 现行设计不标
    manual_hold（enrich 无匹配属预期）→ 'ok'，其余 → 'manual_hold'
  * 否则存在 status='ok' 的 enrich 任务 → 'ok'
  * 否则 'missing'（含旧库迁移、从未跑过 enrich 的记录）

列已存在时保留当前状态，不再执行回填。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from backend.db.conn import get_conn  # noqa: E402


def _backfill(conn) -> dict:
    counts: dict[str, int] = {}
    for (pid,) in conn.execute("SELECT id FROM papers ORDER BY id").fetchall():
        ok_id = conn.execute(
            "SELECT MAX(id) FROM events WHERE paper_id = ? AND msg = 'metadata updated'",
            (pid,),
        ).fetchone()[0]
        hold_id = conn.execute(
            "SELECT MAX(id) FROM events WHERE paper_id = ? AND level = 'warn'"
            " AND (msg LIKE '%marking manual_hold%' OR msg LIKE '%标记 manual_hold%')",
            (pid,),
        ).fetchone()[0]
        if ok_id and (not hold_id or ok_id > hold_id):
            status = "ok"
        elif hold_id:
            kind = conn.execute(
                "SELECT COALESCE(attachment_kind, '') FROM papers WHERE id = ?", (pid,)
            ).fetchone()[0]
            status = "ok" if kind == "si" else "manual_hold"
        else:
            ok_task = conn.execute(
                "SELECT 1 FROM tasks WHERE kind = 'enrich' AND paper_id = ?"
                " AND status = 'ok' LIMIT 1",
                (pid,),
            ).fetchone()
            status = "ok" if ok_task else "missing"
        conn.execute("UPDATE papers SET status_enrich = ? WHERE id = ?", (status, pid))
        counts[status] = counts.get(status, 0) + 1
    return counts


def migrate(conn=None) -> dict:
    own = conn is None
    if own:
        conn = get_conn()
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(papers)").fetchall()}
        added = False
        counts: dict[str, int] = {}
        if "status_enrich" not in cols:
            conn.execute("ALTER TABLE papers ADD COLUMN status_enrich TEXT DEFAULT 'missing'")
            added = True
            counts = _backfill(conn)
        conn.commit()
        return {"added_column": added, "backfilled": counts}
    finally:
        if own:
            conn.close()


if __name__ == "__main__":
    print(f"status_enrich migration done: {migrate()}")
