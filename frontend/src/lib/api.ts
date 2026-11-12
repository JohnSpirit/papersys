export type PaperStatus = "missing" | "processing" | "ok" | "partial" | "error" | "manual_hold" | "skipped";

export interface Paper {
  id: number;
  dir_name: string;
  year: number;
  first_author: string;
  keyword: string | null;
  title: string | null;
  authors: string[] | null;
  journal: string | null;
  doi: string | null;
  arxiv_id: string | null;
  openalex_id: string | null;
  url: string | null;
  cited_by_count: number | null;
  oa_status: string | null;
  pub_type: string | null;
  attachment_kind: string | null;
  parent_paper_id: number | null;
  created_by_user_id: number | null;
  concepts: unknown[] | string | null;
  abstract: string | null;
  bibtex: string | null;
  pdf_path: string | null;
  parsed_path: string | null;
  summary_path: string | null;
  pdf_pages: number | null;
  pdf_size_bytes: number | null;
  parsed_chars: number | null;
  mineru_model: string | null;
  status_pdf: PaperStatus;
  status_parsed: PaperStatus;
  status_enrich: PaperStatus;
  status_translated: PaperStatus;
  status_summary: PaperStatus;
  status_outline: PaperStatus;
  status_embedded: PaperStatus;
  pdf_title: string | null;
  pdf_authors: string | null;
  created_at: string;
  updated_at: string;
}

export interface PapersResp {
  items: Paper[];
  limit: number;
  offset: number;
  count: number;
}

export interface Settings {
  paths: Record<string, string>;
  server: { host: string; port: number; debug: boolean; cors_origins?: string[] };
  workers: Record<string, number>;
  external: Record<string, number>;
  mineru: Record<string, string>;
  llm: { api_url: string; model: string; temperature: number; max_input_chars?: number };
  retrieval: Record<string, string | number | boolean>;
  secrets: { present: Record<string, boolean> };
}

export interface LLMUsageBucket {
  calls: number;
  prompt_tokens: number;
  completion_tokens: number;
  cached_tokens: number;
  prompt_chars: number;
  errors: number;
  cost_input: number;
  cost_cached: number;
  cost_output: number;
  cost_total: number;
}

export interface LLMUsageResp {
  prices: { input_per_mtok: number; cached_input_per_mtok: number; output_per_mtok: number };
  totals: LLMUsageBucket;
  recent30d: LLMUsageBucket;
  by_kind: (LLMUsageBucket & { kind: string })[];
  by_model: (LLMUsageBucket & { model: string })[];
}

export interface GlossaryEntry {
  id: number;
  en: string;
  context: string | null;
  zh: string;
  notes: string | null;
  updated_by_user_id: number | null;
  updated_at: string;
}

export type TaskStatus = "pending" | "running" | "ok" | "error" | "cancelled";

export interface Task {
  id: number;
  kind: string;
  status: TaskStatus;
  progress: number;
  error: string | null;
  retries: number;
  paper_id: number | null;
  payload_json: string;
  payload?: Record<string, unknown>;
  log_path: string | null;
  log_tail?: string;
  created_by_user_id: number | null;
  execution_subject: "user" | "system";
  required_permission: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface TasksResp {
  items: Task[];
  count: number;
  total: number;
  limit: number;
  offset: number;
}

export interface InboxItem {
  name: string;
  path: string;
  size_bytes: number;
  mtime: number;
}

export interface TranslationChunk {
  chunk_index: number;
  status: "ok" | "error" | "pending" | "skipped";
  source: string;
  en_len: number;
  zh_len: number;
  updated_at: string;
}

export interface DocResp {
  doc_type: "en" | "zh" | "summary";
  available: boolean;
  text: string;
  source: string;
  size: number;
  hash: string | null;
}

export interface DocRevision {
  id: number;
  paper_id: number;
  doc_type: "en" | "zh" | "summary";
  content_hash: string;
  source: "auto" | "web_edit" | "agent" | string;
  size_bytes: number | null;
  payload_path: string | null;
  created_by_user_id: number | null;
  ts: string;
  content?: string | null;
}

export interface AnnotationPosition {
  start: number;
  end: number;
}

export interface Annotation {
  id: number;
  paper_id: number;
  type: "highlight" | "callout" | "comment" | string;
  anchor: string | null;
  payload_json: string | null;
  source: string;
  created_by_user_id: number | null;
  created_at: string;
}

export interface SearchResult {
  source: string;
  id: string | null;
  title: string;
  authors: string[];
  year: number | null;
  abstract: string;
  snippet?: string;
  doi: string | null;
  arxiv_id: string | null;
  pmid?: string | null;
  url: string | null;
  pdf_url: string | null;
  journal: string | null;
  cited_by_count: number | null;
  tldr?: string | null;
  score?: number;
}

export interface SemanticItem {
  paper_id: number;
  title: string | null;
  first_author?: string | null;
  year: number | null;
  journal?: string | null;
  attachment_kind?: string | null;
  chunk_index: number;
  score: number;
  text: string;
  char_start?: number;
  char_end?: number;
  low_confidence?: boolean;
}

export interface SemanticSearchResp {
  query: string;
  count: number;
  stage?: "embed" | "rerank" | "embed_fallback";
  threshold?: number;
  rerank_error?: string;
  truncated?: boolean;
  items: SemanticItem[];
  error?: string;
}

export interface CitationNode {
  id: string;
  label: string;
  title: string | null;
  year: number | null;
  author: string | null;
  citations: number | null;
  in_library: boolean;
  paper_id?: number;
  inbound?: number;
}

export interface CitationEdge {
  id: string;
  source: string;
  target: string;
  external?: boolean;
}

export interface CitationGraph {
  nodes: CitationNode[];
  edges: CitationEdge[];
  stats: { library_nodes: number; external_nodes: number; edges: number };
}

export interface SSEvent {
  id: number;
  task_id: number | null;
  paper_id: number | null;
  ts: string;
  level: "info" | "warn" | "err" | "error";
  msg: string;
}

export interface TaskActionResp {
  task_id: number;
  reused?: boolean;
}

export class ApiError extends Error {
  readonly status: number;
  readonly data: unknown;

