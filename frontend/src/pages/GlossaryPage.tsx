import { useCallback, useEffect, useRef, useState } from "react";
import { Plus, RefreshCw, Save, Trash2 } from "lucide-react";
import { api, errMsg, type GlossaryEntry } from "@/lib/api";
import { useToast } from "@/components/toast-context";
import { formatDateTime } from "@/lib/datetime";
import { Dialog } from "@/components/Dialog";
import { FormField } from "@/components/FormField";
import { EmptyState, ErrorState, LoadingState } from "@/components/Feedback";

const EMPTY: Partial<GlossaryEntry> = { en: "", zh: "", context: "", notes: "" };

export function GlossaryPage() {
  const [items, setItems] = useState<GlossaryEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState<Partial<GlossaryEntry> | null>(null);
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState<number | null>(null);
  const originalRef = useRef("");
  const savingRef = useRef(false);
  const { toast } = useToast();

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await api.glossaryList();
      setItems(response.items);
    } catch (reason: unknown) {
      setError(errMsg(reason));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    queueMicrotask(() => { void load(); });
  }, [load]);

  function openEditor(entry?: GlossaryEntry) {
    const value = entry ? { ...entry } : { ...EMPTY };
    originalRef.current = JSON.stringify(value);
    setEditing(value);
  }

  function closeEditor() {
    if (editing && JSON.stringify(editing) !== originalRef.current) {
      if (!window.confirm("词条尚未保存，确定放弃修改吗？")) return;
    }
    setEditing(null);
  }

  async function save() {
    if (!editing || savingRef.current) return;
    if (!editing.en?.trim() || !editing.zh?.trim()) {
      toast("英文和中文不能为空", "error");
      return;
    }
    savingRef.current = true;
    setSaving(true);
    try {
      if (editing.id) await api.glossaryUpdate(editing.id, editing);
      else await api.glossaryCreate(editing);
      setEditing(null);
      await load();
      toast("词条已保存", "success");
    } catch (reason: unknown) {
      toast(`保存失败：${errMsg(reason)}`, "error");
    } finally {
      savingRef.current = false;
      setSaving(false);
    }
  }

  async function remove(id: number) {
    if (deleting !== null || !window.confirm("确定删除这个词条吗？")) return;
    setDeleting(id);
    try {
      await api.glossaryDelete(id);
      await load();
      toast("词条已删除", "success");
    } catch (reason: unknown) {
      toast(`删除失败：${errMsg(reason)}`, "error");
    } finally {
      setDeleting(null);
    }
  }

  return (
    <div className="space-y-4 p-4 sm:p-6">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold">热词库与翻译术语表</h1>
          <p className="text-sm text-gray-400">共 {items.length} 条 · 翻译时自动加入提示词</p>
        </div>
        <div className="flex gap-2">
          <button type="button" className="btn-primary" onClick={() => openEditor()}><Plus size={14} aria-hidden="true" /> 新增</button>
          <button type="button" className="btn px-2" onClick={load} disabled={loading} aria-label="刷新热词库" title="刷新热词库">
            <RefreshCw size={14} className={loading ? "animate-spin" : ""} aria-hidden="true" />
          </button>
        </div>
      </div>

      {error && <ErrorState message={error} onRetry={load} />}
      {loading && items.length === 0 ? <LoadingState label="正在读取热词库…" /> : items.length === 0 ? (
        <EmptyState title="热词库为空" detail="点击新增，添加需要在翻译中保持一致的术语。" />
      ) : (
        <div className="card overflow-x-auto p-0" aria-busy={loading}>
          <table className="w-full min-w-[850px] text-sm">
            <thead className="bg-gray-900/60 text-left text-gray-300">
              <tr><th className="px-3 py-2">英文</th><th className="px-3 py-2">中文</th><th className="px-3 py-2">语境</th><th className="px-3 py-2">备注</th><th className="px-3 py-2">更新时间</th><th className="px-3 py-2">操作</th></tr>
            </thead>
            <tbody>
              {items.map((entry) => (
                <tr key={entry.id} className="border-t border-side-border hover:bg-gray-800/40">
                  <td className="px-3 py-2 font-mono text-gray-100">{entry.en}</td>
                  <td className="px-3 py-2 text-gray-100">{entry.zh}</td>
                  <td className="px-3 py-2 text-xs text-gray-400">{entry.context || "—"}</td>
                  <td className="px-3 py-2 text-xs text-gray-400">{entry.notes || "—"}</td>
                  <td className="px-3 py-2 text-xs text-gray-400">{formatDateTime(entry.updated_at)}</td>
                  <td className="px-3 py-2">
                    <div className="flex gap-1">
                      <button type="button" className="btn text-xs" onClick={() => openEditor(entry)}>编辑</button>
                      <button type="button" className="btn px-2 text-xs text-red-300" onClick={() => remove(entry.id)} disabled={deleting === entry.id} aria-label={`删除 ${entry.en}`}><Trash2 size={12} /></button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <Dialog open={editing !== null} onClose={closeEditor} title={editing?.id ? "编辑词条" : "新增词条"}>
        {editing && (
          <form className="space-y-3" onSubmit={(event) => { event.preventDefault(); void save(); }}>
            <FormField label="英文">
              <input className="input w-full" value={editing.en ?? ""} onChange={(event) => setEditing({ ...editing, en: event.target.value })} autoFocus />
            </FormField>
            <FormField label="中文">
              <input className="input w-full" value={editing.zh ?? ""} onChange={(event) => setEditing({ ...editing, zh: event.target.value })} />
            </FormField>
            <FormField label="语境（可选，例如 spike、action potential）">
              <input className="input w-full" value={editing.context ?? ""} onChange={(event) => setEditing({ ...editing, context: event.target.value })} />
            </FormField>
            <FormField label="备注（可选）">
              <textarea className="input min-h-20 w-full" value={editing.notes ?? ""} onChange={(event) => setEditing({ ...editing, notes: event.target.value })} />
            </FormField>
            <div className="flex justify-end gap-2 pt-2">
              <button type="button" className="btn" onClick={closeEditor}>取消</button>
              <button type="submit" className="btn-primary" disabled={saving}><Save size={14} aria-hidden="true" /> {saving ? "保存中…" : "保存"}</button>
            </div>
          </form>
        )}
      </Dialog>
    </div>
  );
}
