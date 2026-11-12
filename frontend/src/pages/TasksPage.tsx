import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import {
  AlertCircle,
  CheckCircle2,
  Clock,
  FileText,
  Play,
  RefreshCw,
  Upload,
  XCircle,
} from "lucide-react";
import { api, errMsg, type InboxItem, type SSEvent, type Task, type TasksResp } from "@/lib/api";
import { useToast } from "@/components/toast-context";
import { useEventStream } from "@/lib/sse";
import { formatDateTime } from "@/lib/datetime";
import { FormField } from "@/components/FormField";
import { Pagination } from "@/components/Pagination";
import { Dialog } from "@/components/Dialog";
import { EmptyState, ErrorState, LoadingState } from "@/components/Feedback";

const PAGE_SIZE = 50;
const STATUS_OPTIONS = [
  { value: "", label: "全部" }, { value: "pending", label: "等待执行" },
  { value: "running", label: "正在执行" }, { value: "ok", label: "已完成" },
  { value: "error", label: "失败" }, { value: "cancelled", label: "已取消" },
];
const KIND_OPTIONS = [
  { value: "", label: "全部" }, { value: "ingest", label: "入库" },
  { value: "parse", label: "解析" }, { value: "enrich", label: "富化" },
  { value: "translate", label: "翻译" }, { value: "summary", label: "总结" },
  { value: "embed", label: "向量" }, { value: "citation", label: "引文" },
  { value: "pipeline", label: "处理流程" },
];
const KIND_LABELS = Object.fromEntries(KIND_OPTIONS.map((item) => [item.value, item.label]));