  constructor(message: string, status: number, data: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.data = data;
  }
}

export function errMsg(error: unknown): string {
  if (error instanceof Error) return error.message;
  return String(error);
}

export function apiUrl(path: string): string {
  if (/^https?:\/\//i.test(path)) return path;
  const base = import.meta.env.BASE_URL || "/";
  return `${base.endsWith("/") ? base : `${base}/`}${path.replace(/^\/+/, "")}`;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(apiUrl(path), init);
  const text = await response.text();
  let data: unknown = null;
  if (text) {
    try { data = JSON.parse(text); }
    catch { data = text; }
  }
  if (!response.ok) {
    const message = typeof data === "object" && data !== null && "error" in data
      ? String((data as { error: unknown }).error)
      : `${response.status} ${response.statusText}`;
    throw new ApiError(message, response.status, data);
  }
  return data as T;
}

function jget<T>(path: string, signal?: AbortSignal): Promise<T> {
  return request<T>(path, { signal });
}

function jpost<T>(path: string, body?: BodyInit | object, signal?: AbortSignal): Promise<T> {
  const init: RequestInit = { method: "POST", signal };
  if (body && !(body instanceof FormData)) {
    init.headers = { "Content-Type": "application/json" };
    init.body = JSON.stringify(body);
  } else if (body) {
    init.body = body;
  }
  return request<T>(path, init);
}

function jput<T>(path: string, body?: object, signal?: AbortSignal): Promise<T> {
  return request<T>(path, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}),
    signal,
  });
}

function jdelete<T>(path: string, signal?: AbortSignal): Promise<T> {
  return request<T>(path, { method: "DELETE", signal });
}

