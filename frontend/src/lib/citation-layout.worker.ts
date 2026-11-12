import cytoscape, { type NodeSingular } from "cytoscape";
import {
  citationNodeSize,
  createGridPositions,
  separateOverlappingNodes,
  type CitationLayoutRequest,
  type CitationLayoutResponse,
  type CitationPositions,
} from "@/lib/citation-layout";

interface WorkerScope {
  onmessage: ((event: MessageEvent<CitationLayoutRequest>) => void) | null;
  postMessage: (response: CitationLayoutResponse) => void;
}

const workerScope = self as unknown as WorkerScope;

workerScope.onmessage = (event) => {
  const started = performance.now();
  try {
    const { nodes, edges } = event.data;
    const nodeIds = new Set(nodes.map((node) => node.id));
    const connectedEdges = edges.filter((edge) => nodeIds.has(edge.source) && nodeIds.has(edge.target));
    const connectedIds = new Set(connectedEdges.flatMap((edge) => [edge.source, edge.target]));
    const connectedNodes = nodes.filter((node) => connectedIds.has(node.id));
    const isolatedNodes = nodes.filter((node) => !connectedIds.has(node.id));
    let positions: CitationPositions = {};

    if (connectedNodes.length === 1) {
      positions[connectedNodes[0].id] = { x: 0, y: 0 };
    } else if (connectedNodes.length > 1) {
      const cy = cytoscape({
        headless: true,
        styleEnabled: true,
        elements: [
          ...connectedNodes.map((node) => ({ data: { ...node } })),
          ...connectedEdges.map((edge) => ({ data: { ...edge } })),
        ],
        style: [{
          selector: "node",
          style: {
            label: "",
            width: (node: NodeSingular) => citationNodeSize(node.data("citations")),
            height: (node: NodeSingular) => citationNodeSize(node.data("citations")),
            "border-width": 1,
          },
        }],
      });
      let rawPositions: CitationPositions;
      try {
        cy.layout({
          name: "cose",
          animate: false,
          fit: false,
          randomize: true,
          numIter: 250,
          nodeRepulsion: 8000,
          idealEdgeLength: 90,
        }).run();
        rawPositions = Object.fromEntries(cy.nodes().map((node) => [node.id(), { x: node.position("x"), y: node.position("y") }]));
      } finally {
        cy.destroy();
      }
      positions = separateOverlappingNodes(connectedNodes, rawPositions);
    }

    if (isolatedNodes.length > 0) {
      const isolatedPositions = createGridPositions(isolatedNodes);
      if (connectedNodes.length === 0) {
        positions = isolatedPositions;
      } else {
        const bounds = (ids: string[], values: CitationPositions) => {
          const boxes = ids.map((id) => {
            const node = nodes.find((item) => item.id === id)!;
            const radius = citationNodeSize(node.citations) / 2;
            const point = values[id];
            return { minX: point.x - radius, maxX: point.x + radius, minY: point.y - radius, maxY: point.y + radius };
          });
          return {
            minX: Math.min(...boxes.map((box) => box.minX)),
            maxX: Math.max(...boxes.map((box) => box.maxX)),
            minY: Math.min(...boxes.map((box) => box.minY)),
            maxY: Math.max(...boxes.map((box) => box.maxY)),
          };
        };
        const connectedBounds = bounds(connectedNodes.map((node) => node.id), positions);
        const isolatedBounds = bounds(isolatedNodes.map((node) => node.id), isolatedPositions);
        const offsetX = (connectedBounds.minX + connectedBounds.maxX - isolatedBounds.minX - isolatedBounds.maxX) / 2;
        const offsetY = connectedBounds.maxY + 80 - isolatedBounds.minY;
        for (const node of isolatedNodes) {
          positions[node.id] = {
            x: isolatedPositions[node.id].x + offsetX,
            y: isolatedPositions[node.id].y + offsetY,
          };
        }
      }
    }

    workerScope.postMessage({ ok: true, positions, durationMs: performance.now() - started });
  } catch (reason: unknown) {
    workerScope.postMessage({ ok: false, message: reason instanceof Error ? reason.message : String(reason) });
  }
};