export function TasksPage() {
  const [result, setResult] = useState<TasksResp | null>(null);
  const [inbox, setInbox] = useState<InboxItem[]>([]);
  const [statusFilter, setStatusFilter] = useState("");
  const [kindFilter, setKindFilter] = useState("");
  const [page, setPage] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const [scanning, setScanning] = useState(false);
  const [retrying, setRetrying] = useState<number | null>(null);
  const [detail, setDetail] = useState<Task | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [detailReload, setDetailReload] = useState(0);
  const fileRef = useRef<HTMLInputElement>(null);
  const requestRef = useRef(0);
  const actionRef = useRef(false);
  const lastSeenId = useRef(0);
  const detailRequestRef = useRef(0);
  const [searchParams, setSearchParams] = useSearchParams();
  const taskParam = searchParams.get("task");
  const { toast } = useToast();
  const { events, connected } = useEventStream({ maxEvents: 200 });

  const load = useCallback(async () => {
    const requestId = ++requestRef.current;
    setLoading(true);
    setError(null);
    try {
      const [taskResponse, inboxResponse] = await Promise.all([
        api.tasks({ status: statusFilter, kind: kindFilter, limit: PAGE_SIZE, offset: page * PAGE_SIZE }),
        api.inbox(),
      ]);
      if (requestId !== requestRef.current) return;
      setResult(taskResponse);
      setInbox(inboxResponse.items);
    } catch (reason: unknown) {
      if (requestId === requestRef.current) setError(errMsg(reason));
    } finally {
      if (requestId === requestRef.current) setLoading(false);
    }
  }, [statusFilter, kindFilter, page]);

  useEffect(() => {
    queueMicrotask(() => { void load(); });
  }, [load]);

  useEffect(() => {
    const fresh = events.filter((event) => event.id > lastSeenId.current);
    if (fresh.length === 0) return;
    lastSeenId.current = fresh[fresh.length - 1].id;
    if (fresh.some((event) => event.msg.startsWith("done") || event.msg.startsWith("failed"))) {
      queueMicrotask(() => { void load(); });
    }
  }, [events, load]);

  useEffect(() => {
    if (!taskParam) return;
    const taskId = Number(taskParam);
    const requestId = ++detailRequestRef.current;
    queueMicrotask(() => {
      setDetailLoading(true);
      setDetailError(null);
      if (!Number.isInteger(taskId) || taskId <= 0) {
        setDetailError("任务编号无效。");
        setDetailLoading(false);
        return;
      }
      api.task(taskId).then((task) => {
        if (requestId === detailRequestRef.current) setDetail(task);
      }).catch((reason: unknown) => {
        if (requestId === detailRequestRef.current) setDetailError(errMsg(reason));
      }).finally(() => {
        if (requestId === detailRequestRef.current) setDetailLoading(false);
      });
    });
  }, [taskParam, detailReload]);

  function closeDetail() {
    setDetail(null);
    setDetailError(null);
    if (taskParam) {
      const next = new URLSearchParams(searchParams);
      next.delete("task");
      setSearchParams(next, { replace: true });
    }
  }

  async function handleUpload(event: React.ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    if (!file || actionRef.current) return;
    actionRef.current = true;
    setUploading(true);
    try {
      const response = await api.inboxUpload(file);
      toast(`已上传并创建任务 #${response.task_id}`, "success");
      await load();
    } catch (reason: unknown) {
      toast(`上传失败：${errMsg(reason)}`, "error");
    } finally {
      actionRef.current = false;
      setUploading(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  }

  async function scanInbox() {
    if (actionRef.current) return;
    actionRef.current = true;
    setScanning(true);
    try {
      const response = await api.inboxScan();
      toast(`已为 ${response.count} 个文件创建任务`, "success");
      await load();
    } catch (reason: unknown) {
      toast(`扫描失败：${errMsg(reason)}`, "error");
    } finally {
      actionRef.current = false;
      setScanning(false);
    }
  }

  async function retryTask(id: number) {
    if (retrying === id) return;
    setRetrying(id);
    try {
      const response = await api.taskRetry(id);
      toast(`已创建重试任务 #${response.new_task_id}`, "success");
      await load();
      if (taskParam === String(id)) setDetailReload((value) => value + 1);
    } catch (reason: unknown) {
      toast(`重试失败：${errMsg(reason)}`, "error");
    } finally {
      setRetrying(null);
    }
  }

  function openTask(id: number) {
    const next = new URLSearchParams(searchParams);
    next.set("task", String(id));
    setSearchParams(next);
  }

  const tasks = result?.items ?? [];
  const total = result?.total ?? result?.count ?? 0;

  return (
    <div className="space-y-4 p-4 sm:p-6">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold">任务面板</h1>
          <p className="text-sm text-gray-400">
            共 {total} 个任务 · {inbox.length} 个待投递 PDF ·
            <span className={connected ? "ml-2 text-green-300" : "ml-2 text-amber-300"} role="status">
              {connected ? "实时更新已连接" : "实时更新未连接"}
            </span>
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <input ref={fileRef} type="file" accept=".pdf,application/pdf" className="hidden" onChange={handleUpload} aria-label="选择要上传的 PDF" />
          <button type="button" className="btn-primary" onClick={() => fileRef.current?.click()} disabled={uploading}>
            <Upload size={14} aria-hidden="true" /> {uploading ? "上传中…" : "上传 PDF"}
          </button>
          <button type="button" className="btn" onClick={scanInbox} disabled={inbox.length === 0 || scanning || uploading}>
            <Play size={14} aria-hidden="true" /> {scanning ? "扫描中…" : `扫描投递目录 (${inbox.length})`}
          </button>
          <button type="button" className="btn px-2" onClick={load} disabled={loading} aria-label="刷新任务列表" title="刷新任务列表">
            <RefreshCw size={14} className={loading ? "animate-spin" : ""} aria-hidden="true" />
          </button>
        </div>
      </div>

      {inbox.length > 0 && (
        <section className="card">
          <h2 className="mb-2 text-sm font-medium">投递目录中的待处理文件</h2>
          <ul className="space-y-1 text-xs">
            {inbox.map((item) => (
              <li key={item.path} className="flex flex-wrap justify-between gap-2 font-mono text-gray-300">
                <span className="min-w-0 break-all">{item.name}</span>
                <span className="shrink-0 text-gray-400">{(item.size_bytes / 1024).toFixed(0)} KB</span>
              </li>
            ))}
          </ul>
        </section>
      )}

      <section className="card flex flex-wrap gap-3" aria-label="任务筛选">
        <FormField label="状态">
          <select className="input w-36" value={statusFilter} onChange={(event) => { setStatusFilter(event.target.value); setPage(0); }}>
            {STATUS_OPTIONS.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
          </select>
        </FormField>
        <FormField label="类型">
          <select className="input w-36" value={kindFilter} onChange={(event) => { setKindFilter(event.target.value); setPage(0); }}>
            {KIND_OPTIONS.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}
          </select>
        </FormField>
      </section>

      {error && <ErrorState message={error} onRetry={load} />}
      {loading && !result ? <LoadingState label="正在读取任务…" /> : tasks.length === 0 ? (
        <EmptyState title="没有符合条件的任务" detail="可以清空筛选，或上传新的 PDF。" />
      ) : (
        <>
          <div className="card hidden overflow-x-auto p-0 md:block" aria-busy={loading}>
            <table className="w-full min-w-[1050px] text-sm">
              <thead className="bg-gray-900/60 text-left text-gray-300">
                <tr><th className="px-3 py-2">编号</th><th className="px-3 py-2">类型</th><th className="px-3 py-2">状态</th><th className="px-3 py-2">进度</th><th className="px-3 py-2">论文</th><th className="px-3 py-2">创建时间</th><th className="px-3 py-2">最近事件或错误</th><th className="px-3 py-2">操作</th></tr>
              </thead>
              <tbody>
                {tasks.map((task) => (
                  <tr key={task.id} className="border-t border-side-border hover:bg-gray-800/40">
                    <td className="px-3 py-2 font-mono text-gray-400">#{task.id}</td>
                    <td className="px-3 py-2 text-gray-200">{KIND_LABELS[task.kind] || task.kind}</td>
                    <td className="px-3 py-2"><TaskStatusBadge status={task.status} /></td>
                    <td className="px-3 py-2"><Progress value={task.progress} /></td>
                    <td className="px-3 py-2 font-mono text-gray-400">{task.paper_id ? <Link className="text-blue-300 hover:underline" to={`/paper/${task.paper_id}`}>#{task.paper_id}</Link> : "—"}</td>
                    <td className="px-3 py-2 text-xs text-gray-400">{formatDateTime(task.created_at)}</td>
                    <td className="max-w-md truncate px-3 py-2 text-xs text-gray-300" title={task.error || latestEventFor(events, task.id)}>
                      {task.error ? <span className="text-red-300">{task.error}</span> : latestEventFor(events, task.id) || "—"}
                    </td>
                    <td className="space-x-1 px-3 py-2">
                      <button type="button" className="btn px-2 py-0.5 text-xs" onClick={() => openTask(task.id)}>详情</button>
                      {(task.status === "error" || task.status === "cancelled") && <button type="button" className="btn px-2 py-0.5 text-xs" onClick={() => retryTask(task.id)} disabled={retrying === task.id}>重试</button>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="space-y-2 md:hidden">
            {tasks.map((task) => (
              <article key={task.id} className="card space-y-2">
                <div className="flex items-center justify-between gap-2"><span className="font-mono text-xs text-gray-400">#{task.id} · {KIND_LABELS[task.kind] || task.kind}</span><TaskStatusBadge status={task.status} /></div>
                <Progress value={task.progress} />
                <p className="text-xs text-gray-400">{formatDateTime(task.created_at)}</p>
                {task.error && <p className="break-words text-xs text-red-300">{task.error}</p>}
                <div className="flex flex-wrap gap-2">
                  <button type="button" className="btn text-xs" onClick={() => openTask(task.id)}>详情</button>
                  {task.paper_id && <Link className="btn text-xs" to={`/paper/${task.paper_id}`}>论文 #{task.paper_id}</Link>}
                  {(task.status === "error" || task.status === "cancelled") && <button type="button" className="btn text-xs" onClick={() => retryTask(task.id)}>重试</button>}
                </div>
              </article>
            ))}
          </div>
        </>
      )}
      <Pagination page={page} pageSize={PAGE_SIZE} total={total} onChange={setPage} itemLabel="个任务" />

      <section className="card">
        <h2 className="mb-2 flex items-center gap-2 text-sm font-medium"><FileText size={14} aria-hidden="true" /> 实时事件 ({events.length})</h2>
        <div className="max-h-64 space-y-1 overflow-y-auto font-mono text-xs" aria-live="polite">
          {[...events].reverse().slice(0, 50).map((event) => (
            <div key={event.id} className={`flex flex-wrap gap-2 ${levelColor(event.level)}`}>
              <span className="text-gray-400">[{formatDateTime(event.ts)}]</span>
              {event.task_id && <button type="button" className="text-blue-300 hover:underline" onClick={() => openTask(event.task_id!)}>任务 #{event.task_id}</button>}
              <span className="min-w-0 break-words">{event.msg}</span>
            </div>
          ))}
          {events.length === 0 && <p className="text-gray-400">等待任务事件…</p>}
        </div>
      </section>

      <Dialog open={!!taskParam} onClose={closeDetail} title={`任务 #${taskParam || ""}`}>
        {detailLoading ? <LoadingState label="正在读取任务详情…" /> : detailError ? <ErrorState message={detailError} onRetry={() => setDetailReload((value) => value + 1)} /> : detail && (
          <TaskDetail task={detail} onRetry={() => retryTask(detail.id)} retrying={retrying === detail.id} />
        )}
      </Dialog>
    </div>
  );
}

function TaskDetail({ task, onRetry, retrying }: {
  task: Task;
  onRetry: () => void;
  retrying: boolean;
}) {
  let payload = task.payload;
  if (!payload) {
    try { payload = JSON.parse(task.payload_json || "{}"); } catch { payload = {}; }
  }
  return (
    <div className="space-y-4 text-sm">
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <Detail label="类型" value={KIND_LABELS[task.kind] || task.kind} />
        <Detail label="状态" value={<TaskStatusBadge status={task.status} />} />
        <Detail label="论文" value={task.paper_id ? <Link className="text-blue-300 hover:underline" to={`/paper/${task.paper_id}`}>论文 #{task.paper_id}</Link> : "—"} />
        <Detail label="重试次数" value={String(task.retries)} />
        <Detail label="创建时间" value={formatDateTime(task.created_at)} />
        <Detail label="开始时间" value={formatDateTime(task.started_at)} />
        <Detail label="结束时间" value={formatDateTime(task.finished_at)} />
        <Detail label="进度" value={<Progress value={task.progress} />} />
      </div>
      {task.error && <section><h3 className="mb-1 text-xs text-gray-400">完整错误</h3><pre className="whitespace-pre-wrap break-words rounded border border-red-900 bg-red-950/30 p-3 text-xs text-red-200">{task.error}</pre></section>}
      <section><h3 className="mb-1 text-xs text-gray-400">任务参数</h3><pre className="overflow-x-auto rounded border border-side-border bg-gray-950/50 p-3 text-xs text-gray-200">{JSON.stringify(payload, null, 2)}</pre></section>
      <section><h3 className="mb-1 text-xs text-gray-400">最近日志</h3><pre className="max-h-64 overflow-auto whitespace-pre-wrap break-words rounded border border-side-border bg-gray-950/50 p-3 text-xs text-gray-300">{task.log_tail || "暂无日志"}</pre></section>
      {(task.status === "error" || task.status === "cancelled") && <button type="button" className="btn-primary" onClick={onRetry} disabled={retrying}>{retrying ? "重试中…" : "创建重试任务"}</button>}
    </div>
  );
}

function Detail({ label, value }: { label: string; value: React.ReactNode }) {
  return <div><div className="text-xs text-gray-400">{label}</div><div className="mt-0.5 text-gray-200">{value}</div></div>;
}

function Progress({ value }: { value: number }) {
  const percent = Math.round(value * 100);
  return (
    <div className="flex min-w-28 items-center gap-2">
      <div className="h-1.5 flex-1 overflow-hidden rounded bg-gray-800" role="progressbar" aria-label="任务进度" aria-valuemin={0} aria-valuemax={100} aria-valuenow={percent}>
        <div className="h-full bg-blue-500 transition-all" style={{ width: `${percent}%` }} />
      </div>
      <span className="w-10 text-xs text-gray-400">{percent}%</span>
    </div>
  );
}

function TaskStatusBadge({ status }: { status: Task["status"] }) {
  const config = {
    pending: { cls: "badge-gray", icon: <Clock size={11} />, label: "等待执行" },
    running: { cls: "badge-blue", icon: <RefreshCw size={11} className="animate-spin" />, label: "正在执行" },
    ok: { cls: "badge-green", icon: <CheckCircle2 size={11} />, label: "已完成" },
    error: { cls: "badge-red", icon: <XCircle size={11} />, label: "失败" },
    cancelled: { cls: "badge-amber", icon: <AlertCircle size={11} />, label: "已取消" },
  }[status];
  return <span className={`${config.cls} inline-flex items-center gap-1`}><span aria-hidden="true">{config.icon}</span>{config.label}</span>;
}

function latestEventFor(events: SSEvent[], taskId: number): string {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    if (events[index].task_id === taskId) return events[index].msg;
  }
  return "";
}

function levelColor(level: string) {
  return { info: "text-gray-300", warn: "text-amber-300", error: "text-red-300", err: "text-red-300" }[level] || "text-gray-400";
}
