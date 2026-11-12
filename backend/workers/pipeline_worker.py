"""PipelineWorker - 串行执行 ingest -> parse -> enrich -> embed -> translate -> summary。

embed 位于 enrich 之后、translate 之前：嵌入只依赖 EN 原文
（parsed full.md / EN 覆盖层），翻译是面向人类阅读的可选步骤，不应阻塞 RAG 层。

设计：
  * 在单个 task 内顺序调用各 worker handler（不 fan-out 子 task，避免
    单线程 scheduler 自死锁；与 translate_orchestrator 同理）。
  * 默认幂等：若该步骤的 status_* 已经是 'ok'，跳过；可用 force_steps 强制重跑。
  * 任何一步抛异常 -> 整个 pipeline 失败；on_error 默认 'stop'，可设 'continue' 跳过该步继续。
  * 所有子步骤的 ctx.info / warn / error 都落到当前 pipeline task 的日志里。
  * 支持 feed 模式：payload 携带 inbox_path（而非 paper_id）时，首个步骤必须为
    'ingest'，ingest 完成后从 ctx.paper_id 取回 ID 传给后续步骤。
  * feed 任务重试时已关联 paper_id 则跳过 ingest，从后续步骤恢复，不再读取
    已由首次 ingest 删除的 inbox 文件。

payload:
  {
    "paper_id": int,           # 常规模式：已有 paper
    "inbox_path": str,         # feed 模式：PDF 路径（与 paper_id 互斥）
    "steps": ["ingest","parse","enrich","embed","translate","summary"],
    "force_steps": [],          # 列表内的步骤无论当前 status 都重跑
    "translate_force": false,   # 透传给 translate worker（清空已有 chunks）
    "on_error": "stop"          # "stop" | "continue"
  }
"""
from typing import Callable

from backend.db.conn import get_conn
from backend.db.repo import get_paper
from backend.pipeline.steps import PAPER_STEPS
from backend.workers.runner import register_worker, WorkerContext

from backend.workers.ingest_worker import ingest_worker
from backend.workers.parse_worker import parse_worker
from backend.workers.enrich_worker import enrich_worker
from backend.workers.translate_orchestrator import translate_worker
from backend.workers.summary_worker import summary_worker
from backend.workers.embed_worker import embed_worker


DEFAULT_STEPS = PAPER_STEPS

# step -> (handler, status_field_or_None)
# enrich 起有独立 status_enrich 列（20261026 迁移）；此前借用 status_summary 标
# manual_hold，会被后续 summary 任务覆盖成 ok，enrich 失败对消费方不可见
STEP_REGISTRY: dict[str, tuple[Callable, str | None]] = {
    "ingest":       (ingest_worker,         "status_pdf"),
    "parse":        (parse_worker,          "status_parsed"),
    "enrich":       (enrich_worker,         "status_enrich"),
    "translate":    (translate_worker,      "status_translated"),
    "summary":      (summary_worker,        "status_summary"),
    "embed":        (embed_worker,          "status_embedded"),
}


@register_worker("pipeline")
def pipeline_worker(ctx: WorkerContext, payload: dict) -> None:
    paper_id = payload.get("paper_id")
    resumed_feed = paper_id is None and ctx.paper_id is not None
    if resumed_feed:
        paper_id = ctx.paper_id
    inbox_path = payload.get("inbox_path")
    steps = payload.get("steps") or DEFAULT_STEPS
    force_steps = set(payload.get("force_steps") or [])
    translate_force = bool(payload.get("translate_force", False))
    on_error = payload.get("on_error", "stop")

    unknown = [s for s in steps if s not in STEP_REGISTRY]
    if unknown:
        raise ValueError(f"unknown steps: {unknown}")

    # feed 模式：无 paper_id，首个步骤必须是 ingest
    if paper_id is None:
        if not inbox_path:
            raise ValueError("pipeline payload needs either paper_id or inbox_path")
        if "ingest" not in steps:
            raise ValueError("pipeline with inbox_path must include 'ingest' step")
        if steps[0] != "ingest":
            raise ValueError("pipeline with inbox_path must start with 'ingest'")
    else:
        paper_id = int(paper_id)

    ctx.info(f"pipeline start: paper_id={paper_id} steps={steps} "
             f"force={sorted(force_steps)} on_error={on_error}")

    total = len(steps)
    failures: list[str] = []
    completed: list[str] = []
    skipped: list[str] = []

    for i, step in enumerate(steps):
        handler, status_field = STEP_REGISTRY[step]

        if step == "ingest":
            if resumed_feed:
                ctx.info(f"[{i+1}/{total}] resume paper_id={paper_id}; skip 'ingest'")
                skipped.append(step)
                ctx.progress(_step_progress(i + 1, total))
                continue
            if not inbox_path:
                raise ValueError("ingest step requires inbox_path in payload")
            sub_payload = {"inbox_path": inbox_path}
        else:
            if paper_id is None:
                raise ValueError(
                    f"step '{step}' requires paper_id; ingest must run first "
                    f"or provide paper_id in payload"
                )

            conn = get_conn()
            try:
                paper = get_paper(conn, paper_id)
            finally:
                conn.close()
            if not paper:
                raise ValueError(f"paper_id={paper_id} disappeared mid-pipeline")

            if status_field and step not in force_steps:
                cur = paper.get(status_field)
                if cur == "ok":
                    ctx.info(f"[{i+1}/{total}] skip '{step}' ({status_field}=ok)")
                    skipped.append(step)
                    ctx.progress(_step_progress(i + 1, total))
                    continue
                if cur == "manual_hold":
                    ctx.warn(f"[{i+1}/{total}] skip '{step}' ({status_field}=manual_hold; 需要人工介入)")
                    skipped.append(step)
                    ctx.progress(_step_progress(i + 1, total))
                    continue

            sub_payload: dict = {"paper_id": paper_id}
            if step == "translate":
                sub_payload["force"] = translate_force

        ctx.info(f"[{i+1}/{total}] running '{step}' ...")
        try:
            handler(ctx, sub_payload)
            completed.append(step)
            ctx.info(f"[{i+1}/{total}] '{step}' ok")
        except Exception as exc:  # noqa: BLE001
            failures.append(step)
            ctx.error(f"[{i+1}/{total}] '{step}' failed: {exc}")
            if on_error == "stop":
                raise RuntimeError(
                    f"pipeline aborted at step '{step}': {exc} "
                    f"(completed={completed} skipped={skipped})"
                )

        if step == "ingest":
            paper_id = ctx.paper_id
            if not paper_id:
                raise ValueError("ingest completed but ctx.paper_id not set")
            ctx.info(f"[{i+1}/{total}] ingest done, paper_id={paper_id}")

        ctx.progress(_step_progress(i + 1, total))

    summary = (f"pipeline done: completed={completed} "
               f"skipped={skipped} failed={failures}")
    ctx.info(summary)
    if failures:
        raise RuntimeError(summary)


def _step_progress(done: int, total: int) -> float:
    if total <= 0:
        return 1.0
    return min(1.0, done / total)
