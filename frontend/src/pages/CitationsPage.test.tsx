import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { api, type CitationGraph, type CitationNode } from "@/lib/api";
import { CitationsPage } from "@/pages/CitationsPage";
import { ToastProvider } from "@/components/Toast";

const graphMocks = vi.hoisted(() => {
  interface MockNode {
    id: () => string;
    filtered: boolean;
    comfortable: boolean;
    toggleClass: (name: string, state: boolean) => void;
  }
  interface MockEdge {
    id: string;
    comfortable: boolean;
    toggleClass: (name: string, state: boolean) => void;
  }
  interface MockCollection {
    length: number;
    forEach: (callback: (node: MockNode) => void) => void;
    filter: (callback: (node: MockNode) => boolean) => MockCollection;
  }
  const handlers: { tap?: (event: unknown) => void } = {};
  const nodes: MockNode[] = [];
  const edges: MockEdge[] = [];
  const collection = (items: MockNode[]): MockCollection => ({
    length: items.length,
    forEach: (callback) => items.forEach(callback),
    filter: (callback) => collection(items.filter(callback)),
  });
  const styleApi = {
    selector: vi.fn(),
    style: vi.fn(),
    update: vi.fn(),
  };
  styleApi.selector.mockReturnValue(styleApi);
  styleApi.style.mockReturnValue(styleApi);
  const cy = {
    destroy: vi.fn(),
    batch: vi.fn((callback: () => void) => callback()),
    nodes: vi.fn(() => collection(nodes)),
    edges: vi.fn(() => ({ forEach: (callback: (edge: MockEdge) => void) => edges.forEach(callback) })),
    on: vi.fn((event: string, selector: string, handler?: (event: unknown) => void) => {
      if (event === "tap" && selector === "node" && handler) handlers.tap = handler;
    }),
    style: vi.fn(() => styleApi),
    zoom: vi.fn(() => 1),
    fit: vi.fn(),
  };
  const factory = vi.fn((options: {
    elements: { data: { id: string; source?: string; degree?: number; [key: string]: unknown } }[];
    style: { selector: string; style: Record<string, unknown> }[];
  }) => {
    nodes.splice(0, nodes.length, ...options.elements.filter((element) => !element.data.source).map((element) => ({
      id: () => element.data.id,
      filtered: false,
      comfortable: false,
      toggleClass: (name: string, state: boolean) => {
        const item = nodes.find((candidate) => candidate.id() === element.data.id);
        if (item && name === "filtered") item.filtered = state;
        if (item && name === "comfortable") item.comfortable = state;
      },
    })));
    edges.splice(0, edges.length, ...options.elements.filter((element) => element.data.source).map((element) => ({
      id: element.data.id,
      comfortable: false,
      toggleClass: (name: string, state: boolean) => {
        const item = edges.find((candidate) => candidate.id === element.data.id);
        if (item && name === "comfortable") item.comfortable = state;
      },
    })));
    return cy;
  });
  return { cy, handlers, nodes, edges, factory };
});

vi.mock("cytoscape", () => ({ default: graphMocks.factory }));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, api: { ...actual.api, citations: vi.fn(), citationsRebuild: vi.fn() } };
});

function node(id: string): CitationNode {
  return {
    id,
    label: `paper${id}`,
    title: `Paper ${id}`,
    year: 2026,
    author: "author",
    citations: 1,
    in_library: true,
    paper_id: Number(id.slice(1)),
  };
}

const graph: CitationGraph = {
  nodes: [node("p1"), node("p2"), node("p3")],
  edges: [{ id: "e1", source: "p1", target: "p2" }],
  stats: { library_nodes: 3, external_nodes: 0, edges: 1 },
};

beforeEach(() => {
  window.localStorage.setItem("paper.citations.comfort", "0");
  graphMocks.factory.mockClear();
  graphMocks.nodes.splice(0);
  graphMocks.edges.splice(0);
  graphMocks.handlers.tap = undefined;
  vi.mocked(api.citations).mockResolvedValue(graph);
  vi.mocked(api.citationsRebuild).mockResolvedValue({ task_id: 99 });
});

function renderCitations() {
  const router = createMemoryRouter([
    { path: "/citations", element: <ToastProvider><CitationsPage /></ToastProvider> },
    { path: "/paper/:id", element: <div>paper</div> },
  ], { initialEntries: ["/citations"] });
  return render(<RouterProvider router={router} />);
}

function visibleNodeIds() {
  return graphMocks.nodes.filter((node) => !node.filtered).map((node) => node.id());
}

function graphOptions() {
  const call = graphMocks.factory.mock.calls.at(-1);
  if (!call) throw new Error("Cytoscape has not been created");
  return call[0];
}

function nodeDegree(id: string) {
  return graphOptions().elements.find((element) => element.data.id === id)?.data.degree;
}

function nodeColor(id: string) {
  const element = graphOptions().elements.find((candidate) => candidate.data.id === id);
  const color = graphOptions().style.find((style) => style.selector === "node")?.style["background-color"];
  if (!element || typeof color !== "function") throw new Error("Node color is unavailable");
  return (color as (node: { data: (key: string) => unknown }) => string)({
    data: (key: string) => element.data[key],
  });
}

