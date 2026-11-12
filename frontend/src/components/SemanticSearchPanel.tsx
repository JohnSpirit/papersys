import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { ExternalLink, Search } from "lucide-react";
import { api, errMsg, type SemanticSearchResp } from "@/lib/api";
import { ErrorState, EmptyState, LoadingState } from "@/components/Feedback";
import { FormField } from "@/components/FormField";

export function SemanticSearchPanel() {
  const [query, setQuery] = useState("");
  const [topk, setTopk] = useState(5);
  const [response, setResponse] = useState<SemanticSearchResp | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [searched, setSearched] = useState(false);
  const controllerRef = useRef<AbortController | null>(null);
  const requestRef = useRef(0);

  useEffect(() => () => controllerRef.current?.abort(), []);

  async function runSearch() {
    const trimmed = query.trim();
    if (!trimmed) return;
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    const requestId = ++requestRef.current;
    setLoading(true);
    setError(null);
    setSearched(true);
    try {
      const result = await api.semanticSearch(trimmed, topk, controller.signal);
      if (requestId !== requestRef.current) return;
      if (result.error) {
        setError(result.error);
        setResponse(null);
      } else {
        setResponse(result);
      }
    } catch (reason: unknown) {
      if (requestId === requestRef.current && !controller.signal.aborted) {
        setError(errMsg(reason));
        setResponse(null);
      }
    } finally {
      if (requestId === requestRef.current) setLoading(false);
    }
  }

  const stageLabel = response?.stage === "rerank" ? "向量召回后精排" : response?.stage === "embed_fallback" ? "精排不可用，已使用向量结果" : "向量检索";

  return (
    <div className="space-y-4">
      <form className="card flex flex-wrap items-end gap-3" onSubmit={(event) => { event.preventDefault(); void runSearch(); }}>
        <FormField label="用一句话描述要找的内容" className="min-w-[240px] flex-1">
          <div className="flex items-center gap-2">
            <Search size={15} className="shrink-0 text-gray-400" aria-hidden="true" />
            <input
              className="input min-w-0 flex-1"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="例如：决策过程中神经活动如何发生状态转换"
            />
          </div>
        </FormField>
        <FormField label="结果数量">
          <input className="input w-24" type="number" min={1} max={50} value={topk} onChange={(event) => setTopk(Math.max(1, Math.min(50, Number(event.target.value) || 5)))} />
        </FormField>
        <button type="submit" className="btn-primary" disabled={loading || !query.trim()}>
          <Search size={14} aria-hidden="true" /> {loading ? "检索中…" : "语义检索"}
        </button>
      </form>

      {error && <ErrorState message={error} onRetry={runSearch} />}
      {loading && <LoadingState label="正在检索论文片段…" />}
      {!loading && response?.rerank_error && (
        <p className="rounded border border-amber-700 bg-amber-900/20 px-3 py-2 text-xs text-amber-100" role="status">
          精排服务不可用，当前显示向量召回结果：{response.rerank_error}
        </p>
      )}
      {!loading && response?.truncated && <p className="text-xs text-amber-300" role="status">向量库较大，本次只扫描了配置允许的部分片段。</p>}
      {!loading && response && (
        <section className="space-y-3" aria-label="语义检索结果">
          <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-gray-400">
            <span>找到 {response.count} 个片段 · {stageLabel}</span>
            {response.threshold !== undefined && <span>精排阈值 {response.threshold}</span>}
          </div>
          {response.items.map((item) => (
            <article key={`${item.paper_id}:${item.chunk_index}`} className="card space-y-3">
              <div className="flex flex-wrap items-start justify-between gap-3">
                <div className="min-w-0 flex-1">
                  <h2 className="text-base font-medium leading-snug text-gray-100">{item.title || `论文 #${item.paper_id}`}</h2>
                  <p className="mt-1 text-xs text-gray-400">
                    {[item.first_author, item.year, item.journal].filter(Boolean).join(" · ")} · 片段 {item.chunk_index} · 相关度 {(item.score * 100).toFixed(1)}%
                    {item.low_confidence && <span className="badge-amber ml-2">低置信度</span>}
                  </p>
                </div>
                <Link to={`/paper/${item.paper_id}?tab=en`} className="btn shrink-0 text-xs">
                  <ExternalLink size={12} aria-hidden="true" /> 阅读原文
                </Link>
              </div>
              <p className="whitespace-pre-wrap text-sm leading-relaxed text-gray-300">{item.text}</p>
            </article>
          ))}
        </section>
      )}
      {!loading && searched && !error && response?.items.length === 0 && (
        <EmptyState title="没有找到相关片段" detail="可以减少限定词，或先检查论文是否已完成向量化。" />
      )}
      {!searched && <EmptyState title="按含义查找文库内容" detail="语义检索不要求查询词与正文逐字相同，但仍以已生成的向量为范围。" />}
    </div>
  );
}
