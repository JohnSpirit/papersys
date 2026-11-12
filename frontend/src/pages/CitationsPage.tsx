import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import cytoscape, { type Core, type NodeSingular } from "cytoscape";
import { Focus, RefreshCw, ZoomIn, ZoomOut } from "lucide-react";
import { api, errMsg, type CitationGraph, type CitationNode } from "@/lib/api";
import {
  citationNodeSize,
  createGridPositions,
  type CitationLayoutRequest,
  type CitationLayoutResponse,
  type CitationPositions,
} from "@/lib/citation-layout";
import { useToast } from "@/components/toast-context";
import { FormField } from "@/components/FormField";
import { EmptyState, ErrorState, LoadingState } from "@/components/Feedback";

const DEGREE_LOW_RGB = [59, 130, 246] as const;
const DEGREE_HIGH_RGB = [250, 204, 21] as const;
const DEGREE_GRADIENT = "linear-gradient(to right, #3b82f6, #facc15)";
const COMFORT_STORAGE_KEY = "paper.citations.comfort";
const COMFORT_MIN_DEGREE = 6;

function readComfortPreference(): boolean {
  try {
    return window.localStorage.getItem(COMFORT_STORAGE_KEY) !== "0";
  } catch {
    return true;
  }
}

function saveComfortPreference(comfortable: boolean) {
  try {
    window.localStorage.setItem(COMFORT_STORAGE_KEY, comfortable ? "1" : "0");
  } catch {
    return;
  }
}

function citationDegreeColor(degree: number, maxDegree: number): string {
  if (degree <= 0 || maxDegree <= 0) return "#64748b";
  const ratio = maxDegree === 1 ? 1 : Math.min(1, Math.max(0, Math.log(degree) / Math.log(maxDegree)));
  const channels = DEGREE_LOW_RGB.map((low, index) => Math.round(low + (DEGREE_HIGH_RGB[index] - low) * ratio));
  return `rgb(${channels.join(",")})`;
}

interface CitationLayoutState {
  data: CitationGraph;
  positions: CitationPositions;
}