export const api = {
  health: (signal?: AbortSignal) => jget<{ ok: boolean; service: string; version: string }>("/api/health", signal),
  papers: (q: Record<string, string | number | undefined>, signal?: AbortSignal) => {
    const params = new URLSearchParams();
    Object.entries(q).forEach(([key, value]) => {
      if (value !== undefined && value !== "" && value !== null) params.set(key, String(value));
    });
    return jget<PapersResp>(`/api/papers?${params}`, signal);
  },
  paper: (id: number, signal?: AbortSignal) => jget<Paper>(`/api/papers/${id}`, signal),
  paperByDoi: (doi: string, signal?: AbortSignal) =>
    jget<{ count: number; items: Paper[] }>(`/api/papers/by_doi?doi=${encodeURIComponent(doi)}`, signal),
  pdfUrl: (id: number, page = 1, dpi = 110) => apiUrl(`/api/papers/${id}/pdf?page=${page}&dpi=${dpi}`),
  settings: (signal?: AbortSignal) => jget<Settings>("/api/settings", signal),

  tasks: (q: Record<string, string | number | undefined> = {}, signal?: AbortSignal) => {
    const params = new URLSearchParams();
    Object.entries(q).forEach(([key, value]) => {
      if (value !== undefined && value !== "" && value !== null) params.set(key, String(value));
    });
    return jget<TasksResp>(`/api/tasks?${params}`, signal);
  },
  task: (id: number, signal?: AbortSignal) => jget<Task>(`/api/tasks/${id}`, signal),
  taskRetry: (id: number) => jpost<{ new_task_id: number }>(`/api/tasks/${id}/retry`),

  inbox: (signal?: AbortSignal) => jget<{ items: InboxItem[]; count: number }>("/api/inbox", signal),
  inboxScan: () => jpost<{ queued: { file: string; task_id: number }[]; count: number }>("/api/inbox/scan"),
  inboxUpload: (file: File, signal?: AbortSignal) => {
    const data = new FormData();
    data.append("file", file);
    return jpost<{ path: string; task_id: number }>("/api/inbox/upload", data, signal);
  },
  inboxFromUrl: (url: string, filename?: string) =>
    jpost<{ path: string; size: number; task_id: number }>("/api/inbox/from_url", { url, filename }),

  reparse: (id: number) => jpost<TaskActionResp>(`/api/papers/${id}/reparse`),
  reenrich: (id: number) => jpost<TaskActionResp>(`/api/papers/${id}/reenrich`),
  retranslate: (id: number, force = false) => jpost<TaskActionResp>(`/api/papers/${id}/retranslate`, { force }),
  resummarize: (id: number) => jpost<TaskActionResp>(`/api/papers/${id}/resummarize`),
  reembed: (id: number, force = false) => jpost<TaskActionResp>(`/api/papers/${id}/reembed`, { force }),
  refreshBibtex: (id: number) => jpost<{ bibtex: string; doi: string }>(`/api/papers/${id}/bibtex`),
  pipeline: (id: number, opts?: {
    steps?: string[];
    force_steps?: string[];
    translate_force?: boolean;
    on_error?: "stop" | "continue";
  }) => jpost<TaskActionResp>(`/api/papers/${id}/pipeline`, opts ?? {}),
  translations: (id: number, signal?: AbortSignal) =>
    jget<{ items: TranslationChunk[]; count: number }>(`/api/papers/${id}/translations`, signal),

  doc: (id: number, docType: "en" | "zh" | "summary", signal?: AbortSignal) =>
    jget<DocResp>(`/api/papers/${id}/doc/${docType}`, signal),
  docSave: (id: number, docType: "en" | "zh" | "summary", text: string, baseHash?: string | null) =>
    jput<{
      saved: boolean;
      revision_id: number | null;
      changed: boolean;
      hash: string;
      suggest_retranslate: boolean;
      reembed_task_id: number | null;
    }>(`/api/papers/${id}/doc/${docType}`, {
      text,
      ...(baseHash !== undefined ? { base_hash: baseHash } : {}),
    }),
  revisions: (id: number, docType?: string, signal?: AbortSignal) =>
    jget<{ items: DocRevision[]; count: number }>(
      `/api/papers/${id}/revisions${docType ? `?doc_type=${docType}` : ""}`,
      signal,
    ),
  revision: (revisionId: number, signal?: AbortSignal) =>
    jget<DocRevision>(`/api/revisions/${revisionId}`, signal),
  revisionRestore: (revisionId: number) =>
    jpost<{ restored: boolean; from_revision: number; new_revision_id: number; reembed_task_id: number | null }>(
      `/api/revisions/${revisionId}/restore`,
    ),
  annotations: (id: number, signal?: AbortSignal) =>
    jget<{ items: Annotation[]; count: number }>(`/api/papers/${id}/annotations`, signal),
  annotationCreate: (id: number, body: {
    type: string;
    anchor?: string;
    quote?: string;
    note?: string;
    position?: AnnotationPosition;
  }) => jpost<{ id: number }>(`/api/papers/${id}/annotations`, body),
  annotationDelete: (annotationId: number) => jdelete<{ deleted: boolean }>(`/api/annotations/${annotationId}`),

  semanticSearch: (query: string, topk = 5, signal?: AbortSignal, paperId?: number) =>
    jpost<SemanticSearchResp>("/api/semantic_search", { query, topk, paper_id: paperId }, signal),
  search: (params: {
    source: string;
    query: string;
    limit?: number;
    year_from?: number;
    year_to?: number;
  }, signal?: AbortSignal) => jpost<{ source: string; items: SearchResult[]; count: number; error?: string }>(
    "/api/search",
    params,
    signal,
  ),

  citations: (params: { includeExternal?: boolean; yearFrom?: string; yearTo?: string } = {}, signal?: AbortSignal) => {
    const query = new URLSearchParams({ include_external: params.includeExternal ? "1" : "0" });
    if (params.yearFrom) query.set("year_from", params.yearFrom);
    if (params.yearTo) query.set("year_to", params.yearTo);
    return jget<CitationGraph>(`/api/citations?${query}`, signal);
  },
  citationsRebuild: () => jpost<TaskActionResp>("/api/citations/rebuild"),

  llmUsage: (signal?: AbortSignal) => jget<LLMUsageResp>("/api/llm_usage", signal),
  glossaryList: (signal?: AbortSignal) => jget<{ items: GlossaryEntry[]; count: number }>("/api/glossary", signal),
  glossaryCreate: (entry: Partial<GlossaryEntry>) => jpost<{ id: number }>("/api/glossary", entry),
  glossaryUpdate: (id: number, entry: Partial<GlossaryEntry>) => jput<{ id: number }>(`/api/glossary/${id}`, entry),
  glossaryDelete: (id: number) => jdelete<{ ok: boolean }>(`/api/glossary/${id}`),
};
