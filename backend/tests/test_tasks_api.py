import pytest

from backend.db import tasks_repo
from backend.db.conn import get_conn


@pytest.fixture
def client(temp_db, monkeypatch):
    from backend.workers.runner import scheduler
    monkeypatch.setattr(scheduler, "start", lambda: None)
    from backend.app import create_app
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


def _insert_paper() -> int:
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO papers (dir_name, year, first_author, status_parsed)"
            " VALUES ('task_api_paper', 2026, 'test', 'ok')"
        )
        return cur.lastrowid
    finally:
        conn.close()


def test_tasks_total_respects_filters_and_pagination(client):
    paper_id = _insert_paper()
    for i in range(7):
        task_id = tasks_repo.create_task(
            kind="translate" if i % 2 else "summary",
            payload={"paper_id": paper_id, "index": i},
            paper_id=paper_id,
        )
        if i < 2:
            tasks_repo.update_task(task_id, status="error")

    response = client.get("/api/tasks?kind=translate&limit=2&offset=2")
    assert response.status_code == 200
    body = response.get_json()
    assert body["total"] == 3
    assert body["count"] == 1
    assert len(body["items"]) == 1
    assert body["limit"] == 2 and body["offset"] == 2

    response = client.get("/api/tasks?status=error&limit=1")
    assert response.get_json()["total"] == 2
    assert client.get("/api/tasks?limit=bad").status_code == 400


def test_create_or_reuse_active_task_matches_payload(temp_db):
    paper_id = _insert_paper()
    first_id, reused = tasks_repo.create_or_reuse_active_task(
        "translate", {"paper_id": paper_id, "force": False}, paper_id,
    )
    assert reused is False
    second_id, reused = tasks_repo.create_or_reuse_active_task(
        "translate", {"paper_id": paper_id, "force": False}, paper_id,
    )
    assert reused is True and second_id == first_id

    forced_id, reused = tasks_repo.create_or_reuse_active_task(
        "translate", {"paper_id": paper_id, "force": True}, paper_id,
    )
    assert reused is False and forced_id != first_id

    tasks_repo.update_task(first_id, status="ok")
    after_done_id, reused = tasks_repo.create_or_reuse_active_task(
        "translate", {"paper_id": paper_id, "force": False}, paper_id,
    )
    assert reused is False and after_done_id not in (first_id, forced_id)


def test_paper_action_returns_reused_task(client):
    paper_id = _insert_paper()
    first = client.post(f"/api/papers/{paper_id}/retranslate", json={"force": False})
    second = client.post(f"/api/papers/{paper_id}/retranslate", json={"force": False})
    forced = client.post(f"/api/papers/{paper_id}/retranslate", json={"force": True})

    assert first.status_code == 200 and first.get_json()["reused"] is False
    assert second.status_code == 200 and second.get_json()["reused"] is True
    assert second.get_json()["task_id"] == first.get_json()["task_id"]
    assert forced.get_json()["reused"] is False
    assert forced.get_json()["task_id"] != first.get_json()["task_id"]


def test_cancelled_task_can_be_retried(client):
    task_id = tasks_repo.create_task("summary", {"paper_id": None})
    tasks_repo.update_task(task_id, status="cancelled")
    response = client.post(f"/api/tasks/{task_id}/retry")
    assert response.status_code == 200
    new_id = response.get_json()["new_task_id"]
    assert new_id != task_id
    assert tasks_repo.get_task(new_id)["status"] == "pending"
