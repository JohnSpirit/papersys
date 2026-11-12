import { useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { ExternalLink, RefreshCw, Search } from "lucide-react";
import { api, errMsg, type Paper, type PapersResp } from "@/lib/api";
import { PaperStatusRow } from "@/components/StatusBadge";
import { PaperDrawer } from "@/components/PaperDrawer";
import { SemanticSearchPanel } from "@/components/SemanticSearchPanel";
import { FormField } from "@/components/FormField";
import { Pagination } from "@/components/Pagination";
import { EmptyState, ErrorState, LoadingState } from "@/components/Feedback";

const PAGE_SIZE = 50;
const STATUS_OPTIONS = [
  { value: "", label: "全部" },
  { value: "missing", label: "未执行" },
  { value: "processing", label: "处理中" },
  { value: "ok", label: "完成" },
  { value: "partial", label: "部分完成" },
  { value: "error", label: "失败" },
  { value: "manual_hold", label: "待人工处理" },
  { value: "skipped", label: "已跳过" },
];
const ORDER_OPTIONS = [
  { value: "year_desc", label: "年份从新到旧" },
  { value: "year_asc", label: "年份从旧到新" },
  { value: "title_asc", label: "标题字母顺序" },
  { value: "updated", label: "最近更新" },
];

interface Filters {
  q: string;
  author: string;
  fulltext: string;
  yearFrom: string;
  yearTo: string;
  statusPdf: string;
  statusParsed: string;
  statusTranslated: string;
  statusEnrich: string;
  statusSummary: string;
  statusEmbedded: string;
  orderBy: string;
}

const INITIAL_FILTERS: Filters = {
  q: "", author: "", fulltext: "", yearFrom: "", yearTo: "",
  statusPdf: "", statusParsed: "", statusTranslated: "", statusEnrich: "", statusSummary: "", statusEmbedded: "",
  orderBy: "year_desc",
};

export function LibraryPage() {
  const [mode, setMode] = useState<"filters" | "semantic">("filters");
  const [filters, setFilters] = useState<Filters>(INITIAL_FILTERS);
  const [response, setResponse] = useState<PapersResp | null>(null);
  const [selected, setSelected] = useState<Paper | null>(null);
  const [page, setPage] = useState(0);
  const [reloadKey, setReloadKey] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const requestRef = useRef(0);

  const params = useMemo(() => ({
    q: filters.q,
    author: filters.author,
    fulltext: filters.fulltext,
    year_from: filters.yearFrom,
    year_to: filters.yearTo,
    status_pdf: filters.statusPdf,
    status_parsed: filters.statusParsed,
    status_translated: filters.statusTranslated,
    status_enrich: filters.statusEnrich,
    status_summary: filters.statusSummary,
    status_embedded: filters.statusEmbedded,
    order_by: filters.orderBy,
    limit: PAGE_SIZE,
    offset: page * PAGE_SIZE,
  }), [filters, page]);

  useEffect(() => {
    const controller = new AbortController();
    const requestId = ++requestRef.current;
    const timer = window.setTimeout(() => {
      setLoading(true);
      setError(null);
      api.papers(params, controller.signal).then((data) => {
        if (requestId === requestRef.current) setResponse(data);
      }).catch((reason: unknown) => {
        if (requestId === requestRef.current && !controller.signal.aborted) setError(errMsg(reason));
      }).finally(() => {
        if (requestId === requestRef.current) setLoading(false);
      });
    }, 250);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [params, reloadKey]);

  function updateFilter<K extends keyof Filters>(key: K, value: Filters[K]) {
    setFilters((current) => ({ ...current, [key]: value }));
    setPage(0);
  }

  function clearFilters() {
    setFilters(INITIAL_FILTERS);
    setPage(0);
  }

  return (
    <div className="space-y-4 p-4 sm:p-6">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold">文献库</h1>
          <p className="text-sm text-gray-400">
            {mode === "semantic" ? "按含义查找已向量化的论文正文" : response ? `共 ${response.count} 篇` : "正在读取…"}
          </p>
        </div>
        {mode === "filters" && (
          <button type="button" className="btn" onClick={() => setReloadKey((value) => value + 1)} disabled={loading}>
            <RefreshCw size={14} className={loading ? "animate-spin" : ""} aria-hidden="true" /> 刷新
          </button>
        )}
      </div>

      <div className="flex gap-1 border-b border-side-border" role="tablist" aria-label="文献查询模式">
        <ModeButton active={mode === "filters"} onClick={() => setMode("filters")}>筛选与全文</ModeButton>
        <ModeButton active={mode === "semantic"} onClick={() => setMode("semantic")}>语义检索</ModeButton>
      </div>

      {mode === "semantic" ? <SemanticSearchPanel /> : (
        <>
          <section className="card flex flex-wrap items-end gap-3" aria-label="文献筛选">
            <FormField label="关键字" className="w-full sm:w-auto">
              <div className="flex items-center gap-1">
                <Search size={14} className="text-gray-400" aria-hidden="true" />
                <input className="input w-full sm:w-48" value={filters.q} onChange={(event) => updateFilter("q", event.target.value)} placeholder="标题、目录或关键词" />
              </div>
            </FormField>
            <FormField label="作者" className="w-full sm:w-auto">
              <input className="input w-full sm:w-36" value={filters.author} onChange={(event) => updateFilter("author", event.target.value)} placeholder="例如 shadlen" />
            </FormField>
            <FormField label="全文" className="w-full sm:w-auto">
              <input className="input w-full sm:w-48" value={filters.fulltext} onChange={(event) => updateFilter("fulltext", event.target.value)} placeholder="正文、翻译或总结" />
            </FormField>
            <FormField label="年份" group>
              <div className="flex items-center gap-1">
                <input className="input w-24" type="number" aria-label="起始年份" value={filters.yearFrom} onChange={(event) => updateFilter("yearFrom", event.target.value)} placeholder="起始" />
                <span className="text-gray-400">–</span>
                <input className="input w-24" type="number" aria-label="结束年份" value={filters.yearTo} onChange={(event) => updateFilter("yearTo", event.target.value)} placeholder="结束" />
              </div>
            </FormField>
            <FormField label="排序">
              <Select value={filters.orderBy} onChange={(value) => updateFilter("orderBy", value)} options={ORDER_OPTIONS} label="排序" />
            </FormField>
            <button type="button" className="btn" onClick={clearFilters}>清空</button>
          </section>

          <details className="card py-2">
            <summary className="cursor-pointer text-sm text-gray-300">按流水线状态筛选</summary>
            <div className="mt-3 flex flex-wrap gap-3">
              <FormField label="PDF"><Select value={filters.statusPdf} onChange={(value) => updateFilter("statusPdf", value)} options={STATUS_OPTIONS} label="PDF 状态" /></FormField>
              <FormField label="解析"><Select value={filters.statusParsed} onChange={(value) => updateFilter("statusParsed", value)} options={STATUS_OPTIONS} label="解析状态" /></FormField>
              <FormField label="富化"><Select value={filters.statusEnrich} onChange={(value) => updateFilter("statusEnrich", value)} options={STATUS_OPTIONS} label="富化状态" /></FormField>
              <FormField label="翻译"><Select value={filters.statusTranslated} onChange={(value) => updateFilter("statusTranslated", value)} options={STATUS_OPTIONS} label="翻译状态" /></FormField>
              <FormField label="总结"><Select value={filters.statusSummary} onChange={(value) => updateFilter("statusSummary", value)} options={STATUS_OPTIONS} label="总结状态" /></FormField>
              <FormField label="向量"><Select value={filters.statusEmbedded} onChange={(value) => updateFilter("statusEmbedded", value)} options={STATUS_OPTIONS} label="向量状态" /></FormField>
            </div>
          </details>

          {error && <ErrorState message={error} onRetry={() => setReloadKey((value) => value + 1)} />}
          {loading && !response ? <LoadingState label="正在读取文献列表…" /> : response && response.items.length === 0 ? (
            <EmptyState title="没有符合条件的论文" detail="请调整关键字、年份或状态筛选。" />
          ) : response && (
            <>
              <div className="card hidden overflow-x-auto p-0 md:block" aria-busy={loading}>
                <table className="w-full min-w-[900px] table-fixed text-sm">
                  <thead className="sticky top-0 bg-gray-900/90 text-left text-gray-300">
                    <tr>
                      <th className="w-16 px-3 py-2">年份</th>
                      <th className="w-48 px-3 py-2">目录</th>
                      <th className="px-3 py-2">标题</th>
                      <th className="w-28 px-3 py-2">第一作者</th>
                      <th className="w-36 px-3 py-2">期刊</th>
                      <th className="w-16 px-3 py-2 text-right">被引</th>
                      <th className="w-28 px-3 py-2">状态</th>
                    </tr>
                  </thead>
                  <tbody>
                    {response.items.map((paper) => (
                      <tr key={paper.id} className="border-t border-side-border hover:bg-gray-800/40">
                        <td className="px-3 py-2 font-mono text-gray-300">{paper.year}</td>
                        <td className="truncate px-3 py-2 font-mono text-xs text-gray-400" title={paper.dir_name}>{paper.dir_name}</td>
                        <td className="px-3 py-2">
                          <div className="flex items-start gap-2">
                            <button type="button" className="line-clamp-2 min-w-0 flex-1 text-left text-gray-100 hover:text-blue-200" onClick={() => setSelected(paper)} aria-label={`预览 ${paper.title || paper.dir_name}`}>
                              {paper.title || "(无标题)"}
                            </button>
                            <Link to={`/paper/${paper.id}`} className="shrink-0 text-gray-400 hover:text-blue-200" aria-label={`打开 ${paper.title || paper.dir_name} 的详情页`}><ExternalLink size={13} /></Link>
                          </div>
                          {paper.attachment_kind === "si" && <span className="badge-amber mt-1" title="补充材料">SI</span>}
                          {paper.doi && <div className="truncate text-xs text-gray-400" title={paper.doi}>doi:{paper.doi}</div>}
                        </td>
                        <td className="truncate px-3 py-2 text-gray-300" title={paper.first_author}>{paper.first_author}</td>
                        <td className="truncate px-3 py-2 text-gray-400" title={paper.journal || ""}>{paper.journal || "—"}</td>
                        <td className="px-3 py-2 text-right text-gray-400">{paper.cited_by_count ?? "—"}</td>
                        <td className="px-3 py-2"><PaperStatusRow p={paper} /></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>

              <div className="space-y-2 md:hidden">
                {response.items.map((paper) => (
                  <article key={paper.id} className="card space-y-2">
                    <div className="flex items-start justify-between gap-2">
                      <button type="button" className="text-left text-sm font-medium leading-snug text-gray-100" onClick={() => setSelected(paper)}>{paper.title || "(无标题)"}</button>
                      <Link to={`/paper/${paper.id}`} className="shrink-0 text-gray-400" aria-label="打开详情页"><ExternalLink size={14} /></Link>
                    </div>
                    <p className="break-all font-mono text-xs text-gray-400">{paper.dir_name}</p>
                    <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-gray-400">
                      <span>{paper.first_author} · {paper.year}</span><PaperStatusRow p={paper} />
                    </div>
                  </article>
                ))}
              </div>
            </>
          )}
          <Pagination page={page} pageSize={PAGE_SIZE} total={response?.count ?? 0} onChange={setPage} itemLabel="篇" />
        </>
      )}
      <PaperDrawer paper={selected} onClose={() => setSelected(null)} />
    </div>
  );
}

function ModeButton({ active, onClick, children }: { active: boolean; onClick: () => void; children: React.ReactNode }) {
  return (
    <button type="button" role="tab" aria-selected={active} onClick={onClick} className={`border-b-2 px-4 py-2 text-sm ${active ? "border-blue-500 text-blue-100" : "border-transparent text-gray-400 hover:text-gray-200"}`}>
      {children}
    </button>
  );
}

function Select({ value, onChange, options, label }: {
  value: string;
  onChange: (value: string) => void;
  options: { value: string; label: string }[];
  label: string;
}) {
  return (
    <select className="input w-32" value={value} onChange={(event) => onChange(event.target.value)} aria-label={label}>
      {options.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
    </select>
  );
}
