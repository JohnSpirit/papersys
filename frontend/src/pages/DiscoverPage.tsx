import { useEffect, useRef, useState } from "react";
import { Download, ExternalLink, FileText, RefreshCw, Search } from "lucide-react";
import { api, errMsg, type SearchResult } from "@/lib/api";
import { useToast } from "@/components/toast-context";
import { FormField } from "@/components/FormField";
import { EmptyState, ErrorState, LoadingState } from "@/components/Feedback";

const SOURCES = [
  { key: "arxiv", label: "arXiv", hint: "免费预印本，覆盖广" },
  { key: "openalex", label: "OpenAlex", hint: "元数据与引用计数" },
  { key: "semantic_scholar", label: "Semantic Scholar", hint: "推荐与引用关系" },
  { key: "sciverse", label: "Sciverse", hint: "语义片段检索" },
];

export function DiscoverPage() {
  const [source, setSource] = useState("arxiv");
  const [query, setQuery] = useState("");
  const [limit, setLimit] = useState(20);
  const [yearFrom, setYearFrom] = useState("");
  const [yearTo, setYearTo] = useState("");
  const [items, setItems] = useState<SearchResult[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [searched, setSearched] = useState(false);
  const [ingestStatus, setIngestStatus] = useState<Record<string, string>>({});
  const [ingestErrors, setIngestErrors] = useState<Record<string, string>>({});
  const controllerRef = useRef<AbortController | null>(null);
  const searchIdRef = useRef(0);
  const ingestRef = useRef(new Set<string>());
  const { toast } = useToast();

  useEffect(() => () => controllerRef.current?.abort(), []);

  async function doSearch() {
    const trimmed = query.trim();
    if (!trimmed) return;
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    const searchId = ++searchIdRef.current;
    setLoading(true);
    setError(null);
    setSearched(true);
    setIngestStatus({});
    setIngestErrors({});
    try {
      const response = await api.search({
        source,
        query: trimmed,
        limit,
        year_from: yearFrom ? Number(yearFrom) : undefined,
        year_to: yearTo ? Number(yearTo) : undefined,
      }, controller.signal);
      if (searchId !== searchIdRef.current) return;
      setItems(response.items);
      if (response.error) setError(response.error);
    } catch (reason: unknown) {
      if (searchId === searchIdRef.current && !controller.signal.aborted) {
        setError(errMsg(reason));
        setItems([]);
      }
    } finally {
      if (searchId === searchIdRef.current) setLoading(false);
    }
  }

  function changeSource(next: string) {
    controllerRef.current?.abort();
    searchIdRef.current += 1;
    setSource(next);
    setItems([]);
    setError(null);
    setSearched(false);
    setLoading(false);
    setIngestStatus({});
    setIngestErrors({});
  }

  async function ingest(item: SearchResult) {
    if (!item.pdf_url) return;
    const key = item.id || item.url || item.title;
    if (ingestRef.current.has(key)) return;
    ingestRef.current.add(key);
    setIngestStatus((current) => ({ ...current, [key]: "下载中…" }));
    setIngestErrors((current) => ({ ...current, [key]: "" }));
    try {
      const filename = item.arxiv_id ? `${item.arxiv_id}.pdf` : item.doi ? `${item.doi.replace(/[^\w.-]/g, "_")}.pdf` : "";
      const response = await api.inboxFromUrl(item.pdf_url, filename);
      setIngestStatus((current) => ({ ...current, [key]: `任务 #${response.task_id}` }));
      toast(`已创建任务 #${response.task_id}`, "success");
    } catch (reason: unknown) {
      setIngestStatus((current) => ({ ...current, [key]: "失败" }));
      setIngestErrors((current) => ({ ...current, [key]: errMsg(reason) }));
    } finally {
      ingestRef.current.delete(key);
    }
  }

  return (
    <div className="space-y-4 p-4 sm:p-6">
      <div>
        <h1 className="text-2xl font-semibold">文献发现</h1>
        <p className="text-sm text-gray-400">在外部数据源中检索，并把有 PDF 的论文加入投递目录。</p>
      </div>

      <div className="flex gap-1 overflow-x-auto border-b border-side-border" role="tablist" aria-label="外部数据源">
        {SOURCES.map((item) => (
          <button
            type="button"
            key={item.key}
            role="tab"
            aria-selected={source === item.key}
            onClick={() => changeSource(item.key)}
            className={`shrink-0 px-4 py-2 text-sm ${source === item.key ? "border-b-2 border-blue-500 text-blue-100" : "text-gray-400 hover:text-gray-200"}`}
            title={item.hint}
          >
            {item.label}
          </button>
        ))}
      </div>

      <form onSubmit={(event) => { event.preventDefault(); void doSearch(); }} className="card flex flex-wrap items-end gap-3">
        <FormField label="关键字" className="min-w-[240px] flex-1">
          <div className="flex items-center gap-2">
            <Search size={14} className="shrink-0 text-gray-400" aria-hidden="true" />
            <input className="input min-w-0 flex-1" value={query} onChange={(event) => setQuery(event.target.value)} placeholder={`在 ${SOURCES.find((item) => item.key === source)?.label} 中搜索`} />
          </div>
        </FormField>
        <FormField label="数量">
          <input className="input w-20" type="number" min={1} max={100} value={limit} onChange={(event) => setLimit(Math.max(1, Math.min(100, Number(event.target.value) || 20)))} />
        </FormField>
        <FormField label="年份" group>
          <div className="flex items-center gap-1">
            <input className="input w-24" type="number" aria-label="起始年份" placeholder="起始" value={yearFrom} onChange={(event) => setYearFrom(event.target.value)} />
            <span className="text-gray-400">–</span>
            <input className="input w-24" type="number" aria-label="结束年份" placeholder="结束" value={yearTo} onChange={(event) => setYearTo(event.target.value)} />
          </div>
        </FormField>
        <button type="submit" className="btn-primary" disabled={loading || !query.trim()}>
          {loading ? <RefreshCw size={14} className="animate-spin" /> : <Search size={14} />} 搜索
        </button>
      </form>

      {error && <ErrorState message={error} onRetry={doSearch} />}
      {loading && <LoadingState label="正在搜索外部数据源…" />}
      {!loading && items.length === 0 && (
        <EmptyState title={searched ? "没有检索结果" : "输入关键字开始检索"} detail={searched ? "可以更换数据源、减少年份限制或调整关键字。" : "当前只检索外部数据源，库内语义检索位于文献库页面。"} />
      )}

      <div className="space-y-2" aria-busy={loading}>
        {items.map((item, index) => {
          const key = item.id || item.url || item.title || String(index);
          const status = ingestStatus[key];
          const authors = item.authors || [];
          return (
            <article key={key} className="card transition-colors hover:bg-gray-900/60">
              <div className="flex flex-col gap-4 sm:flex-row sm:justify-between">
                <div className="min-w-0 flex-1">
                  <h2 className="text-base font-medium leading-snug text-gray-100">{item.title || "(无标题)"}</h2>
                  <div className="mt-1 text-xs text-gray-400">
                    {item.year && <span>{item.year}</span>}
                    {item.journal && <> · <span>{item.journal}</span></>}
                    {item.cited_by_count != null && <> · 引用 {item.cited_by_count}</>}
                    {item.score != null && <> · 相关度 {(item.score * 100).toFixed(0)}%</>}
                  </div>
                  {authors.length > 0 && <p className="mt-1 truncate text-xs text-gray-400">{authors.slice(0, 5).join(", ")}{authors.length > 5 ? ` …(+${authors.length - 5})` : ""}</p>}
                  {(item.tldr || item.snippet || item.abstract) && <p className="mt-2 line-clamp-3 text-sm text-gray-300">{item.tldr || item.snippet || item.abstract}</p>}
                  <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1 break-all text-xs text-gray-400">
                    {item.doi && <span>doi: <span className="font-mono">{item.doi}</span></span>}
                    {item.arxiv_id && <span>arxiv: <span className="font-mono">{item.arxiv_id}</span></span>}
                  </div>
                </div>
                <div className="flex shrink-0 flex-col items-stretch gap-1.5 sm:items-end">
                  {item.url && <a className="btn text-xs" href={item.url} target="_blank" rel="noreferrer"><ExternalLink size={12} /> 打开</a>}
                  {item.pdf_url && (
                    <button type="button" className="btn-primary text-xs" onClick={() => ingest(item)} disabled={!!status && status !== "失败"}>
                      <Download size={12} /> {status || "加入投递目录"}
                    </button>
                  )}
                  {!item.pdf_url && <span className="inline-flex items-center gap-1 text-xs text-gray-400"><FileText size={11} /> 无 PDF</span>}
                  {ingestErrors[key] && <p className="max-w-xs break-words text-xs text-red-300" role="alert">{ingestErrors[key]}</p>}
                </div>
              </div>
            </article>
          );
        })}
      </div>
    </div>
  );
}
