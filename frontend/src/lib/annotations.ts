import type { Annotation, AnnotationPosition } from "@/lib/api";

export interface AnnotationMark {
  id: number;
  quote: string;
  note: string;
  position?: AnnotationPosition | null;
  created_by_user_id?: number | null;
}

export interface TextSelection {
  quote: string;
  position: AnnotationPosition;
}

export function parseAnnotation(annotation: Annotation): AnnotationMark | null {
  try {
    const payload = JSON.parse(annotation.payload_json || "{}") as {
      quote?: unknown;
      note?: unknown;
      position?: { start?: unknown; end?: unknown } | null;
    };
    if (typeof payload.quote !== "string") return null;
    const start = payload.position?.start;
    const end = payload.position?.end;
    const position = Number.isInteger(start) && Number.isInteger(end)
      ? { start: start as number, end: end as number }
      : null;
    return {
      id: annotation.id,
      quote: payload.quote,
      note: typeof payload.note === "string" ? payload.note : "",
      position,
      created_by_user_id: annotation.created_by_user_id,
    };
  } catch {
    return null;
  }
}

export function selectionFromRange(root: HTMLElement, range: Range): TextSelection | null {
  if (range.collapsed) return null;
  const raw = range.toString();
  const quote = raw.trim();
  if (!quote || quote.length >= 2000) return null;
  const before = document.createRange();
  before.selectNodeContents(root);
  before.setEnd(range.startContainer, range.startOffset);
  const leading = raw.length - raw.trimStart().length;
  const start = before.toString().length + leading;
  return { quote, position: { start, end: start + quote.length } };
}

function textNodes(root: HTMLElement): Text[] {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const nodes: Text[] = [];
  let current = walker.nextNode();
  while (current) {
    if (current.nodeValue) nodes.push(current as Text);
    current = walker.nextNode();
  }
  return nodes;
}

function highlightOne(root: HTMLElement, annotation: AnnotationMark): void {
  if (!annotation.quote.trim()) return;
  const nodes = textNodes(root);
  const fullText = nodes.map((node) => node.data).join("");
  const saved = annotation.position;
  let start = saved?.start ?? -1;
  let end = saved?.end ?? -1;
  if (
    start < 0 || end <= start || end > fullText.length
    || fullText.slice(start, end) !== annotation.quote
  ) {
    start = fullText.indexOf(annotation.quote);
    end = start >= 0 ? start + annotation.quote.length : -1;
  }
  if (start < 0 || end < 0) return;

  let offset = 0;
  for (const node of nodes) {
    const nodeStart = offset;
    const nodeEnd = offset + node.data.length;
    const from = Math.max(start, nodeStart);
    const to = Math.min(end, nodeEnd);
    if (from < to) {
      const range = document.createRange();
      range.setStart(node, from - nodeStart);
      range.setEnd(node, to - nodeStart);
      const mark = document.createElement("mark");
      mark.className = "paper-annotation";
      mark.dataset.annotationId = String(annotation.id);
      if (annotation.note) mark.title = annotation.note;
      range.surroundContents(mark);
    }
    offset = nodeEnd;
    if (offset >= end) break;
  }
}

export function applyAnnotations(root: HTMLElement, annotations: AnnotationMark[]): void {
  cleanupAnnotations(root);
  annotations.forEach((annotation) => highlightOne(root, annotation));
}

export function cleanupAnnotations(root: HTMLElement): void {
  root.querySelectorAll("mark.paper-annotation").forEach((mark) => {
    mark.replaceWith(...Array.from(mark.childNodes));
  });
  root.normalize();
}

export function focusAnnotation(root: HTMLElement, annotationId: number | null): boolean {
  root.querySelectorAll("mark.annotation-target").forEach((mark) => mark.classList.remove("annotation-target"));
  if (annotationId === null) return false;
  const marks = root.querySelectorAll<HTMLElement>(`mark[data-annotation-id="${annotationId}"]`);
  if (marks.length === 0) return false;
  marks.forEach((mark) => mark.classList.add("annotation-target"));
  marks[0].scrollIntoView({ behavior: "smooth", block: "center" });
  return true;
}
