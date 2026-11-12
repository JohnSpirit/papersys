import { useCallback, useEffect, useState } from "react";
import { RefreshCw } from "lucide-react";
import { api, errMsg, type LLMUsageBucket, type LLMUsageResp, type Settings } from "@/lib/api";
import { ErrorState, LoadingState } from "@/components/Feedback";

// 托管检索解析链（backend/config.py retrieval_api_key）：BP 专用密钥 -> FIREWORKS -> ARK。
// 链上密钥未设置时走备用来源，不显示为缺失；可用性以后端解析链结果为准。
const RETRIEVAL_CHAIN_KEYS = new Set(["bp_embed_api_key", "bp_rerank_api_key", "fireworks_api_key", "ark_api_key"]);
const RETRIEVAL_DERIVED_KEYS = new Set(["retrieval_embed_available", "retrieval_rerank_available"]);

export function SettingsPage() {
  const [data, setData] = useState<Settings | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setData(await api.settings());
    } catch (reason: unknown) {
      setError(errMsg(reason));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    queueMicrotask(() => { void load(); });
  }, [load]);

  if (loading && !data) return <div className="p-4 sm:p-6"><LoadingState label="正在读取设置…" /></div>;
  if (error && !data) return <div className="p-4 sm:p-6"><ErrorState message={error} onRetry={load} /></div>;
  if (!data) return null;

  return (
    <div className="max-w-4xl space-y-5 p-4 sm:p-6">
      <div className="flex items-center justify-between gap-3">
        <h1 className="text-2xl font-semibold">设置</h1>
        <button type="button" className="btn px-2" onClick={load} disabled={loading} aria-label="刷新设置" title="刷新设置"><RefreshCw size={14} className={loading ? "animate-spin" : ""} /></button>
      </div>
      {error && <ErrorState message={error} onRetry={load} />}
      <Section title="路径">
        {Object.entries(data.paths).map(([key, value]) => <Row key={key} label={key} value={value} mono />)}
      </Section>
      <Section title="服务">
        <Row label="host" value={data.server.host} mono />
        <Row label="port" value={String(data.server.port)} />
        <Row label="debug" value={String(data.server.debug)} />
        {data.server.cors_origins && <Row label="cors_origins" value={data.server.cors_origins.join(", ")} mono />}
      </Section>
      <Section title="Worker 并发">
        {Object.entries(data.workers).map(([key, value]) => <Row key={key} label={key} value={String(value)} />)}
      </Section>
      <Section title="外部服务限频">
        {Object.entries(data.external).map(([key, value]) => <Row key={key} label={key} value={String(value)} />)}
      </Section>
      <Section title="MinerU">
        {Object.entries(data.mineru).map(([key, value]) => <Row key={key} label={key} value={value} mono />)}
      </Section>
      <Section title="翻译与总结模型">
        <Row label="api_url" value={data.llm.api_url} mono />
        <Row label="model" value={data.llm.model} />
        <Row label="temperature" value={String(data.llm.temperature)} />
        {data.llm.max_input_chars != null && <Row label="max_input_chars" value={String(data.llm.max_input_chars)} />}
        <LLMUsageBlock />
      </Section>
      <Section title="托管检索">
        {Object.entries(data.retrieval).map(([key, value]) => <Row key={key} label={key} value={fmt(value)} mono={typeof value === "string"} />)}
      </Section>
      <Section title="API Token 状态">
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <span className="text-gray-400">托管检索最终密钥</span>
          <span className={data.secrets.present.retrieval_embed_available ? "badge-green" : "badge-red"}>嵌入{data.secrets.present.retrieval_embed_available ? "可用" : "不可用"}</span>
          <span className={data.secrets.present.retrieval_rerank_available ? "badge-green" : "badge-red"}>精排{data.secrets.present.retrieval_rerank_available ? "可用" : "不可用"}</span>
        </div>
        <p className="text-xs text-gray-400">
          解析顺序：BP 专用密钥 → FIREWORKS_API_KEY → ARK_API_KEY。下表中检索链密钥显示灰色“未设置”时由备用来源提供，不表示功能缺失；四个来源都为空时对应能力不可用。
        </p>
        <div className="overflow-x-auto">
          <table className="w-full min-w-[560px] text-sm">
            <thead className="text-left text-gray-400"><tr><th className="px-2 py-1">Token</th><th className="px-2 py-1">状态</th></tr></thead>
            <tbody>
              {Object.entries(data.secrets.present).filter(([key]) => !RETRIEVAL_DERIVED_KEYS.has(key)).map(([key, present]) => (
                <tr key={key} className="border-t border-side-border">
                  <td className="px-2 py-1 font-mono text-xs">{key}</td>
                  <td className="px-2 py-1">
                    {present
                      ? <span className="badge-green">已配置</span>
                      : RETRIEVAL_CHAIN_KEYS.has(key)
                        ? <span className="badge-gray">未设置</span>
                        : <span className="badge-red">未配置</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Section>
    </div>
  );
}

function LLMUsageBlock() {
  const [usage, setUsage] = useState<LLMUsageResp | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    let active = true;
    api.llmUsage().then((value) => {
      if (active) {
        setUsage(value);
        setError(null);
      }
    }).catch((reason: unknown) => {
      if (active) setError(errMsg(reason));
    });
    return () => { active = false; };
  }, [reloadKey]);

  if (error) return <div className="pt-2"><ErrorState message={`用量加载失败：${error}`} onRetry={() => setReloadKey((value) => value + 1)} /></div>;
  if (!usage) return <p className="pt-2 text-xs text-gray-400" role="status">用量加载中…</p>;

  const rows: [string, LLMUsageBucket][] = [
    ["全部累计", usage.totals],
    ["近 30 天", usage.recent30d],
    ...usage.by_kind.map((item): [string, LLMUsageBucket] => [`按类型：${item.kind}`, item]),
  ];
  return (
    <div className="space-y-2 pt-2">
      <div className="overflow-x-auto">
        <table className="w-full min-w-[680px] text-xs">
          <thead className="text-left text-gray-400"><tr><th className="px-1 py-1">范围</th><th className="px-1 py-1">调用</th><th className="px-1 py-1">输入 tokens</th><th className="px-1 py-1">其中缓存</th><th className="px-1 py-1">输出 tokens</th><th className="px-1 py-1">失败</th><th className="px-1 py-1">费用（¥）</th></tr></thead>
          <tbody>
            {rows.map(([label, bucket]) => (
              <tr key={label} className="border-t border-side-border text-gray-300">
                <td className="px-1 py-1">{label}</td>
                <td className="px-1 py-1">{bucket.calls.toLocaleString()}</td>
                <td className="px-1 py-1">{bucket.prompt_tokens.toLocaleString()}</td>
                <td className="px-1 py-1 text-sky-300">{bucket.cached_tokens.toLocaleString()}</td>
                <td className="px-1 py-1">{bucket.completion_tokens.toLocaleString()}</td>
                <td className="px-1 py-1">{bucket.errors > 0 ? <span className="text-red-300">{bucket.errors}</span> : "0"}</td>
                <td className="px-1 py-1">{bucket.cost_total.toFixed(4)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="text-xs text-gray-400">单价（¥/百万 tokens）：输入 {usage.prices.input_per_mtok} / 缓存输入 {usage.prices.cached_input_per_mtok} / 输出 {usage.prices.output_per_mtok}</p>
      {usage.by_model.length > 0 && <p className="text-xs text-gray-400">模型：{usage.by_model.map((item) => `${item.model} × ${item.calls}`).join("，")}</p>}
    </div>
  );
}

function fmt(value: unknown): string {
  return Array.isArray(value) ? value.join(", ") : String(value);
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return <section className="card space-y-2"><h2 className="text-sm font-medium text-gray-200">{title}</h2><div className="space-y-1">{children}</div></section>;
}

function Row({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="flex flex-col gap-1 text-sm sm:flex-row sm:items-start sm:gap-3">
      <span className="shrink-0 text-gray-400 sm:w-40">{label}</span>
      <span className={`min-w-0 break-all text-gray-200 ${mono ? "font-mono text-xs" : ""}`}>{value}</span>
    </div>
  );
}
