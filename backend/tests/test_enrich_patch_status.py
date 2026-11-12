"""Test: enrich_worker._patch_status 实际写入 status_enrich（20261026 起独立列）。"""
from backend.db.conn import get_conn
from backend.workers.enrich_worker import _patch_status


def _insert_paper(status_enrich: str = "missing") -> int:
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO papers (dir_name, year, first_author, status_enrich) VALUES (?, ?, ?, ?)",
            ("test2024_x", 2024, "test", status_enrich),
        )
        return cur.lastrowid
    finally:
        conn.close()


def _read_status(paper_id: int) -> dict:
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT status_enrich, status_summary FROM papers WHERE id = ?", (paper_id,)
        ).fetchone()
        return dict(row)
    finally:
        conn.close()


def test_patch_status_manual_hold(temp_db):
    pid = _insert_paper(status_enrich="missing")
    _patch_status(pid, "manual_hold")
    st = _read_status(pid)
    assert st["status_enrich"] == "manual_hold"
    # 独立列起不再借用 status_summary（借用标记会被后续 summary 任务覆盖成 ok，
    # enrich 失败因此对 MCP/API 消费方不可见，实证）
    assert st["status_summary"] == "missing"


def test_patch_status_no_op_when_paper_missing(temp_db):
    """对不存在的 paper_id 不应抛异常。"""
    _patch_status(99999, "manual_hold")  # 无 raise = pass