export function CitationsPage() {
  const containerRef = useRef<HTMLDivElement>(null);
  const cyRef = useRef<Core | null>(null);
  const requestRef = useRef(0);
  const [data, setData] = useState<CitationGraph | null>(null);
  const [layoutResult, setLayoutResult] = useState<CitationLayoutState | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [includeExt, setIncludeExt] = useState(false);
  const [hideIsolated, setHideIsolated] = useState(true);
  const [comfortable, setComfortable] = useState(readComfortPreference);
  const [yearFrom, setYearFrom] = useState("");
  const [yearTo, setYearTo] = useState("");
  const [query, setQuery] = useState("");
  const [activePaper, setActivePaper] = useState<CitationNode | null>(null);
  const [focusId, setFocusId] = useState<string | null>(null);
  const [rebuilding, setRebuilding] = useState(false);
  const [rebuildTask, setRebuildTask] = useState<number | null>(null);
  const { toast } = useToast();

  const load = useCallback(async (signal?: AbortSignal) => {
    const requestId = ++requestRef.current;
    setLoading(true);
    setError(null);
    try {
      const response = await api.citations({ includeExternal: includeExt, yearFrom, yearTo }, signal);
      if (requestId === requestRef.current) setData(response);
    } catch (reason: unknown) {
      if (requestId === requestRef.current && !signal?.aborted) setError(errMsg(reason));
    } finally {
      if (requestId === requestRef.current) setLoading(false);
    }
  }, [includeExt, yearFrom, yearTo]);

  useEffect(() => {
    const controller = new AbortController();
    const delay = requestRef.current === 0 ? 0 : 250;
    const timer = window.setTimeout(() => { void load(controller.signal); }, delay);
    return () => {
      window.clearTimeout(timer);
      controller.abort();
    };
  }, [load]);

  useEffect(() => {
    saveComfortPreference(comfortable);
  }, [comfortable]);

  useEffect(() => {
    if (!data) return;
    let cancelled = false;
    const finish = (positions: CitationPositions) => {
      if (!cancelled) setLayoutResult({ data, positions });
    };
    const showGridFallback = (message?: string) => {
      if (cancelled) return;
      if (message) setError(`节点布局计算失败，已改用网格排列：${message}`);
      finish(createGridPositions(data.nodes));
    };
    const fallback = (message?: string) => {
      const timer = window.setTimeout(() => showGridFallback(message), 0);
      return () => {
        cancelled = true;
        window.clearTimeout(timer);
      };
    };
    if (data.nodes.length === 0) return fallback();
    if (typeof Worker === "undefined") return fallback("当前浏览器不支持后台布局");

    let worker: Worker;
    try {
      worker = new Worker(new URL("../lib/citation-layout.worker.ts", import.meta.url), { type: "module" });
    } catch (reason: unknown) {
      return fallback(errMsg(reason));
    }
    worker.onmessage = (event: MessageEvent<CitationLayoutResponse>) => {
      if (event.data.ok) finish(event.data.positions);
      else showGridFallback(event.data.message);
    };
    worker.onerror = (event: ErrorEvent) => {
      event.preventDefault();
      showGridFallback(event.message);
    };
    const request: CitationLayoutRequest = { nodes: data.nodes, edges: data.edges };
    worker.postMessage(request);
    return () => {
      cancelled = true;
      worker.onmessage = null;
      worker.onerror = null;
      worker.terminate();
    };
  }, [data]);

  const displayData = layoutResult?.data ?? null;
  const layoutPending = data !== null && displayData !== data;
  const busy = loading || layoutPending;
  const degreeById = useMemo(() => {
    const degrees = new Map<string, number>();
    if (!displayData) return degrees;
    displayData.nodes.forEach((node) => degrees.set(node.id, 0));
    displayData.edges.forEach((edge) => {
      degrees.set(edge.source, (degrees.get(edge.source) ?? 0) + 1);
      degrees.set(edge.target, (degrees.get(edge.target) ?? 0) + 1);
    });
    return degrees;
  }, [displayData]);
  const maxDegree = Math.max(0, ...degreeById.values());
  const midDegree = Math.round(Math.sqrt(maxDegree));
  const graph = useMemo(() => {
    if (!displayData) return { nodes: [], edges: [] };
    const connected = new Set(displayData.edges.flatMap((edge) => [edge.source, edge.target]));
    const needle = query.trim().toLowerCase();
    let nodes = displayData.nodes.filter((node) => {
      if (hideIsolated && !connected.has(node.id)) return false;
      if (comfortable && !needle && !focusId && (degreeById.get(node.id) ?? 0) < COMFORT_MIN_DEGREE) return false;
      if (needle && ![node.title, node.label, node.author].some((value) => value?.toLowerCase().includes(needle))) return false;
      return true;
    });
    if (focusId) {
      const neighbors = new Set([focusId]);
      displayData.edges.forEach((edge) => {
        if (edge.source === focusId) neighbors.add(edge.target);
        if (edge.target === focusId) neighbors.add(edge.source);
      });
      nodes = nodes.filter((node) => neighbors.has(node.id));
    }
    const ids = new Set(nodes.map((node) => node.id));
    return {
      nodes,
      edges: displayData.edges.filter((edge) => ids.has(edge.source) && ids.has(edge.target)),
    };
  }, [displayData, query, hideIsolated, focusId, comfortable, degreeById]);
  const hasVisibleNodes = graph.nodes.length > 0;

  useEffect(() => {
    if (!containerRef.current || !layoutResult || !hasVisibleNodes) {
      cyRef.current?.destroy();
      cyRef.current = null;
      return;
    }
    cyRef.current?.destroy();
    const cy = cytoscape({
      container: containerRef.current,
      elements: [
        ...layoutResult.data.nodes.map((node) => ({
          data: { ...node, degree: degreeById.get(node.id) ?? 0 },
          position: layoutResult.positions[node.id] ?? { x: 0, y: 0 },
        })),
        ...layoutResult.data.edges.map((edge) => ({ data: { ...edge } })),
      ],
      style: [
        {
          selector: "node",
          style: {
            label: "",
            "font-size": "10px",
            "text-valign": "center",
            "text-halign": "center",
            "background-color": (node: NodeSingular) => citationDegreeColor(Number(node.data("degree")) || 0, maxDegree),
            color: "#e5e7eb",
            width: (node: NodeSingular) => citationNodeSize(node.data("citations")),
            height: (node: NodeSingular) => citationNodeSize(node.data("citations")),
            "border-width": 0,
          },
        },
        { selector: "node.filtered", style: { display: "none" } },
        { selector: "node[?in_library]", style: { "border-width": 1, "border-color": "#93c5fd" } },
        { selector: "node[!in_library]", style: { "border-width": 1, "border-color": "#d4d4d8", opacity: 0.8 } },
        {
          selector: "node.comfortable",
          style: {
            shape: "round-rectangle",
            width: (node: NodeSingular) => citationNodeSize(node.data("citations")) * 0.8,
            height: (node: NodeSingular) => citationNodeSize(node.data("citations")) * 0.8,
            "border-width": 0,
          },
        },
        { selector: "edge", style: { width: 1, "line-color": "#64748b", "target-arrow-color": "#64748b", "target-arrow-shape": "triangle" } },
        { selector: "edge[?external]", style: { "line-color": "#a1a1aa", "target-arrow-color": "#a1a1aa", opacity: 0.55, "line-style": "dashed" } },
        { selector: "edge.comfortable", style: { width: 0.7, opacity: 0.25 } },
        { selector: "node:selected", style: { label: "data(label)", "border-width": 3, "border-color": "#fbbf24" } },
      ],
      layout: { name: "preset", fit: false },
      minZoom: 0.2,
      maxZoom: 3,
    });
    const updateLabels = () => {
      cy.style().selector("node").style("label", cy.zoom() >= 1.15 ? "data(label)" : "").update();
    };
    updateLabels();
    cy.on("zoom", updateLabels);
    cy.on("tap", "node", (event) => {
      const node = event.target as NodeSingular;
      setActivePaper(node.data() as CitationNode);
    });
    cyRef.current = cy;
    return () => {
      cy.destroy();
      if (cyRef.current === cy) cyRef.current = null;
    };
  }, [layoutResult, hasVisibleNodes, degreeById, maxDegree]);

  useEffect(() => {
    const cy = cyRef.current;
    if (!cy || !hasVisibleNodes) return;
    const visibleIds = new Set(graph.nodes.map((node) => node.id));
    cy.batch(() => {
      cy.nodes().forEach((node) => {
        node.toggleClass("filtered", !visibleIds.has(node.id()));
        node.toggleClass("comfortable", comfortable);
      });
      cy.edges().forEach((edge) => {
        edge.toggleClass("comfortable", comfortable);
      });
    });
    const visibleNodes = cy.nodes().filter((node) => visibleIds.has(node.id()));
    if (visibleNodes.length > 0) cy.fit(visibleNodes, 30);
  }, [graph, hasVisibleNodes, comfortable]);

  async function rebuild() {
    if (rebuilding) return;
    setRebuilding(true);
    try {
      const response = await api.citationsRebuild();
      setRebuildTask(response.task_id);
      toast(`已创建引文重建任务 #${response.task_id}`, "success");
    } catch (reason: unknown) {
      toast(`重建失败：${errMsg(reason)}`, "error");
    } finally {
      setRebuilding(false);
    }
  }

  const active = activePaper && graph.nodes.some((node) => node.id === activePaper.id) ? activePaper : null;

  return (
    <div className="space-y-4 p-4 sm:p-6">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold">引文网络</h1>
          <p className="text-sm text-gray-400">
            当前显示 {graph.nodes.length} 个节点、{graph.edges.length} 条边
            {displayData ? ` · 全部 ${displayData.stats.library_nodes} 个库内节点、${displayData.stats.edges} 条边` : ""}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <button type="button" className="btn text-xs" onClick={rebuild} disabled={rebuilding}>{rebuilding ? "创建中…" : "重建网络"}</button>
          <button type="button" className="btn px-2" onClick={() => load()} disabled={busy} aria-label="刷新引文网络" title="刷新引文网络">
            <RefreshCw size={14} className={busy ? "animate-spin" : ""} aria-hidden="true" />
          </button>
        </div>
      </div>
      {rebuildTask && <p className="text-xs text-gray-400">重建任务：<Link className="text-blue-300 hover:underline" to={`/tasks?task=${rebuildTask}`}>#{rebuildTask}</Link>，完成后可手动刷新网络。</p>}

      <section className="card flex flex-wrap items-end gap-3" aria-label="引文图筛选">
        <FormField label="标题、目录或作者" className="min-w-[220px] flex-1">
          <input className="input w-full" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="在当前结果中筛选" />
        </FormField>
        <FormField label="年份" group>
          <div className="flex items-center gap-1">
            <input className="input w-24" type="number" aria-label="起始年份" placeholder="起始" value={yearFrom} onChange={(event) => setYearFrom(event.target.value)} />
            <span className="text-gray-400">–</span>
            <input className="input w-24" type="number" aria-label="结束年份" placeholder="结束" value={yearTo} onChange={(event) => setYearTo(event.target.value)} />
          </div>
        </FormField>
        <label className="flex min-h-8 items-center gap-2 text-xs text-gray-300"><input type="checkbox" checked={includeExt} onChange={(event) => setIncludeExt(event.target.checked)} /> 含外部引用</label>
        <label className="flex min-h-8 items-center gap-2 text-xs text-gray-300"><input type="checkbox" checked={hideIsolated} onChange={(event) => setHideIsolated(event.target.checked)} /> 隐藏孤立节点</label>
        <label className="flex min-h-8 items-center gap-2 text-xs text-gray-300" title={`默认浏览时显示连接数 ≥ ${COMFORT_MIN_DEGREE} 的节点，使用圆角矩形并降低连线透明度；搜索和邻接聚焦不受限制`}><input type="checkbox" aria-label="舒适视图" checked={comfortable} onChange={(event) => setComfortable(event.target.checked)} /> 舒适视图（≥{COMFORT_MIN_DEGREE}）</label>
        {comfortable && <button type="button" className="btn min-h-8 text-xs" onClick={() => { setComfortable(false); setHideIsolated(false); }}>显示全部节点</button>}
        {focusId && <button type="button" className="btn text-xs" onClick={() => setFocusId(null)}>退出邻接聚焦</button>}
      </section>

      {error && <ErrorState message={error} onRetry={() => load()} />}
      {busy && !layoutResult ? <LoadingState label={loading ? "正在读取引文网络…" : "正在计算节点位置…"} /> : graph.nodes.length === 0 ? (
        <EmptyState
          title="当前筛选下没有可显示的节点"
          detail={comfortable && !query && !focusId ? `舒适视图仅显示连接数 ≥ ${COMFORT_MIN_DEGREE} 的节点。可以搜索具体论文，或点击“显示全部节点”。` : "可以取消隐藏孤立节点，或调整年份和关键字。"}
        />
      ) : (
        <div className="flex flex-col gap-4 lg:flex-row">
          <div className="min-w-0 flex-1">
            <div className="mb-2 flex justify-end gap-1">
              <button type="button" className="btn px-2 text-xs" onClick={() => cyRef.current?.zoom(cyRef.current.zoom() * 1.2)} aria-label="放大引文图"><ZoomIn size={14} /></button>
              <button type="button" className="btn px-2 text-xs" onClick={() => cyRef.current?.zoom(cyRef.current.zoom() / 1.2)} aria-label="缩小引文图"><ZoomOut size={14} /></button>
              <button type="button" className="btn px-2 text-xs" onClick={() => cyRef.current?.fit()}>适应全图</button>
            </div>
            <div className="relative">
              <div ref={containerRef} className="card h-[55dvh] min-h-[420px] min-w-0" aria-busy={busy} aria-label="可缩放引文关系图" />
              <div role="img" aria-label="节点连接数颜色条" className="pointer-events-none absolute bottom-3 left-3 w-56 max-w-[calc(100%-1.5rem)] rounded-md border border-slate-700 bg-slate-950/85 p-3 text-[10px] text-gray-300 shadow-lg">
                <div>连接数（入边 + 出边）</div>
                <div className="mt-2 flex items-center gap-3">
                  <span className="flex items-center gap-1"><span className="h-2.5 w-2.5 rounded-full bg-slate-500" />0</span>
                  {maxDegree > 0 && (
                    <div className="min-w-0 flex-1">
                      <div className="h-2 rounded-sm" style={{ background: DEGREE_GRADIENT }} />
                      <div className="mt-1 flex justify-between gap-1 text-gray-400">
                        <span>1</span>
                        {midDegree > 1 && midDegree < maxDegree && <span>{midDegree}</span>}
                        {maxDegree > 1 && <span>{maxDegree}</span>}
                      </div>
                    </div>
                  )}
                </div>
                {maxDegree === 0 && <div className="mt-1 text-gray-400">当前无连接</div>}
              </div>
            </div>
          </div>
          <aside className="card w-full shrink-0 space-y-2 text-sm lg:w-72">
            <h2 className="text-xs font-medium uppercase text-gray-400">节点详情</h2>
            {active ? (
              <>
                <div className="text-sm font-medium leading-snug text-gray-100">{active.title || active.label}</div>
                <div className="space-y-1 text-xs text-gray-400">
                  {active.author && <div>作者：{active.author}</div>}
                  <div>年份：{active.year ?? "—"}</div>
                  <div>连接数：{degreeById.get(active.id) ?? 0}</div>
                  <div>引用数：{active.citations ?? active.inbound ?? "—"}</div>
                  <div>{active.in_library ? "库内论文" : "外部节点"}</div>
                </div>
                <button type="button" className="btn w-full text-xs" onClick={() => setFocusId(focusId === active.id ? null : active.id)}>
                  <Focus size={12} aria-hidden="true" /> {focusId === active.id ? "退出邻接聚焦" : "仅显示相邻节点"}
                </button>
                {active.in_library && active.paper_id && <Link to={`/paper/${active.paper_id}`} className="btn w-full text-xs">打开论文</Link>}
              </>
            ) : <p className="py-4 text-xs text-gray-400">点击节点查看详情。放大到 115% 后显示节点标签。</p>}
          </aside>
        </div>
      )}
    </div>
  );
}
