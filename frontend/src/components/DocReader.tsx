import { useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle,
  Code2,
  Eye,
  History,
  MessageSquarePlus,
  Pencil,
  RotateCcw,
  Save,
  Trash2,
  X,
} from "lucide-react";
import { api, ApiError, errMsg } from "@/lib/api";
import type { Annotation, DocResp, DocRevision } from "@/lib/api";
import { parseAnnotation, type TextSelection } from "@/lib/annotations";
import { formatDateTime } from "@/lib/datetime";
import { MarkdownView } from "@/components/MarkdownView";
import { Dialog } from "@/components/Dialog";
import { ErrorState, LoadingState } from "@/components/Feedback";
import { useToast } from "@/components/toast-context";

type View = "render" | "raw" | "edit";
export type DocType = "en" | "zh" | "summary";

export interface DocDraft {
  text: string;
  dirty: boolean;
  baseHash: string | null;
}

interface DocReaderProps {
  paperId: number;
  docType: DocType;
  draft?: DocDraft;
  onDraftChange?: (draft: DocDraft) => void;
  refreshKey?: number;
}

export function DocReader({ paperId, docType, draft, onDraftChange, refreshKey = 0 }: DocReaderProps) {
  const { toast } = useToast();
  const [doc, setDoc] = useState<DocResp | null>(null);
  const [view, setView] = useState<View>("render");
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [editText, setEditText] = useState(draft?.text ?? "");
  const [dirty, setDirty] = useState(draft?.dirty ?? false);
  const [baseHash, setBaseHash] = useState<string | null>(draft?.baseHash ?? null);
  const [saving, setSaving] = useState(false);
  const [revisions, setRevisions] = useState<DocRevision[]>([]);
  const [annotations, setAnnotations] = useState<Annotation[]>([]);
  const [showRevisions, setShowRevisions] = useState(false);
  const [pendingSelection, setPendingSelection] = useState<TextSelection | null>(null);
  const [noteText, setNoteText] = useState("");
  const [creatingAnnotation, setCreatingAnnotation] = useState(false);
  const [focusAnnotationId, setFocusAnnotationId] = useState<number | null>(null);
  const [suggestRetranslate, setSuggestRetranslate] = useState(false);
  const [reembedTaskId, setReembedTaskId] = useState<number | null>(null);
  const [conflict, setConflict] = useState<{ message: string; currentHash: string | null } | null>(null);
  const [previewRevision, setPreviewRevision] = useState<DocRevision | null>(null);
  const [previewLoading, setPreviewLoading] = useState<number | null>(null);
  const [restoring, setRestoring] = useState(false);

  const editTextRef = useRef(editText);
  const dirtyRef = useRef(dirty);
  const baseHashRef = useRef(baseHash);
  const docRef = useRef(doc);
  const loadIdRef = useRef(0);
  const draftCallbackRef = useRef(onDraftChange);

  useEffect(() => { draftCallbackRef.current = onDraftChange; }, [onDraftChange]);

  function publishDraft(text: string, isDirty: boolean, hash: string | null) {
    editTextRef.current = text;
    dirtyRef.current = isDirty;
    baseHashRef.current = hash;
    setEditText(text);
    setDirty(isDirty);
    setBaseHash(hash);
    draftCallbackRef.current?.({ text, dirty: isDirty, baseHash: hash });
  }

  async function load(replaceDraft = false) {
    const loadId = ++loadIdRef.current;
    setLoading(true);
    setLoadError(null);
    try {
      const nextDoc = await api.doc(paperId, docType);
      if (loadId !== loadIdRef.current) return;
      docRef.current = nextDoc;
      setDoc(nextDoc);
      if (replaceDraft || !dirtyRef.current) publishDraft(nextDoc.text, false, nextDoc.hash);
      const [revisionResult, annotationResult] = await Promise.allSettled([
        api.revisions(paperId, docType),
        api.annotations(paperId),
      ]);
      if (loadId !== loadIdRef.current) return;
      setRevisions(revisionResult.status === "fulfilled" ? revisionResult.value.items : []);
      setAnnotations(annotationResult.status === "fulfilled"
        ? annotationResult.value.items.filter((item) => !item.anchor || item.anchor === docType)
        : []);
    } catch (error: unknown) {
      if (loadId === loadIdRef.current) setLoadError(errMsg(error));
    } finally {
      if (loadId === loadIdRef.current) setLoading(false);
    }
  }

  useEffect(() => {
    queueMicrotask(() => { void load(); });
    return () => { loadIdRef.current += 1; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [paperId, docType, refreshKey]);

  async function loadAnnotations() {
    try {
      const response = await api.annotations(paperId);
      setAnnotations(response.items.filter((item) => !item.anchor || item.anchor === docType));
    } catch (error: unknown) {
      toast(`批注读取失败：${errMsg(error)}`, "error");
    }
  }

  async function save(overrideHash?: string | null) {
    if (!docRef.current || saving) return;
    const submittedText = editTextRef.current;
    const submittedHash = overrideHash !== undefined ? overrideHash : baseHashRef.current;
    setSaving(true);
    setConflict(null);
    try {
      const result = await api.docSave(paperId, docType, submittedText, submittedHash);
      const latest = await api.doc(paperId, docType);
      docRef.current = latest;
      setDoc(latest);
      if (editTextRef.current === submittedText) {
        publishDraft(latest.text, false, latest.hash);
      } else {
        publishDraft(editTextRef.current, true, latest.hash);
      }
      setSuggestRetranslate(result.suggest_retranslate);
      setReembedTaskId(result.reembed_task_id);
      try {
        const revisionResult = await api.revisions(paperId, docType);
        setRevisions(revisionResult.items);
      } catch {
        setRevisions([]);
      }
      toast(result.changed ? `已保存（修订 #${result.revision_id}）` : "内容无变化", "success");
    } catch (error: unknown) {
      if (error instanceof ApiError && error.status === 409) {
        const currentHash = typeof error.data === "object" && error.data !== null && "current_hash" in error.data
          ? (error.data as { current_hash: string | null }).current_hash
          : null;
        setConflict({ message: error.message, currentHash });
      } else {
        toast(`保存失败：${errMsg(error)}`, "error");
      }
    } finally {
      setSaving(false);
    }
  }

  function discardDraft() {
    if (dirtyRef.current && !window.confirm("确定放弃当前文档的未保存修改吗？")) return;
    const current = docRef.current;
    if (current) publishDraft(current.text, false, current.hash);
    setConflict(null);
    setView("render");
  }

  async function createAnnotation() {
    if (!pendingSelection || creatingAnnotation) return;
    setCreatingAnnotation(true);
    try {
      await api.annotationCreate(paperId, {
        type: "highlight",
        anchor: docType,
        quote: pendingSelection.quote,
        note: noteText,
        position: pendingSelection.position,
      });
      setPendingSelection(null);
      setNoteText("");
      await loadAnnotations();
      toast("批注已添加", "success");
    } catch (error: unknown) {
      toast(`批注失败：${errMsg(error)}`, "error");
    } finally {
      setCreatingAnnotation(false);
    }
  }

  async function deleteAnnotation(id: number) {
    if (!window.confirm("确定删除这条批注吗？")) return;
    try {
      await api.annotationDelete(id);
      if (focusAnnotationId === id) setFocusAnnotationId(null);
      await loadAnnotations();
      toast("批注已删除", "success");
    } catch (error: unknown) {
      toast(`删除失败：${errMsg(error)}`, "error");
    }
  }

  async function preview(revision: DocRevision) {
    setPreviewLoading(revision.id);
    try {
      const detail = await api.revision(revision.id);
      setPreviewRevision(detail);
    } catch (error: unknown) {
      toast(`修订读取失败：${errMsg(error)}`, "error");
    } finally {
      setPreviewLoading(null);
    }
  }

  async function restore() {
    if (!previewRevision || restoring) return;
    if (dirtyRef.current && !window.confirm("当前草稿尚未保存。确定回滚并放弃这份草稿吗？")) return;
    if (!window.confirm(`确定回滚到修订 #${previewRevision.id} 吗？当前内容会进入新的修订记录。`)) return;
    setRestoring(true);
    try {
      await api.revisionRestore(previewRevision.id);
      setPreviewRevision(null);
      setSuggestRetranslate(false);
      setReembedTaskId(null);
      await load(true);
      toast("已回滚到所选修订", "success");
    } catch (error: unknown) {
      toast(`回滚失败：${errMsg(error)}`, "error");
    } finally {
      setRestoring(false);
    }
  }

  const annotationMarks = useMemo(
    () => annotations.map(parseAnnotation).filter((item) => item !== null),
    [annotations],
  );
  const displayText = dirty ? editText : (doc?.text ?? "");
  const viewButtons = [
    { key: "render" as View, icon: <Eye size={13} />, label: "渲染" },
    { key: "raw" as View, icon: <Code2 size={13} />, label: "原始" },
    { key: "edit" as View, icon: <Pencil size={13} />, label: "编辑" },
  ];

  if (loading && !doc) return <LoadingState label="正在读取文档…" />;
  if (loadError && !doc) return <ErrorState message={loadError} onRetry={() => load()} />;

  return (
    <div className="space-y-3">
      <div className="card flex flex-wrap items-center gap-2 py-2">
        <div className="flex gap-1" role="group" aria-label="文档视图">
          {viewButtons.map((button) => (
            <button
              type="button"
              key={button.key}
              aria-pressed={view === button.key}
              onClick={() => setView(button.key)}
              className={`inline-flex items-center gap-1 rounded px-2 py-1 text-xs transition-colors ${
                view === button.key
                  ? "border border-blue-700 bg-blue-800/40 text-blue-100"
                  : "border border-transparent text-gray-300 hover:text-gray-100"
              }`}
            >
              <span aria-hidden="true">{button.icon}</span> {button.label}
            </button>
          ))}
        </div>
        {doc?.source && <SourceTag source={doc.source} />}
        <span className="text-xs text-gray-400">{displayText.length.toLocaleString()} 字符</span>
        {dirty && view !== "edit" && <span className="text-xs text-amber-300">正在预览未保存草稿</span>}
        <div className="ml-auto flex items-center gap-1">
          <button type="button" className="btn text-xs" onClick={() => setShowRevisions((value) => !value)} aria-expanded={showRevisions}>
            <History size={12} aria-hidden="true" /> 历史 ({revisions.length})
          </button>
          {view === "edit" && (
            <>
              {dirty && <span className="text-xs text-amber-300">未保存</span>}
              <button type="button" className="btn-primary text-xs" onClick={() => save()} disabled={saving || !dirty}>
                <Save size={12} aria-hidden="true" /> {saving ? "保存中…" : "保存"}
              </button>
              <button type="button" className="btn text-xs" onClick={discardDraft}>
                <X size={12} aria-hidden="true" /> 取消
              </button>
            </>
          )}
        </div>
      </div>

      {loadError && <ErrorState message={loadError} onRetry={() => load()} />}
      {conflict && (
        <div className="card flex flex-wrap items-center gap-2 border-amber-700 text-sm text-amber-100" role="alert">
          <AlertTriangle size={16} aria-hidden="true" />
          <span className="min-w-0 flex-1">服务器内容已变化，当前草稿尚未保存。请重新读取服务器版本，或明确使用草稿覆盖。</span>
          <button type="button" className="btn text-xs" onClick={() => load(true)}>读取服务器版本</button>
          <button type="button" className="btn text-xs" onClick={() => save(conflict.currentHash)} disabled={saving}>
            使用草稿覆盖
          </button>
        </div>
      )}
      {suggestRetranslate && (
        <div className="flex items-center gap-2 rounded border border-amber-700/50 bg-amber-900/20 px-3 py-2 text-xs text-amber-100">
          <AlertTriangle size={13} className="shrink-0" aria-hidden="true" />
          EN 复核版已更新。下次翻译将使用此版本{reembedTaskId ? `，重新生成向量的任务为 #${reembedTaskId}` : ""}。
        </div>
      )}

      {pendingSelection && (
        <div className="card flex flex-wrap items-center gap-2 py-2">
          <span className="text-xs text-gray-400">选中：</span>
          <span className="line-clamp-2 min-w-0 flex-1 text-xs text-gray-200">“{pendingSelection.quote}”</span>
          <input
            className="input min-w-[160px] flex-1 text-xs"
            aria-label="批注备注"
            placeholder="备注（可选）"
            value={noteText}
            onChange={(event) => setNoteText(event.target.value)}
          />
          <button type="button" className="btn-primary text-xs" onClick={createAnnotation} disabled={creatingAnnotation}>
            <MessageSquarePlus size={12} aria-hidden="true" /> {creatingAnnotation ? "添加中…" : "高亮批注"}
          </button>
          <button type="button" className="btn px-2 text-xs" onClick={() => setPendingSelection(null)} aria-label="取消划词">
            <X size={12} aria-hidden="true" />
          </button>
        </div>
      )}

      {showRevisions && (
        <div className="card overflow-x-auto">
          <h3 className="mb-2 text-sm font-medium">修订历史 ({revisions.length})</h3>
          {revisions.length === 0 ? (
            <p className="py-3 text-xs text-gray-400">暂无修订，编辑保存后会自动记录。</p>
          ) : (
            <table className="w-full min-w-[680px] text-xs">
              <thead className="text-left text-gray-400">
                <tr><th className="px-2 py-1">编号</th><th className="px-2 py-1">文档</th><th className="px-2 py-1">来源</th><th className="px-2 py-1">摘要</th><th className="px-2 py-1">大小</th><th className="px-2 py-1">时间</th><th className="px-2 py-1 text-right">操作</th></tr>
              </thead>
              <tbody>
                {revisions.map((revision) => (
                  <tr key={revision.id} className="border-t border-side-border">
                    <td className="px-2 py-1 font-mono text-gray-400">#{revision.id}</td>
                    <td className="px-2 py-1 text-gray-200">{revision.doc_type}</td>
                    <td className="px-2 py-1 text-gray-300">{revision.source}</td>
                    <td className="px-2 py-1 font-mono text-gray-400">{revision.content_hash.slice(0, 8)}</td>
                    <td className="px-2 py-1 text-gray-400">{revision.size_bytes ? `${(revision.size_bytes / 1024).toFixed(1)} KB` : "—"}</td>
                    <td className="px-2 py-1 text-gray-400">{formatDateTime(revision.ts)}</td>
                    <td className="px-2 py-1 text-right">
                      <button type="button" className="btn px-2 py-0.5 text-xs" onClick={() => preview(revision)} disabled={previewLoading === revision.id}>
                        {previewLoading === revision.id ? "读取中…" : "预览"}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}

      {!displayText && doc && !doc.available && view !== "edit" ? (
        <div className="card py-12 text-center text-sm text-gray-400">
          {docType === "en" ? "尚无解析结果，请先执行解析。" : docType === "zh" ? "尚无翻译，请先执行翻译。" : "尚无总结，请先执行总结。"}
        </div>
      ) : view === "render" ? (
        <div className="card">
          <MarkdownView
            paperId={paperId}
            text={displayText}
            annotations={annotationMarks}
            onSelect={setPendingSelection}
            focusAnnotationId={focusAnnotationId}
          />
        </div>
      ) : view === "raw" ? (
        <div className="card">
          <pre className="max-h-[70vh] overflow-y-auto whitespace-pre-wrap text-sm leading-relaxed text-gray-200">{displayText || "(无内容)"}</pre>
        </div>
      ) : (
        <div className="card">
          <textarea
            className="h-[70vh] w-full resize-y bg-transparent font-mono text-sm leading-relaxed text-gray-200 outline-none"
            aria-label={`编辑 ${docType.toUpperCase()} 文档`}
            value={editText}
            onChange={(event) => publishDraft(event.target.value, true, baseHashRef.current)}
            spellCheck={false}
            placeholder="Markdown 内容…"
          />
        </div>
      )}

      {annotations.length > 0 && (
        <div className="card">
          <h3 className="mb-2 text-sm font-medium">批注 ({annotations.length})</h3>
          <div className="space-y-2">
            {annotationMarks.map((annotation) => (
              <div key={annotation.id} className="flex items-start gap-2 rounded border border-side-border px-2 py-1.5 text-xs">
                <div className="min-w-0 flex-1">
                  {annotation.quote && <div className="line-clamp-2 italic text-amber-100">“{annotation.quote}”</div>}
                  {annotation.note && <div className="mt-1 whitespace-pre-wrap break-words text-gray-200">{annotation.note}</div>}
                </div>
                {annotation.quote && <button type="button" className="btn px-2 py-0.5 text-xs" onClick={() => setFocusAnnotationId(annotation.id)}>定位</button>}
                <button type="button" className="btn px-2 py-0.5 text-xs" onClick={() => deleteAnnotation(annotation.id)} aria-label={`删除批注 ${annotation.id}`}>
                  <Trash2 size={12} aria-hidden="true" />
                </button>
              </div>
            ))}
          </div>
        </div>
      )}

      <Dialog
        open={previewRevision !== null}
        onClose={() => { if (!restoring) setPreviewRevision(null); }}
        title={previewRevision ? `修订 #${previewRevision.id} 快照` : "修订快照"}
      >
        {previewRevision && (
          <div className="space-y-3">
            <p className="text-xs text-gray-400">{formatDateTime(previewRevision.ts)} · {previewRevision.source}</p>
            <pre className="max-h-[55dvh] overflow-auto whitespace-pre-wrap rounded border border-side-border bg-gray-950/60 p-3 text-xs leading-relaxed text-gray-200">{previewRevision.content || "(空文档)"}</pre>
            <div className="flex justify-end">
              <button type="button" className="btn text-xs" onClick={restore} disabled={restoring}>
                <RotateCcw size={12} aria-hidden="true" /> {restoring ? "回滚中…" : "回滚到此版本"}
              </button>
            </div>
          </div>
        )}
      </Dialog>
    </div>
  );
}

function SourceTag({ source }: { source: string }) {
  const map: Record<string, { label: string; cls: string; title: string }> = {
    manual_edit: { label: "复核版", cls: "bg-amber-700/40 text-amber-100 border-amber-600/60", title: "人工编辑覆盖层" },
    web_edit: { label: "复核版", cls: "bg-amber-700/40 text-amber-100 border-amber-600/60", title: "网页编辑版本" },
    parsed: { label: "解析原文", cls: "bg-gray-700/40 text-gray-200 border-gray-600", title: "MinerU 解析的 full.md" },
    auto: { label: "自动生成", cls: "bg-blue-800/40 text-blue-100 border-blue-700/60", title: "流水线自动产出" },
  };
  const item = map[source] || { label: source, cls: "bg-gray-700/40 text-gray-300 border-gray-600", title: source };
  return <span className={`rounded border px-1.5 py-0.5 text-xs ${item.cls}`} title={item.title}>{item.label}</span>;
}
