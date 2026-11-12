"""任务调度容量、重启恢复与外部调用故障处理。"""
import json
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

from backend.db import tasks_repo
from backend.db.conn import get_conn
from backend.external import base as base_mod
from backend.external.base import RateLimitedClient


def _task_row(task_id: int) -> dict:
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return dict(row)
    finally:
        conn.close()


def test_scheduler_claims_only_when_worker_slot_free(temp_db, monkeypatch):
    from backend.config import CONFIG
    from backend.workers import runner

    monkeypatch.setitem(CONFIG["workers"], "default_concurrency", 1)
    started = threading.Event()
    release = threading.Event()

    def handler(ctx, payload):
        started.set()
        release.wait(2)

    monkeypatch.setitem(runner.WORKER_REGISTRY, "pipeline", handler)
    ids = [tasks_repo.create_task("pipeline", {"i": i}) for i in range(3)]
    scheduler = runner.Scheduler(poll_interval=0.01)
    scheduler.start()
    try:
        assert started.wait(1)
        rows = [_task_row(i) for i in ids]
        assert [r["status"] for r in rows].count("running") == 1
        assert [r["status"] for r in rows].count("pending") == 2
        running = next(r for r in rows if r["status"] == "running")
        assert running["started_at"] is not None
        assert all(r["started_at"] is None for r in rows if r["status"] == "pending")

        release.set()
        deadline = time.time() + 3
        while time.time() < deadline:
            if all(_task_row(i)["status"] == "ok" for i in ids):
                break
            time.sleep(0.02)
        assert all(_task_row(i)["status"] == "ok" for i in ids)
    finally:
        release.set()
        scheduler.stop()
        scheduler._thread.join(1)


def test_execute_sets_started_at_and_requeues_transient_error(temp_db, monkeypatch):
    from backend.workers import runner

    tid = tasks_repo.create_task("pipeline", {})
    task = tasks_repo.claim_pending(["pipeline"])
    assert task["started_at"] is None

    monkeypatch.setitem(
        runner.WORKER_REGISTRY, "pipeline",
        lambda ctx, payload: (_ for _ in ()).throw(TimeoutError("MinerU poll timeout")),
    )
    runner.Scheduler()._execute(task)

    row = _task_row(tid)
    assert row["status"] == "pending"
    assert row["retries"] == 1
    assert row["started_at"] is None
    assert row["finished_at"] is None
    assert row["error"] is None

    monkeypatch.setitem(runner.WORKER_REGISTRY, "pipeline", lambda ctx, payload: None)
    runner.Scheduler()._execute(tasks_repo.claim_pending(["pipeline"]))
    row = _task_row(tid)
    assert row["status"] == "ok"
    assert row["started_at"] is not None
    assert row["finished_at"] is not None


def test_execute_non_transient_error_stays_error(temp_db, monkeypatch):
    from backend.workers import runner

    tid = tasks_repo.create_task("pipeline", {})
    task = tasks_repo.claim_pending(["pipeline"])
    monkeypatch.setitem(
        runner.WORKER_REGISTRY, "pipeline",
        lambda ctx, payload: (_ for _ in ()).throw(ValueError("bad input")),
    )
    runner.Scheduler()._execute(task)
    row = _task_row(tid)
    assert row["status"] == "error"
    assert "bad input" in row["error"]


def test_recover_orphans_requeues_and_marks_others(temp_db):
    pipeline = tasks_repo.create_task("pipeline", {})
    summary = tasks_repo.create_task("summary", {})
    translate = tasks_repo.create_task("translate", {})
    exhausted = tasks_repo.create_task("pipeline", {})
    for tid, kind in ((pipeline, "pipeline"), (summary, "summary"),
                      (translate, "translate"), (exhausted, "pipeline")):
        assert tasks_repo.claim_pending([kind])["id"] == tid
    for _ in range(3):
        tasks_repo.update_task(exhausted, increment_retries=True)

    stats = tasks_repo.recover_orphans(max_retries=3)
    assert stats == {"requeued": 2, "failed": 2}

    assert _task_row(pipeline)["status"] == "pending"
    assert _task_row(pipeline)["retries"] == 1
    assert _task_row(summary)["status"] == "pending"
    assert _task_row(translate)["status"] == "error"
    assert _task_row(exhausted)["status"] == "error"
    assert tasks_repo.last_recovery_stats()["requeued"] == 2
    assert tasks_repo.last_recovery_stats()["failed"] == 2


def test_pipeline_resume_skips_ingest_after_paper_was_linked(temp_db, monkeypatch):
    from backend.workers import pipeline_worker as pipeline
    from backend.workers.runner import WorkerContext

    conn = get_conn()
    try:
        paper_id = conn.execute(
            "INSERT INTO papers (dir_name, year, first_author) VALUES ('resumed', 2026, 'test')"
        ).lastrowid
    finally:
        conn.close()
    task_id = tasks_repo.create_task("pipeline", {
        "inbox_path": "/already-removed/inbox.pdf",
        "steps": ["ingest", "parse"],
    }, paper_id=paper_id)
    task = tasks_repo.get_task(task_id)
    calls = []
    monkeypatch.setitem(pipeline.STEP_REGISTRY, "ingest", (
        lambda ctx, payload: calls.append("ingest"), "status_pdf",
    ))
    monkeypatch.setitem(pipeline.STEP_REGISTRY, "parse", (
        lambda ctx, payload: calls.append(payload["paper_id"]), "status_parsed",
    ))

    pipeline.pipeline_worker(
        WorkerContext(task_id=task_id, paper_id=task["paper_id"]), task["payload"],
    )

    assert calls == [paper_id]


class _CacheClient(RateLimitedClient):
    service_name = "test_corrupt_cache"
    rps = 1000.0


class _JsonSession:
    def __init__(self):
        self.calls = 0

    def request(self, method, url, timeout=None, **kw):
        self.calls += 1
        r = requests.Response()
        r.status_code = 200
        r._content = b'{"ok": true}'
        r.headers["Content-Type"] = "application/json"
        r.encoding = "utf-8"
        return r


def test_corrupt_cache_is_discarded(tmp_path, monkeypatch):
    monkeypatch.setattr(base_mod, "CACHE_DIR", tmp_path)
    client = _CacheClient()
    session = _JsonSession()
    client._session = session
    url = "https://api.example/corrupt"

    client.get(url)
    assert session.calls == 1
    cache_files = list((tmp_path / "test_corrupt_cache").glob("*.json"))
    assert len(cache_files) == 1
    cache_files[0].write_text('{"truncated":', encoding="utf-8")

    response = client.get(url)
    assert session.calls == 2
    assert response.json() == {"ok": True}
    assert getattr(response, "from_cache", False) is not True


def test_local_mineru_total_timeout(tmp_path):
    from backend.external.mineru import MinerUClient

    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.7 fake")
    with patch.object(MinerUClient, "__init__", lambda self: None):
        client = MinerUClient()
        client._mode = "local"
        client._session = MagicMock()
        client._session.post.side_effect = lambda *a, **kw: time.sleep(1)
        with patch("backend.external.mineru.CONFIG", {
            "mineru": {"local_backend": "hybrid-engine", "local_effort": "medium",
                       "local_api_base": "http://127.0.0.1:8001", "local_timeout": 0.05},
        }):
            start = time.monotonic()
            with pytest.raises(TimeoutError, match="total timeout"):
                client.parse_pdf_local(str(pdf), tmp_path / "out")
            assert time.monotonic() - start < 0.5
