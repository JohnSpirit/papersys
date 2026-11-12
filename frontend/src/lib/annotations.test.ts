import { describe, expect, it } from "vitest";
import {
  applyAnnotations,
  cleanupAnnotations,
  focusAnnotation,
  parseAnnotation,
  selectionFromRange,
  type AnnotationMark,
} from "@/lib/annotations";
import type { Annotation } from "@/lib/api";

function rootWithText(text = "alpha beta gamma"): HTMLElement {
  const root = document.createElement("div");
  root.textContent = text;
  document.body.appendChild(root);
  return root;
}

describe("annotation positions", () => {
  it("stores selection offsets relative to the rendered document", () => {
    const root = rootWithText();
    const text = root.firstChild as Text;
    const range = document.createRange();
    range.setStart(text, 6);
    range.setEnd(text, 10);
    expect(selectionFromRange(root, range)).toEqual({ quote: "beta", position: { start: 6, end: 10 } });
  });

  it("uses saved positions and falls back to the first matching quote", () => {
    const root = rootWithText("beta alpha beta");
    const annotations: AnnotationMark[] = [
      { id: 1, quote: "beta", note: "", position: { start: 12, end: 16 } },
      { id: 2, quote: "alpha", note: "", position: null },
    ];
    applyAnnotations(root, annotations);
    expect(root.querySelector("mark[data-annotation-id='1']")).toHaveTextContent("beta");
    expect(root.querySelector("mark[data-annotation-id='2']")).toHaveTextContent("alpha");
    expect(root.textContent).toBe("beta alpha beta");
    expect(focusAnnotation(root, 1)).toBe(true);
    expect(root.querySelector("mark[data-annotation-id='1']")).toHaveClass("annotation-target");
    cleanupAnnotations(root);
    expect(root.querySelector("mark")).toBeNull();
    expect(root.textContent).toBe("beta alpha beta");
  });

  it("parses existing annotations and keeps comments without quotes", () => {
    const annotation: Annotation = {
      id: 3,
      paper_id: 1,
      type: "comment",
      anchor: "en",
      payload_json: JSON.stringify({ quote: "", note: "普通评论", position: null }),
      source: "web",
      created_by_user_id: null,
      created_at: "2026-01-01 00:00:00",
    };
    expect(parseAnnotation(annotation)?.note).toBe("普通评论");
    expect(parseAnnotation(annotation)?.quote).toBe("");
  });
});
