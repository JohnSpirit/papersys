"""Tasks API + SSE 事件流（无用户体系：不按操作者过滤）。"""
import json
import time
from typing import Generator

from flask import Blueprint, Response, abort, jsonify, request

from backend.db import tasks_repo

bp = Blueprint("tasks", __name__, url_prefix="/api")


@bp.get("/tasks")
def tasks_list():
    args = request.args
    try:
        paper_id = int(args["paper_id"]) if args.get("paper_id") else None
        limit = min(int(args.get("limit", 100)), 500)
        offset = max(0, int(args.get("offset", 0)))
    except ValueError:
        abort(400, "invalid integer parameter")
    filters = {
        "status": args.get("status"), "kind": args.get("kind"), "paper_id": paper_id,
    }
    items = tasks_repo.list_tasks(**filters, limit=limit, offset=offset)
    total = tasks_repo.count_tasks(**filters)
    return jsonify({
        "items": items, "count": len(items), "total": total,
        "limit": limit, "offset": offset,
    })


@bp.get("/tasks/<int:task_id>")
def task_detail(task_id: int):
    task = tasks_repo.get_task(task_id)
    if not task:
        abort(404)
    task["log_tail"] = tasks_repo.read_task_log(task_id, tail=200)
    return jsonify(task)


@bp.post("/tasks/<int:task_id>/retry")
def task_retry(task_id: int):
    task = tasks_repo.get_task(task_id)
    if not task:
        abort(404)
    if task["status"] not in ("error", "cancelled"):
        abort(400, f"cannot retry task in status={task['status']}")
    new_id = tasks_repo.create_task(
        kind=task["kind"], payload=task.get("payload"), paper_id=task.get("paper_id"),
    )
    return jsonify({"new_task_id": new_id})


@bp.get("/events")
def events_stream():
    args = request.args
    try:
        since = int(args.get("since", 0))
        task_id = int(args["task_id"]) if args.get("task_id") else None
        paper_id = int(args["paper_id"]) if args.get("paper_id") else None
    except ValueError:
        abort(400, "invalid integer parameter")

    def stream() -> Generator[str, None, None]:
        last_id = since
        last_heartbeat = time.monotonic()
        while True:
            events = tasks_repo.list_events(
                since_id=last_id, task_id=task_id, paper_id=paper_id, limit=200,
            )
            for event in events:
                last_id = event["id"]
                event_name = "err" if event["level"] == "error" else event["level"]
                yield f"id: {event['id']}\n"
                yield f"event: {event_name}\n"
                yield f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"
            if time.monotonic() - last_heartbeat > 15:
                yield ": heartbeat\n\n"
                last_heartbeat = time.monotonic()
            time.sleep(0.8)

    return Response(stream(), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    })
