import { describe, expect, it } from "vitest";
import {
  CITATION_NODE_GAP,
  citationNodeSize,
  createGridPositions,
  separateOverlappingNodes,
  type CitationPositions,
} from "@/lib/citation-layout";

type TestNode = { id: string; citations: number };

function expectSeparated(nodes: TestNode[], positions: CitationPositions) {
  for (let i = 0; i < nodes.length; i++) {
    for (let j = i + 1; j < nodes.length; j++) {
      const a = nodes[i];
      const b = nodes[j];
      const minimum = citationNodeSize(a.citations) / 2 + citationNodeSize(b.citations) / 2 + 4 + CITATION_NODE_GAP;
      const distance = Math.hypot(positions[a.id].x - positions[b.id].x, positions[a.id].y - positions[b.id].y);
      expect(distance, `${a.id} and ${b.id}`).toBeGreaterThanOrEqual(minimum - 0.0001);
    }
  }
}

describe("citation layout", () => {
  it("uses the same bounded node size as the graph style", () => {
    expect(citationNodeSize(null)).toBe(20);
    expect(citationNodeSize(undefined)).toBe(20);
    expect(citationNodeSize(10)).toBe(20);
    expect(citationNodeSize(100)).toBe(50);
    expect(citationNodeSize(1000)).toBe(60);
  });

  it("separates coincident nodes without mutating their input positions", () => {
    const nodes = [{ id: "a", citations: 1000 }, { id: "b", citations: 1000 }];
    const input = { a: { x: 0, y: 0 }, b: { x: 0, y: 0 } };
    const output = separateOverlappingNodes(nodes, input);
    expect(input).toEqual({ a: { x: 0, y: 0 }, b: { x: 0, y: 0 } });
    expectSeparated(nodes, output);
  });

  it("deterministically separates a dense cluster", () => {
    const nodes = Array.from({ length: 40 }, (_, index) => ({
      id: `p${index}`,
      citations: index % 3 === 0 ? 1000 : index % 2 === 0 ? 100 : 20,
    }));
    const input = Object.fromEntries(nodes.map((node, index) => [node.id, { x: index % 4, y: index % 5 }]));
    const output = separateOverlappingNodes(nodes, input);
    expectSeparated(nodes, output);
    expect(separateOverlappingNodes(nodes, input)).toEqual(output);
  });

  it("places grid nodes with the required spacing", () => {
    const nodes = Array.from({ length: 31 }, (_, index) => ({ id: `p${index}`, citations: index * 20 }));
    expectSeparated(nodes, createGridPositions(nodes));
  });
});
