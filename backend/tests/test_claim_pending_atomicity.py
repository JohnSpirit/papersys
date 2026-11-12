"""Test: claim_pending 原子性 + 多线程不重复领取同一任务。

P0-2 修复后必须保证：N 个并发线程同时调用 claim_pending，
每个 task 最多只被一个线程领取（绝不能被两次领取）。
"""
import json
import threading

from backend.db import tasks_repo
from backend.db.conn import get_conn


def _insert_tasks(n: int) -> None:
    conn = get_conn()
    try:
        for i in range(n):
            conn.execute(
                "INSERT INTO tasks (kind, payload_json, status) VALUES (?, ?, 'pending')",
                ("translate", json.dumps({"i": i})),
            )
    finally:
        conn.close()


def test_claim_pending_single(temp_db):
    _insert_tasks(1)
    t1 = tasks_repo.claim_pending(["translate"])
    assert t1 is not None
    assert t1["status"] == "running"
    # 已被 claim 的不会再被领取
    t2 = tasks_repo.claim_pending(["translate"])
    assert t2 is None


def test_claim_pending_filter_by_kind(temp_db):
    conn = get_conn()
    try:
        conn.execute("INSERT INTO tasks (kind, status) VALUES ('translate', 'pending')")
        conn.execute("INSERT INTO tasks (kind, status) VALUES ('parse', 'pending')")
    finally:
        conn.close()
    t1 = tasks_repo.claim_pending(["parse"])
    assert t1 is not None
    assert t1["kind"] == "parse"
    t2 = tasks_repo.claim_pending(["parse"])
    assert t2 is None
    t3 = tasks_repo.claim_pending(["translate"])
    assert t3 is not None
    assert t3["kind"] == "translate"


def test_claim_pending_concurrent_no_duplicate(temp_db):
    """8 个线程同时 claim，N 个任务总共只被领取 N 次。"""
    N_TASKS = 20
    N_THREADS = 8
    _insert_tasks(N_TASKS)

    claimed_ids: list[int] = []
    lock = threading.Lock()
    barrier = threading.Barrier(N_THREADS)

    def worker():
        barrier.wait()
        while True:
            t = tasks_repo.claim_pending(["translate"])
            if t is None:
                break
            with lock:
                claimed_ids.append(t["id"])

    threads = [threading.Thread(target=worker) for _ in range(N_THREADS)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    assert len(claimed_ids) == N_TASKS, f"expected {N_TASKS} claims, got {len(claimed_ids)}"
    assert len(set(claimed_ids)) == N_TASKS, (
        f"duplicate claim detected! claimed_ids={sorted(claimed_ids)}"
    )


# ── 同一 paper 任务串行化（3.1.0b4：防并发写同一论文）────────


def _insert_papers_for_tasks(n: int) -> None:
    """tasks.paper_id 有外键指向 papers，先造 papers 行。"""
    conn = get_conn()
    try:
        for i in range(1, n + 1):
            conn.execute(
                "INSERT INTO papers (dir_name, year, first_author, title) VALUES (?, 2023, 'a', 't')",
                (f"claim_test_{i}",),
            )
    finally:
        conn.close()


def _insert_paper_task(kind: str, paper_id, payload_i: int = 0) -> int:
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO tasks (kind, payload_json, status, paper_id) VALUES (?, ?, 'pending', ?)",
            (kind, json.dumps({"i": payload_i}), paper_id),
        )
        return cur.lastrowid
    finally:
        conn.close()


def test_claim_skips_paper_with_running_task(temp_db):
    """paper 1 已有 running 任务时，paper 1 的其他 pending 任务不被领取，
    其他论文的任务照常领取。"""
    _insert_papers_for_tasks(2)
    _insert_paper_task("translate", 1, 0)
    _insert_paper_task("translate", 1, 1)
    _insert_paper_task("translate", 2, 2)

    t1 = tasks_repo.claim_pending(["translate"])  # 领走 paper 1 的第一个
    assert t1["paper_id"] == 1
    t2 = tasks_repo.claim_pending(["translate"])  # 跳过 paper 1 第二个，领 paper 2
    assert t2["paper_id"] == 2
    assert tasks_repo.claim_pending(["translate"]) is None  # paper 1 第二个仍 pending

    # running 任务结束后（标 ok），paper 1 的第二个可领取
    tasks_repo.update_task(t1["id"], status="ok", finished=True)
    t3 = tasks_repo.claim_pending(["translate"])
    assert t3 is not None
    assert t3["paper_id"] == 1


def test_claim_allows_null_paper_tasks_always(temp_db):
    """paper_id 为 NULL 的任务（ingest/feed）不受同论文串行化限制。"""
    _insert_papers_for_tasks(1)
    _insert_paper_task("translate", 1, 0)  # 先领走，占住 paper 1
    assert tasks_repo.claim_pending(["translate"])["paper_id"] == 1

    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO tasks (kind, payload_json, status) VALUES ('ingest', '{}', 'pending')"
        )
    finally:
        conn.close()

    t = tasks_repo.claim_pending(["ingest", "translate"])
    assert t is not None
    assert t["kind"] == "ingest"
    assert t["paper_id"] is None