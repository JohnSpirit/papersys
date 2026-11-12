import { useEffect, useRef } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import rehypeRaw from "rehype-raw";
import rehypeKatex from "rehype-katex";
import rehypeSanitize, { defaultSchema } from "rehype-sanitize";
import "katex/dist/katex.min.css";
import {
  applyAnnotations,
  cleanupAnnotations,
  focusAnnotation,
  selectionFromRange,
  type AnnotationMark,
  type TextSelection,
} from "@/lib/annotations";
import { splitDetailsBlocks, type MarkdownSegment } from "@/lib/markdown-details";
import { apiUrl } from "@/lib/api";

const sanitizeSchema = {
  ...defaultSchema,
  tagNames: Array.from(new Set([
    ...(defaultSchema.tagNames || []),
    "details", "summary", "table", "thead", "tbody", "tfoot", "tr", "th", "td", "sub", "sup",
  ])),
  attributes: {
    ...defaultSchema.attributes,
    "*": [...(defaultSchema.attributes?.["*"] || []), "className", "aria-hidden"],
    details: [...(defaultSchema.attributes?.details || []), "open"],
    th: [...(defaultSchema.attributes?.th || []), "align", "colSpan", "rowSpan"],
    td: [...(defaultSchema.attributes?.td || []), "align", "colSpan", "rowSpan"],
    img: [...(defaultSchema.attributes?.img || []), "src", "alt", "title", "width", "height", "loading"],
  },
};

interface MarkdownViewProps {
  paperId: number;
  text: string;
  className?: string;
  annotations?: AnnotationMark[];
  onSelect?: (selection: TextSelection) => void;
  focusAnnotationId?: number | null;
}

function MarkdownContent({ paperId, text }: { paperId: number; text: string }) {
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm, remarkMath]}
      rehypePlugins={[rehypeRaw, [rehypeSanitize, sanitizeSchema], rehypeKatex]}
      urlTransform={(url, key) => {
        if (key === "src" && /^images\//i.test(url)) return apiUrl(`/api/papers/${paperId}/${url}`);
        if (key === "src" || key === "href") {
          if (/^(https?:|mailto:|#|\/)/i.test(url)) return url;
          if (key === "src" && /^data:image\/(?:png|jpe?g|gif|webp);/i.test(url)) return url;
          return apiUrl(`/api/papers/${paperId}/${url.replace(/^\.\//, "")}`);
        }
        return url;
      }}
      components={{
        img: ({ src, alt }) => (
          <img
            src={typeof src === "string" ? src : ""}
            alt={alt ?? ""}
            className="my-2 max-w-full rounded border border-side-border"
            loading="lazy"
          />
        ),
        table: ({ children }) => (
          <div className="my-3 overflow-x-auto">
            <table className="border-collapse text-sm">{children}</table>
          </div>
        ),
        th: ({ children }) => <th className="border border-side-border bg-gray-800/60 px-2 py-1 text-left">{children}</th>,
        td: ({ children }) => <td className="border border-side-border px-2 py-1">{children}</td>,
        a: ({ href, children }) => {
          const external = !!href && /^https?:/i.test(href);
          return (
            <a
              href={href}
              target={external ? "_blank" : undefined}
              rel={external ? "noreferrer" : undefined}
              className="break-all text-blue-300 underline hover:text-blue-200"
            >
              {children}
            </a>
          );
        },
        code: ({ className: cls, children }) => {
          const isBlock = /language-/.test(cls ?? "");
          if (isBlock) return <code className={cls}>{children}</code>;
          return <code className="rounded bg-gray-800/70 px-1 py-0.5 text-sm font-mono text-amber-200">{children}</code>;
        },
      }}
    >
      {text}
    </ReactMarkdown>
  );
}

function Segments({ paperId, segments }: { paperId: number; segments: MarkdownSegment[] }) {
  return segments.map((segment, index) => {
    if (segment.kind === "markdown") {
      return <MarkdownContent key={index} paperId={paperId} text={segment.text} />;
    }
    return (
      <details key={index} open={segment.open}>
        <summary>{segment.summary}</summary>
        <Segments paperId={paperId} segments={splitDetailsBlocks(segment.text)} />
      </details>
    );
  });
}

export function MarkdownView({
  paperId,
  text,
  className,
  annotations = [],
  onSelect,
  focusAnnotationId = null,
}: MarkdownViewProps) {
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const root = rootRef.current;
    if (!root) return;
    applyAnnotations(root, annotations);
    return () => cleanupAnnotations(root);
  }, [text, annotations]);

  useEffect(() => {
    const root = rootRef.current;
    if (root) focusAnnotation(root, focusAnnotationId);
  }, [focusAnnotationId, annotations, text]);

  function handleMouseUp() {
    if (!onSelect || !rootRef.current) return;
    const selection = window.getSelection();
    if (!selection || selection.rangeCount === 0) return;
    const selected = selectionFromRange(rootRef.current, selection.getRangeAt(0));
    if (selected) onSelect(selected);
  }

  return (
    <div
      ref={rootRef}
      className={`markdown-body text-gray-200 ${className ?? ""}`}
      onMouseUp={handleMouseUp}
    >
      <Segments paperId={paperId} segments={splitDetailsBlocks(text || "")} />
    </div>
  );
}
