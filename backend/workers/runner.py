"""Worker base + scheduler。"""
import json
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

from backend.config import CONFIG
from backend.db import tasks_repo
from backend.services.redaction import redact_secrets


class WorkerContext:
    """Worker 运行上下文：暴露 emit_event + update_progress 等。"""

    def __init__(self, task_id: int, paper_id: Optional[int] = None,
                 task: Optional[dict] = None) -> None:
        self.task_id = task_id
        self.paper_id = paper_id
        self.task = task
        self.created_by_user_id = task.get("created_by_user_id") if task else None
        self.execution_subject = (task.get("execution_subject") or "system") if task else "system"
        self.required_permission = task.get("required_permission") if task else None

    def info(self, msg: str) -> None:
        tasks_repo.emit_event(self.task_id, msg, level="info", paper_id=self.paper_id)

    def warn(self, msg: str) -> None:
        tasks_repo.emit_event(self.task_id, msg, level="warn", paper_id=self.paper_id)

    def error(self, msg: str) -> None:
        tasks_repo.emit_event(self.task_id, msg, level="error", paper_id=self.paper_id)

    def progress(self, value: float) -> None:
        tasks_repo.update_task(self.task_id, progress=value)


# ── Worker registry ────────────────────────────────────────

WORKER_REGISTRY: dict[str, Callable[[WorkerContext, dict], None]] = {}


def register_worker(kind: str):
    def deco(fn: Callable[[WorkerContext, dict], None]):
        WORKER_REGISTRY[kind] = fn
        return fn
    return deco


# ── Scheduler 线程 ────────────────────────────────────────


class Scheduler:
    """多线程调度器：按 kind 分配独立线程池，长任务不阻塞短任务。"""

    def __init__(self, poll_interval: float = 1.0):
        self.poll_interval = poll_interval
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

        translate_cfg = CONFIG["workers"].get("translate_concurrency", 1)
        default_cfg = CONFIG["workers"].get("default_concurrency", 2)
        summary_cfg = CONFIG["workers"].get("summary_concurrency", 2)
        self._pools: dict[str, ThreadPoolExecutor] = {
            "translate": ThreadPoolExecutor(
                max_workers=translate_cfg, thread_name_prefix="wkr-translate"
            ),
            "summary": ThreadPoolExecutor(
                max_workers=summary_cfg, thread_name_prefix="wkr-summary"
            ),
        }
        self._default_pool = ThreadPoolExecutor(
            max_workers=default_cfg, thread_name_prefix="wkr-default"
        )
        self._slots = {
            "translate": threading.BoundedSemaphore(translate_cfg),
            "summary": threading.BoundedSemaphore(summary_cfg),
            "default": threading.BoundedSemaphore(default_cfg),
        }

    def _pool_for(self, kind: str) -> ThreadPoolExecutor:
        return self._pools.get(kind, self._default_pool)

    def _slot_for(self, kind: str) -> threading.BoundedSemaphore:
        return self._slots[kind if kind in ("translate", "summary") else "default"]

    def start(self) -> None:
        if self._thread:
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="task-scheduler")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        kinds = list(WORKER_REGISTRY.keys())
        while not self._stop.is_set():
            try:
                preview = tasks_repo.peek_pending(kinds)
            except Exception:
                preview = None
            if not preview:
                time.sleep(self.poll_interval)
                continue
            kind = preview["kind"]
            slot = self._slot_for(kind)
            # 先拿执行空位再领取任务：pending 不会在线程池排队时被提前标成 running。
            if not slot.acquire(blocking=False):
                time.sleep(self.poll_interval)
                continue
            submitted = False
            claimed = None
            try:
                claimed = tasks_repo.claim_pending([kind])
                if not claimed:
                    continue
                pool = self._pool_for(kind)
                pool.submit(self._execute_with_slot, claimed, slot)
                submitted = True
            except Exception as exc:
                if claimed:
                    tasks_repo.update_task(
                        claimed["id"], status="error", error=redact_secrets(str(exc)), finished=True,
                    )
            finally:
                if not submitted:
                    slot.release()

    def _execute_with_slot(self, task: dict, slot: threading.BoundedSemaphore) -> None:
        try:
            self._execute(task)
        finally:
            slot.release()

    def _execute(self, task: dict) -> None:
        kind = task["kind"]
        task_id = task["id"]
        handler = WORKER_REGISTRY.get(kind)
        ctx = WorkerContext(task_id=task_id, paper_id=task.get("paper_id"), task=task)
        if not handler:
            tasks_repo.update_task(task_id, status="error", error=f"no handler for kind={kind}", finished=True)
            ctx.error(f"no handler for kind={kind}")
            return
        if task.get("execution_subject") is None:
            ctx.warn("task has NULL execution_subject; treating it as a legacy system task")
        try:
            tasks_repo.update_task(task_id, started=True)
            ctx.info(f"started kind={kind}")
            handler(ctx, task.get("payload") or {})
            tasks_repo.update_task(task_id, status="ok", progress=1.0, finished=True)
            ctx.info("done")
        except Exception as exc:
            transient = _is_transient_error(exc)
            can_retry = (
                transient
                and kind in tasks_repo.REQUEUE_KINDS
                and (task.get("retries") or 0) < tasks_repo._max_task_retries()
            )
            if can_retry:
                tasks_repo.requeue_task(
                    task_id,
                    f"transient failure; requeued: {redact_secrets(str(exc))}",
                    level="warn",
                )
                return
            tb = traceback.format_exc()
            tasks_repo.update_task(task_id, status="error", error=redact_secrets(str(exc)), finished=True)
            ctx.error(f"failed: {redact_secrets(str(exc))}\n{redact_secrets(tb)}")


def _is_transient_error(exc: Exception) -> bool:
    """MinerU 超时、SQLite 写锁和损坏 JSON 缓存都可安全重试。"""
    return (
        isinstance(exc, (TimeoutError, json.JSONDecodeError))
        or "database is locked" in str(exc).lower()
    )


# 全局实例（在 Flask app 启动时 .start()）
scheduler = Scheduler()