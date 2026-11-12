export type MarkdownSegment =
  | { kind: "markdown"; text: string }
  | { kind: "details"; text: string; summary: string; open: boolean };

export function splitDetailsBlocks(text: string): MarkdownSegment[] {
  const segments: MarkdownSegment[] = [];
  const tags = /<\/?details\b[^>]*>/gi;
  let cursor = 0;
  let match: RegExpExecArray | null;
  while ((match = tags.exec(text))) {
    if (/^<\//.test(match[0])) continue;
    const openEnd = tags.lastIndex;
    let depth = 1;
    let close: RegExpExecArray | null = null;
    let nested: RegExpExecArray | null;
    while ((nested = tags.exec(text))) {
      if (/^<\//.test(nested[0])) depth -= 1;
      else depth += 1;
      if (depth === 0) {
        close = nested;
        break;
      }
    }
    if (!close) break;
    const body = text.slice(openEnd, close.index);
    const summary = /<summary\b[^>]*>([\s\S]*?)<\/summary>/i.exec(body);
    if (match.index > cursor) segments.push({ kind: "markdown", text: text.slice(cursor, match.index) });
    segments.push({
      kind: "details",
      text: summary ? body.replace(summary[0], "") : body,
      summary: summary ? summary[1].replace(/<[^>]+>/g, "").trim() || "详情" : "详情",
      open: /\sopen(?:\s|=|$)/i.test(match[0]),
    });
    cursor = tags.lastIndex;
    tags.lastIndex = cursor;
  }
  if (cursor < text.length) segments.push({ kind: "markdown", text: text.slice(cursor) });
  return segments.length > 0 ? segments : [{ kind: "markdown", text }];
}
