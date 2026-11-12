import type { CitationEdge, CitationNode } from "@/lib/api";

export const CITATION_NODE_GAP = 8;

const BORDER_ALLOWANCE = 2;
const MAX_SEARCH_RINGS = 128;

export interface CitationPosition {
  x: number;
  y: number;
}

export type CitationPositions = Record<string, CitationPosition>;

export interface CitationLayoutRequest {
  nodes: CitationNode[];
  edges: CitationEdge[];
}

export interface CitationLayoutSuccess {
  ok: true;
  positions: CitationPositions;
  durationMs: number;
}

export interface CitationLayoutFailure {
  ok: false;
  message: string;
}

export type CitationLayoutResponse = CitationLayoutSuccess | CitationLayoutFailure;

type LayoutNode = Pick<CitationNode, "id" | "citations">;

interface LayoutBody {
  id: string;
  radius: number;
}

function bodyRadius(node: LayoutNode): number {
  return citationNodeSize(node.citations) / 2 + BORDER_ALLOWANCE;
}

export function citationNodeSize(citations: number | null | undefined): number {
  return Math.max(20, Math.min(60, (Number(citations) || 1) * 0.5));
}

export function createGridPositions(
  nodes: LayoutNode[],
  center: CitationPosition = { x: 0, y: 0 },
): CitationPositions {
  if (nodes.length === 0) return {};
  const columns = Math.ceil(Math.sqrt(nodes.length));
  const rows = Math.ceil(nodes.length / columns);
  const maxDiameter = Math.max(...nodes.map((node) => citationNodeSize(node.citations)));
  const cellSize = maxDiameter + 2 * BORDER_ALLOWANCE + 2 * CITATION_NODE_GAP;
  const startX = center.x - ((columns - 1) * cellSize) / 2;
  const startY = center.y - ((rows - 1) * cellSize) / 2;
  return Object.fromEntries(nodes.map((node, index) => [node.id, {
    x: startX + (index % columns) * cellSize,
    y: startY + Math.floor(index / columns) * cellSize,
  }]));
}

export function separateOverlappingNodes(
  nodes: LayoutNode[],
  positions: CitationPositions,
): CitationPositions {
  const next = Object.fromEntries(nodes.map((node) => [
    node.id,
    { x: positions[node.id]?.x ?? 0, y: positions[node.id]?.y ?? 0 },
  ]));
  const bodies = nodes.map((node) => ({ id: node.id, radius: bodyRadius(node) }));
  if (bodies.length < 2) return next;

  const maxRadius = Math.max(...bodies.map((body) => body.radius));
  const cellSize = 2 * maxRadius + CITATION_NODE_GAP;
  const grid = new Map<string, LayoutBody[]>();
  const placed: LayoutBody[] = [];
  const cellKey = (x: number, y: number) => `${Math.floor(x / cellSize)},${Math.floor(y / cellSize)}`;

  const isFree = (body: LayoutBody, x: number, y: number) => {
    const cellX = Math.floor(x / cellSize);
    const cellY = Math.floor(y / cellSize);
    for (let dx = -1; dx <= 1; dx++) {
      for (let dy = -1; dy <= 1; dy++) {
        const neighbors = grid.get(`${cellX + dx},${cellY + dy}`) ?? [];
        for (const other of neighbors) {
          const point = next[other.id];
          const minimum = body.radius + other.radius + CITATION_NODE_GAP;
          if (Math.hypot(x - point.x, y - point.y) < minimum - 0.000001) return false;
        }
      }
    }
    return true;
  };

  const place = (body: LayoutBody) => {
    const point = next[body.id];
    const key = cellKey(point.x, point.y);
    const cell = grid.get(key) ?? [];
    cell.push(body);
    grid.set(key, cell);
    placed.push(body);
  };

  const ordered = [...bodies].sort((a, b) => b.radius - a.radius || a.id.localeCompare(b.id));
  for (const body of ordered) {
    const origin = next[body.id];
    if (!isFree(body, origin.x, origin.y)) {
      const step = Math.max(CITATION_NODE_GAP, body.radius / 2);
      let candidate: CitationPosition | null = null;
      for (let ring = 1; ring <= MAX_SEARCH_RINGS && !candidate; ring++) {
        const distance = ring * step;
        const count = Math.max(16, Math.ceil((2 * Math.PI * distance) / step));
        for (let index = 0; index < count; index++) {
          const angle = (2 * Math.PI * index) / count;
          const x = origin.x + distance * Math.cos(angle);
          const y = origin.y + distance * Math.sin(angle);
          if (isFree(body, x, y)) {
            candidate = { x, y };
            break;
          }
        }
      }
      if (!candidate) {
        const alternatives = [
          { x: Math.max(...placed.map((other) => next[other.id].x + other.radius + CITATION_NODE_GAP)) + body.radius + CITATION_NODE_GAP, y: origin.y },
          { x: Math.min(...placed.map((other) => next[other.id].x - other.radius - CITATION_NODE_GAP)) - body.radius - CITATION_NODE_GAP, y: origin.y },
          { x: origin.x, y: Math.max(...placed.map((other) => next[other.id].y + other.radius + CITATION_NODE_GAP)) + body.radius + CITATION_NODE_GAP },
          { x: origin.x, y: Math.min(...placed.map((other) => next[other.id].y - other.radius - CITATION_NODE_GAP)) - body.radius - CITATION_NODE_GAP },
        ];
        candidate = alternatives.reduce((nearest, option) =>
          Math.hypot(option.x - origin.x, option.y - origin.y) < Math.hypot(nearest.x - origin.x, nearest.y - origin.y) ? option : nearest,
        );
      }
      next[body.id] = candidate;
    }
    place(body);
  }
  return next;
}