describe("CitationsPage", () => {
  it("reduces visual density and can reveal every node", async () => {
    window.localStorage.setItem("paper.citations.comfort", "1");
    const comfortNodes = Array.from({ length: 12 }, (_, index) => node(`p${index + 1}`));
    const comfortEdges: CitationGraph["edges"] = [{ id: "e1_2", source: "p1", target: "p2" }];
    for (let index = 3; index <= 12; index++) {
      comfortEdges.push({ id: `e${index}`, source: index <= 7 ? "p1" : "p2", target: `p${index}` });
    }
    vi.mocked(api.citations).mockResolvedValueOnce({
      nodes: comfortNodes,
      edges: comfortEdges,
      stats: { library_nodes: 12, external_nodes: 0, edges: 11 },
    });
    const user = userEvent.setup();
    renderCitations();
    await waitFor(() => expect(graphMocks.factory).toHaveBeenCalledTimes(1));
    expect(screen.getByLabelText("舒适视图")).toBeChecked();
    await waitFor(() => expect(visibleNodeIds()).toEqual(["p1", "p2"]));
    expect(graphMocks.nodes.every((node) => node.comfortable)).toBe(true);
    expect(graphMocks.edges.every((edge) => edge.comfortable)).toBe(true);
    const comfortableStyle = graphOptions().style.find((style) => style.selector === "node.comfortable")?.style;
    expect(comfortableStyle?.shape).toBe("round-rectangle");
    expect((comfortableStyle?.width as (node: { data: (key: string) => number }) => number)({ data: () => 1 })).toBe(16);
    expect(graphOptions().style.find((style) => style.selector === "edge.comfortable")?.style.opacity).toBe(0.25);
    await user.type(screen.getByLabelText("标题、目录或作者"), "Paper p3");
    await waitFor(() => expect(visibleNodeIds()).toEqual(["p3"]));
    await user.clear(screen.getByLabelText("标题、目录或作者"));
    await waitFor(() => expect(visibleNodeIds()).toEqual(["p1", "p2"]));
    await user.click(screen.getByRole("button", { name: "显示全部节点" }));
    await waitFor(() => expect(visibleNodeIds()).toHaveLength(12));
    expect(graphMocks.nodes.every((node) => !node.comfortable)).toBe(true);
    expect(graphMocks.edges.every((edge) => !edge.comfortable)).toBe(true);
    expect(graphMocks.factory).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(window.localStorage.getItem("paper.citations.comfort")).toBe("0"));
  });

  it("colors nodes by connection count and shows a color bar", async () => {
    const degreeGraph: CitationGraph = {
      nodes: [node("p1"), node("p2"), node("p3"), node("p4"), node("p5")],
      edges: [
        { id: "e1", source: "p1", target: "p2" },
        { id: "e2", source: "p1", target: "p3" },
        { id: "e3", source: "p4", target: "p1" },
      ],
      stats: { library_nodes: 5, external_nodes: 0, edges: 3 },
    };
    vi.mocked(api.citations).mockResolvedValueOnce(degreeGraph);
    renderCitations();
    await waitFor(() => expect(graphMocks.factory).toHaveBeenCalledTimes(1));
    expect(nodeDegree("p1")).toBe(3);
    expect(nodeDegree("p2")).toBe(1);
    expect(nodeDegree("p3")).toBe(1);
    expect(nodeDegree("p4")).toBe(1);
    expect(nodeDegree("p5")).toBe(0);
    expect(nodeColor("p1")).toBe("rgb(250,204,21)");
    expect(nodeColor("p2")).toBe("rgb(59,130,246)");
    expect(nodeColor("p5")).toBe("#64748b");
    const legend = screen.getByRole("img", { name: "节点连接数颜色条" });
    expect(legend).toHaveTextContent("连接数（入边 + 出边）");
    expect(legend).toHaveTextContent("1");
    expect(legend).toHaveTextContent("2");
    expect(legend).toHaveTextContent("3");
    act(() => graphMocks.handlers.tap?.({ target: { data: () => degreeGraph.nodes[0] } }));
    expect(screen.getByText("连接数：3")).toBeInTheDocument();
  });

  it("changes node visibility without recreating the graph", async () => {
    const user = userEvent.setup();
    renderCitations();
    await waitFor(() => expect(graphMocks.factory).toHaveBeenCalledTimes(1));
    expect(visibleNodeIds()).toEqual(["p1", "p2"]);
    await user.click(screen.getByLabelText("隐藏孤立节点"));
    await waitFor(() => expect(visibleNodeIds()).toEqual(["p1", "p2", "p3"]));
    await user.type(screen.getByLabelText("标题、目录或作者"), "Paper p1");
    await waitFor(() => expect(visibleNodeIds()).toEqual(["p1"]));
    expect(graphMocks.factory).toHaveBeenCalledTimes(1);
  });

  it("focuses neighbors and restores the graph after an empty result", async () => {
    const user = userEvent.setup();
    renderCitations();
    await waitFor(() => expect(graphMocks.factory).toHaveBeenCalledTimes(1));
    act(() => graphMocks.handlers.tap?.({ target: { data: () => graph.nodes[0] } }));
    await user.click(screen.getByRole("button", { name: "仅显示相邻节点" }));
    expect(screen.getAllByRole("button", { name: "退出邻接聚焦" }).length).toBeGreaterThan(0);
    expect(visibleNodeIds()).toEqual(["p1", "p2"]);
    await user.type(screen.getByLabelText("标题、目录或作者"), "no matching paper");
    expect(await screen.findByText("当前筛选下没有可显示的节点")).toBeInTheDocument();
    expect(graphMocks.factory).toHaveBeenCalledTimes(1);
    await user.clear(screen.getByLabelText("标题、目录或作者"));
    await waitFor(() => expect(graphMocks.factory).toHaveBeenCalledTimes(2));
    expect(visibleNodeIds()).toEqual(["p1", "p2"]);
  });

  it("shows a retryable error instead of a blank graph", async () => {
    const user = userEvent.setup();
    vi.mocked(api.citations).mockRejectedValueOnce(new Error("graph failed"));
    renderCitations();
    expect(await screen.findByText("graph failed")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "重试" }));
    await waitFor(() => expect(graphMocks.factory).toHaveBeenCalled());
  });
});
