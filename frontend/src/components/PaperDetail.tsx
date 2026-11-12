import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { Link, useBlocker } from "react-router-dom";
import {
  BookOpen,
  Check,
  ChevronDown,
  Copy,
  Cog,
  ExternalLink,
  FileImage,
  FileText,
  Languages,
  NotebookPen,
  RefreshCw,
  Wand2,
  Zap,
} from "lucide-react";
import { api, errMsg, type Paper, type TranslationChunk } from "@/lib/api";
import { useEventStream } from "@/lib/sse";
import { useToast } from "@/components/toast-context";
import { PaperStatusRow } from "@/components/StatusBadge";
import { PipelineStepper } from "@/components/PipelineStepper";
import { DocReader, type DocDraft, type DocType } from "@/components/DocReader";

type Tab = "info" | DocType;
type ActionKind = "reparse" | "reenrich" | "retranslate" | "resummarize" | "reembed" | "pipeline";

interface PaperDetailProps {
  paper: Paper;
  initialTab?: Tab;
  onDirtyChange?: (dirty: boolean) => void;
}

export function PaperDetail({ paper, initialTab = "info", onDirtyChange }: PaperDetailProps) {
  const { toast } = useToast();
  const [tab, setTab] = useState<Tab>(initialTab);
  const [livePaper, setLivePaper] = useState<{ id: number; data: Paper } | null>(null);
  const [chunks, setChunks] = useState<TranslationChunk[]>([]);
  const [hasEnOverride, setHasEnOverride] = useState(false);
  const [drafts, setDrafts] = useState<Partial<Record<DocType, DocDraft>>>({});
  const [refreshKey, setRefreshKey] = useState(0);
  const [pendingAction, setPendingAction] = useState<ActionKind | null>(null);
  const [taskLinks, setTaskLinks] = useState<number[]>([]);
  const [refreshing, setRefreshing] = useState(false);
  const actionMenuRef = useRef<HTMLDetailsElement>(null);
  const pendingActionRef = useRef<ActionKind | null>(null);
  const draftsRef = useRef(drafts);
  const chunkRequestRef = useRef(0);
  const lastEventRef = useRef(0);
  const currentPaper = livePaper?.id === paper.id ? livePaper.data : paper;
  const anyDirty = Object.values(drafts).some((draft) => draft?.dirty);

  const loadChunks = useCallback(async () => {
    const requestId = ++chunkRequestRef.current;
    try {
      const response = await api.translations(paper.id);
      if (requestId !== chunkRequestRef.current) return;
      const override = response.items.find((chunk) => chunk.chunk_index === -1);
      setHasEnOverride(!!override && override.en_len > 0);
      setChunks(response.items.filter((chunk) => chunk.chunk_index >= 0));
    } catch {
      if (requestId === chunkRequestRef.current) setHasEnOverride(false);
    }
  }, [paper.id]);

  useEffect(() => {
    queueMicrotask(() => { void loadChunks(); });
  }, [loadChunks]);

  const refreshPaper = useCallback(async () => {
    setRefreshing(true);
    try {
      const next = await api.paper(paper.id);
      setLivePaper({ id: next.id, data: next });
      await loadChunks();
      setRefreshKey((value) => value + 1);
    } catch (error: unknown) {
      toast(`论文状态刷新失败：${errMsg(error)}`, "error");
    } finally {
      setRefreshing(false);
    }
  }, [paper.id, loadChunks, toast]);

  const { events } = useEventStream({ paperId: paper.id, maxEvents: 100 });
  useEffect(() => {
    const fresh = events.filter((event) => event.id > lastEventRef.current);
    if (fresh.length === 0) return;
    lastEventRef.current = fresh[fresh.length - 1].id;
    if (!fresh.some((event) => /^(done|failed)|pipeline done/.test(event.msg))) return;
    queueMicrotask(() => { void refreshPaper(); });
  }, [events, refreshPaper]);

  const blocker = useBlocker(anyDirty);
  useEffect(() => {
    if (blocker.state !== "blocked") return;
    if (window.confirm("当前论文有未保存的文档修改。确定离开并放弃这些修改吗？")) blocker.proceed();
    else blocker.reset();
  }, [blocker]);

  useEffect(() => {
    if (!anyDirty) return;
    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [anyDirty]);

  function updateDraft(docType: DocType, draft: DocDraft) {
    const next = { ...draftsRef.current, [docType]: draft };
    draftsRef.current = next;
    setDrafts(next);
    onDirtyChange?.(Object.values(next).some((item) => item?.dirty));
  }

  async function trigger(kind: ActionKind) {
    if (pendingActionRef.current) return;
    pendingActionRef.current = kind;
    setPendingAction(kind);
    actionMenuRef.current?.removeAttribute("open");
    try {
      let result;
      if (kind === "pipeline") result = await api.pipeline(paper.id, { on_error: "continue" });
      else if (kind === "reparse") result = await api.reparse(paper.id);
      else if (kind === "reenrich") result = await api.reenrich(paper.id);
      else if (kind === "retranslate") result = await api.retranslate(paper.id);
      else if (kind === "resummarize") result = await api.resummarize(paper.id);
      else result = await api.reembed(paper.id);
      setTaskLinks((items) => [result.task_id, ...items.filter((id) => id !== result.task_id)]);
      toast(result.reused ? `已有相同任务 #${result.task_id}` : `已创建任务 #${result.task_id}`, "success");
    } catch (error: unknown) {
      toast(`操作失败：${errMsg(error)}`, "error");
    } finally {
      pendingActionRef.current = null;
      setPendingAction(null);
    }
  }

  const actionDisabled = pendingAction !== null;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-start gap-3">
        <div className="min-w-0 flex-1 space-y-1">
          <h1 className="text-xl font-semibold leading-snug text-gray-100">{currentPaper.title || currentPaper.dir_name}</h1>
          <p className="text-sm text-gray-400">
            {[currentPaper.first_author, currentPaper.year, currentPaper.journal].filter(Boolean).join(" · ")}
          </p>
          <div className="flex flex-wrap items-center gap-2 text-xs text-gray-400">
            <span className="font-mono">{currentPaper.dir_name}</span>
            {currentPaper.attachment_kind === "si" && <span className="badge-amber">SI 附件</span>}
            <PaperStatusRow p={currentPaper} />
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <button type="button" className="btn px-2" onClick={refreshPaper} disabled={refreshing} aria-label="刷新论文状态" title="刷新论文状态">
            <RefreshCw size={14} className={refreshing ? "animate-spin" : ""} aria-hidden="true" />
          </button>
          <button type="button" className="btn-primary text-xs" onClick={() => trigger("pipeline")} disabled={actionDisabled}>
            <Zap size={12} aria-hidden="true" /> {pendingAction === "pipeline" ? "创建中…" : "一键流程"}
          </button>
          <details ref={actionMenuRef} className="relative">
            <summary className="btn cursor-pointer list-none text-xs" aria-label="更多论文操作">
              <ChevronDown size={12} aria-hidden="true" /> 更多操作
            </summary>
            <div className="card absolute right-0 z-20 mt-1 flex w-40 flex-col gap-1 p-1 shadow-xl">
              <ActionButton label="重新解析" icon={<Cog size={12} />} onClick={() => trigger("reparse")} disabled={actionDisabled} />
              <ActionButton label="重新富化" icon={<Wand2 size={12} />} onClick={() => trigger("reenrich")} disabled={actionDisabled} />
              <ActionButton
                label={hasEnOverride ? "翻译（使用复核版）" : "翻译全文"}
                icon={<Languages size={12} />}
                onClick={() => trigger("retranslate")}
                disabled={actionDisabled}
              />
              <ActionButton label="生成总结" icon={<NotebookPen size={12} />} onClick={() => trigger("resummarize")} disabled={actionDisabled} />
              <ActionButton label="重新生成向量" icon={<Zap size={12} />} onClick={() => trigger("reembed")} disabled={actionDisabled} />
            </div>
          </details>
        </div>
      </div>

      {taskLinks.length > 0 && (
        <div className="flex flex-wrap items-center gap-2 text-xs text-gray-400">
          <span>最近任务：</span>
          {taskLinks.slice(0, 4).map((id) => <Link key={id} to={`/tasks?task=${id}`} className="text-blue-300 hover:underline">#{id}</Link>)}
        </div>
      )}

      <PipelineStepper p={currentPaper} />

      <div className="flex flex-wrap gap-1 border-b border-side-border pb-2 text-sm" role="tablist" aria-label="论文内容">
        <TabBtn active={tab === "info"} onClick={() => setTab("info")}><FileText size={14} />信息</TabBtn>
        <TabBtn active={tab === "en"} onClick={() => setTab("en")} dirty={drafts.en?.dirty}><FileImage size={14} />原文 (EN)</TabBtn>
        <TabBtn active={tab === "zh"} onClick={() => setTab("zh")} dirty={drafts.zh?.dirty}><Languages size={14} />翻译 (ZH)</TabBtn>
        <TabBtn active={tab === "summary"} onClick={() => setTab("summary")} dirty={drafts.summary?.dirty}><BookOpen size={14} />总结</TabBtn>
      </div>

      {tab === "info" && <InfoTab key={currentPaper.id} paper={currentPaper} />}
      {(["en", "zh", "summary"] as DocType[]).map((docType) => tab === docType && (
        <div key={docType}>
          {docType === "zh" && chunks.length > 0 && <ChunkPanel chunks={chunks} />}
          <DocReader
            key={`${paper.id}:${docType}`}
            paperId={paper.id}
            docType={docType}
            draft={drafts[docType]}
            onDraftChange={(draft) => updateDraft(docType, draft)}
            refreshKey={refreshKey}
          />
        </div>
      ))}
    </div>
  );
}

function ActionButton({ label, icon, onClick, disabled }: {
  label: string;
  icon: ReactNode;
  onClick: () => void;
  disabled: boolean;
}) {
  return (
    <button type="button" className="flex items-center gap-2 rounded px-2 py-2 text-left text-xs text-gray-200 hover:bg-gray-800" onClick={onClick} disabled={disabled}>
      <span aria-hidden="true">{icon}</span>{label}
    </button>
  );
}

function TabBtn({ active, onClick, children, dirty }: {
  active: boolean;
  onClick: () => void;
  children: ReactNode;
  dirty?: boolean;
}) {
  return (
    <button
      type="button"
      role="tab"
      aria-selected={active}
      onClick={onClick}
      className={`inline-flex items-center gap-1.5 rounded px-3 py-1 text-sm ${active ? "border border-blue-700 bg-blue-800/40 text-blue-100" : "border border-transparent text-gray-300 hover:text-gray-100"}`}
    >
      {children}
      {dirty && <span className="text-amber-300" title="有未保存修改">●<span className="sr-only">有未保存修改</span></span>}
    </button>
  );
}

function InfoTab({ paper }: { paper: Paper }) {
  const [page, setPage] = useState(1);
  const [imgErr, setImgErr] = useState(false);
  const maxPage = paper.pdf_pages ?? 1;
  const authors = Array.isArray(paper.authors) ? paper.authors : [];
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <div className="card flex flex-col items-center gap-2">
          <div className="self-start text-xs text-gray-400">PDF 预览</div>
          {paper.pdf_path && !imgErr ? (
            <>
              <img
                src={api.pdfUrl(paper.id, page, 100)}
                className="max-h-[500px] w-full rounded border border-side-border object-contain"
                alt={`PDF 第 ${page} 页`}
                onError={() => setImgErr(true)}
              />
              <div className="flex flex-wrap items-center justify-center gap-2">
                <button type="button" className="btn text-xs" disabled={page <= 1} onClick={() => { setPage(page - 1); setImgErr(false); }}>上一页</button>
                <label className="flex items-center gap-1 text-xs text-gray-400">
                  <input
                    type="number"
                    className="input w-16"
                    min={1}
                    max={maxPage}
                    value={page}
                    aria-label="PDF 页码"
                    onChange={(event) => {
                      const next = Math.max(1, Math.min(maxPage, Number(event.target.value) || 1));
                      setPage(next);
                      setImgErr(false);
                    }}
                  />
                  / {maxPage}
                </label>
                <button type="button" className="btn text-xs" disabled={page >= maxPage} onClick={() => { setPage(page + 1); setImgErr(false); }}>下一页</button>
              </div>
            </>
          ) : (
            <div className="flex flex-col items-center justify-center gap-2 py-10 text-gray-400">
              <FileImage size={32} aria-hidden="true" />
              <span className="text-xs">{imgErr ? "PDF 加载失败" : "无 PDF"}</span>
            </div>
          )}
        </div>
        <div className="space-y-2 text-sm">
          <MetaItem label="标题" value={paper.title || "(无标题)"} />
          <MetaItem label="目录" value={paper.dir_name} mono />
          <MetaItem label="第一作者" value={paper.first_author} />
          <MetaItem label="所有作者" value={authors.slice(0, 8).join(", ") + (authors.length > 8 ? ` …(+${authors.length - 8})` : "")} />
          <MetaItem label="期刊" value={paper.journal || "—"} />
          <MetaItem label="年份" value={String(paper.year)} />
          <MetaItem label="DOI" value={paper.doi ? <a className="text-blue-300 hover:underline" href={`https://doi.org/${paper.doi}`} target="_blank" rel="noreferrer">{paper.doi}</a> : "—"} />
          <MetaItem label="arXiv ID" value={paper.arxiv_id || "—"} />
          <MetaItem label="OpenAlex ID" value={paper.openalex_id ? <a className="text-blue-300 hover:underline" href={paper.openalex_id} target="_blank" rel="noreferrer">{paper.openalex_id.replace("https://openalex.org/", "")}</a> : "—"} />
          {paper.url && <MetaItem label="原文链接" value={<a className="inline-flex items-center gap-1 text-blue-300 hover:underline" href={paper.url} target="_blank" rel="noreferrer">打开来源 <ExternalLink size={12} /></a>} />}
          {paper.parent_paper_id && <MetaItem label="母论文" value={<Link className="text-blue-300 hover:underline" to={`/paper/${paper.parent_paper_id}`}>论文 #{paper.parent_paper_id}</Link>} />}
          <MetaItem label="引用数" value={paper.cited_by_count != null ? String(paper.cited_by_count) : "—"} />
          <MetaItem label="OA 状态" value={paper.oa_status || "—"} />
          <MetaItem label="PDF 路径" value={paper.pdf_path ?? "—"} mono small />
          <MetaItem label="解析路径" value={paper.parsed_path ?? "—"} mono small />
          <MetaItem label="PDF 大小" value={paper.pdf_size_bytes ? `${(paper.pdf_size_bytes / 1024).toFixed(0)} KB` : "—"} />
          <MetaItem label="解析字符数" value={paper.parsed_chars ? `${paper.parsed_chars.toLocaleString()} 字符` : "—"} />
          <MetaItem label="MinerU 模型" value={paper.mineru_model || "—"} />
        </div>
      </div>
      {paper.abstract && (
        <details className="card">
          <summary className="cursor-pointer text-sm font-medium text-gray-200">摘要</summary>
          <p className="mt-2 whitespace-pre-wrap text-sm leading-relaxed text-gray-300">{paper.abstract}</p>
        </details>
      )}
      <BibtexCard key={`${paper.id}:${paper.updated_at}`} paper={paper} />
    </div>
  );
}

function MetaItem({ label, value, mono, small }: {
  label: string;
  value: ReactNode;
  mono?: boolean;
  small?: boolean;
}) {
  return (
    <div className="flex flex-col gap-0.5">
      <div className="text-xs text-gray-400">{label}</div>
      <div className={`${mono ? "font-mono" : ""} ${small ? "text-xs" : "text-sm"} break-words text-gray-200`}>{value}</div>
    </div>
  );
}

function ChunkPanel({ chunks }: { chunks: TranslationChunk[] }) {
  const labels: Record<string, string> = { ok: "完成", error: "失败", pending: "等待", skipped: "跳过" };
  return (
    <div className="card">
      <div className="mb-2 flex items-center justify-between">
        <h3 className="text-sm font-medium">翻译块 ({chunks.length})</h3>
        <div className="flex gap-3 text-xs text-gray-400">
          {Object.entries(chunks.reduce<Record<string, number>>((acc, chunk) => {
            acc[chunk.status] = (acc[chunk.status] || 0) + 1;
            return acc;
          }, {})).map(([status, count]) => <span key={status}>{labels[status] || status}: {count}</span>)}
        </div>
      </div>
      <div className="grid grid-cols-8 gap-1 sm:grid-cols-12">
        {chunks.map((chunk) => (
          <div key={chunk.chunk_index} className={`flex h-7 items-center justify-center rounded text-xs ${chunkBg(chunk.status)}`} title={`#${chunk.chunk_index} ${labels[chunk.status] || chunk.status}，英文 ${chunk.en_len} 字符，中文 ${chunk.zh_len} 字符`}>
            {chunk.chunk_index}
          </div>
        ))}
      </div>
    </div>
  );
}

function chunkBg(status: string) {
  return {
    ok: "bg-green-700/50 text-green-100",
    error: "bg-red-700/50 text-red-100",
    pending: "bg-blue-700/50 text-blue-100",
    skipped: "bg-gray-700/40 text-gray-300",
  }[status] || "bg-gray-800";
}

function BibtexCard({ paper }: { paper: Paper }) {
  const [bibtex, setBibtex] = useState(paper.bibtex ?? "");
  const [loading, setLoading] = useState(false);
  const [copied, setCopied] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const loadingRef = useRef(false);

  async function refresh() {
    if (loadingRef.current) return;
    loadingRef.current = true;
    setLoading(true);
    setMsg(null);
    try {
      const result = await api.refreshBibtex(paper.id);
      setBibtex(result.bibtex);
      setMsg("已刷新");
    } catch (error: unknown) {
      setMsg(`刷新失败：${errMsg(error)}`);
    } finally {
      loadingRef.current = false;
      setLoading(false);
    }
  }

  async function copy() {
    if (!bibtex) return;
    try {
      await navigator.clipboard.writeText(bibtex);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      setMsg("复制失败");
    }
  }

  return (
    <div className="card">
      <div className="mb-2 flex items-center justify-between">
        <h3 className="text-sm font-medium">BibTeX</h3>
        <div className="flex items-center gap-2">
          {bibtex && <button type="button" className="btn text-xs" onClick={copy}>{copied ? <Check size={12} /> : <Copy size={12} />}{copied ? "已复制" : "复制"}</button>}
          {paper.doi && <button type="button" className="btn text-xs" onClick={refresh} disabled={loading}><RefreshCw size={12} className={loading ? "animate-spin" : ""} />{bibtex ? "刷新" : "获取"}</button>}
        </div>
      </div>
      {bibtex ? <pre className="max-h-[220px] overflow-y-auto whitespace-pre-wrap text-xs leading-relaxed text-gray-200">{bibtex}</pre> : <p className="py-3 text-xs text-gray-400">{paper.doi ? "尚未获取，请点击获取。" : "无 DOI，无法从 Crossref 获取 BibTeX。"}</p>}
      {msg && <p className="mt-1 text-xs text-amber-300">{msg}</p>}
    </div>
  );
}
