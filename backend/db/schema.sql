-- 文献调研系统 3.0 SQLite Schema
-- 3.0 变更（相对 2.0）：
--   * 路径字段存储约定：pdf_path/parsed_path 存相对 library_root 的相对路径；
--     旧存储绝对路径以 "legacy:" 前缀标记（见 backend/db/migrations/20260901_localize_paths.py）
--   * outline_revisions 表停止写入（Outline 集成移除）；annotations.source 默认 'web'
--   * translations.source 枚举：auto|manual_edit|manual（原 outline_edit 更名）

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ── 论文主表 ───────────────────────────────────────────
CREATE TABLE IF NOT EXISTS papers (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    dir_name                TEXT NOT NULL UNIQUE,
    year                    INTEGER NOT NULL,
    first_author            TEXT NOT NULL,
    keyword                 TEXT,

    title                   TEXT,
    authors                 TEXT,    -- JSON array (legacy)
    journal                 TEXT,
    doi                     TEXT,
    arxiv_id                TEXT,
    pmid                    TEXT,
    openalex_id             TEXT,
    url                     TEXT,    -- 源文档链接（OpenReview 论坛页等；手动录入 / enrich 自 OpenAlex landing_page_url 带回，不参与反查

    abstract                TEXT,
    cited_by_count          INTEGER,
    oa_status               TEXT,
    pub_type                TEXT,
    concepts                TEXT,    -- JSON
    keywords_json           TEXT,    -- JSON
    bibtex                  TEXT,    -- BibTeX 引用条目（Crossref content negotiation 获取）
    attachment_kind         TEXT,    -- 附件标记：NULL=正文；'si'=SI 附件（supporting/supplementary information 等随正文提交的补充材料文件）
    parent_paper_id         INTEGER REFERENCES papers(id),  -- SI 附件对应的母论文；正文为 NULL

    -- 文件路径（相对 library_root；legacy: 前缀 = 旧存储归档待迁移）
    pdf_path                TEXT,
    parsed_path             TEXT,
    summary_path            TEXT,
    pdf_pages               INTEGER,
    pdf_size_bytes          INTEGER,
    parsed_chars            INTEGER,
    mineru_model            TEXT,

-- 流水线状态机
    -- missing | processing | ok | partial | error | manual_hold

    status_pdf              TEXT DEFAULT 'missing',
    status_parsed           TEXT DEFAULT 'missing',
    status_enrich           TEXT DEFAULT 'missing',
    status_translated       TEXT DEFAULT 'missing',
    status_summary          TEXT DEFAULT 'missing',
    status_outline          TEXT DEFAULT 'missing',
    status_embedded         TEXT DEFAULT 'missing',

    -- Outline 文档 ID（容器 + 三个子文档）
    outline_container_id    TEXT,
    outline_en_id           TEXT,
    outline_zh_id           TEXT,
    outline_summary_id      TEXT,
    outline_url             TEXT,

    -- PDF metadata 交叉验证
    pdf_title               TEXT,
    pdf_authors             TEXT,
    created_by_user_id      INTEGER,

    created_at              TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at              TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_papers_year         ON papers(year);
CREATE INDEX IF NOT EXISTS idx_papers_first_author ON papers(first_author);
CREATE INDEX IF NOT EXISTS idx_papers_doi          ON papers(doi);
CREATE INDEX IF NOT EXISTS idx_papers_status_pdf   ON papers(status_pdf);
CREATE INDEX IF NOT EXISTS idx_papers_status_parsed ON papers(status_parsed);

-- ── 全文搜索索引（FTS5）──────────────────────────────────
-- 独立 FTS5 表，存储 title / abstract / parsed_text 的索引副本。
-- 由 parse_worker / enrich_worker / ingest_worker 手动维护（调用 repo.update_fts）。
CREATE VIRTUAL TABLE IF NOT EXISTS papers_fts USING fts5(
    paper_id UNINDEXED,
    title,
    abstract,
    parsed_text
);

-- ── 作者关系 ───────────────────────────────────────────
CREATE TABLE IF NOT EXISTS authors (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    full_name           TEXT NOT NULL,
    normalized_name     TEXT NOT NULL UNIQUE,
    orcid               TEXT,
    openalex_id         TEXT
);

CREATE TABLE IF NOT EXISTS paper_authors (
    paper_id            INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    author_id           INTEGER NOT NULL REFERENCES authors(id) ON DELETE CASCADE,
    position            INTEGER NOT NULL,
    is_corresponding    BOOLEAN DEFAULT 0,
    PRIMARY KEY (paper_id, author_id)
);

-- ── 翻译（按 chunk 存）─────────────────────────────────
CREATE TABLE IF NOT EXISTS translations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_id        INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    chunk_index     INTEGER NOT NULL,
    en_text         TEXT,
    zh_text         TEXT,
    status          TEXT DEFAULT 'pending',   -- pending|ok|error|skipped
    source          TEXT DEFAULT 'auto',      -- auto|manual_edit|manual
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (paper_id, chunk_index)
);

-- ── 总结 ───────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS summaries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_id        INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    content_md      TEXT,
    version         INTEGER DEFAULT 1,
    source          TEXT DEFAULT 'auto',      -- auto|outline_edit|manual
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_summaries_paper ON summaries(paper_id);

-- ── 注释 / 高亮（阅读器划词批注 + Agent 写入）────────────
CREATE TABLE IF NOT EXISTS annotations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_id        INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    type            TEXT NOT NULL,            -- highlight|callout|comment|edit
    anchor          TEXT,                     -- 锚点描述
    payload_json    TEXT,
    source          TEXT DEFAULT 'web',
    created_by_user_id INTEGER,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_annotations_paper ON annotations(paper_id);

-- ── 热词库 ─────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS glossary (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    en          TEXT NOT NULL,
    context     TEXT,
    zh          TEXT NOT NULL,
    notes       TEXT,
    updated_by_user_id INTEGER,
    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ── 引文网络 ───────────────────────────────────────────
CREATE TABLE IF NOT EXISTS citations (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    src_paper_id        INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    dst_paper_id        INTEGER REFERENCES papers(id) ON DELETE SET NULL,
    dst_openalex_id     TEXT,                  -- 库外节点
    dst_title           TEXT,
    dst_year            INTEGER,
    edge_type           TEXT DEFAULT 'references'
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_citations
    ON citations(src_paper_id, IFNULL(dst_paper_id, 0), IFNULL(dst_openalex_id, ''));
CREATE INDEX IF NOT EXISTS idx_citations_src ON citations(src_paper_id);
CREATE INDEX IF NOT EXISTS idx_citations_dst ON citations(dst_paper_id);

-- ── 任务/事件 ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS tasks (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kind            TEXT NOT NULL,
    payload_json    TEXT,
    status          TEXT DEFAULT 'pending',   -- pending|running|ok|error|cancelled
    progress        REAL DEFAULT 0.0,
    error           TEXT,
    retries         INTEGER DEFAULT 0,
    paper_id        INTEGER REFERENCES papers(id) ON DELETE SET NULL,
    log_path        TEXT,
    created_by_user_id INTEGER,
    execution_subject TEXT NOT NULL DEFAULT 'system',
    required_permission TEXT,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    started_at      TIMESTAMP,
    finished_at     TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_kind   ON tasks(kind);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
    paper_id    INTEGER REFERENCES papers(id) ON DELETE SET NULL,
    ts          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    level       TEXT DEFAULT 'info',          -- info|warn|error
    msg         TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id);

-- ── LLM 调用明细（翻译/总结；2026-10-17 起，LLMClient 每次调用落一行）────
-- provider 返回的 usage（prompt/completion tokens）解析入库；失败调用也记录
-- （ok=0 + error），用于排查 401/限流。字符数恒有，token 数视提供方可能为 NULL。
CREATE TABLE IF NOT EXISTS llm_calls (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    kind              TEXT,                    -- summary|translate（调用方注入 call_meta）
    paper_id          INTEGER REFERENCES papers(id) ON DELETE SET NULL,
    task_id           INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
    model             TEXT,
    prompt_chars      INTEGER,
    completion_chars  INTEGER,
    prompt_tokens     INTEGER,
    completion_tokens INTEGER,
    cached_tokens     INTEGER,                 -- provider prompt cache 命中（DeepSeek 系 prompt_cache_hit_tokens）；为 NULL 表示提供方不回该字段
    latency_ms        INTEGER,
    ok                BOOLEAN DEFAULT 1,
    error             TEXT
);
CREATE INDEX IF NOT EXISTS idx_llm_calls_ts ON llm_calls(ts);
CREATE INDEX IF NOT EXISTS idx_llm_calls_paper ON llm_calls(paper_id);

-- ── 向量索引（P4 RAG：EmbedWorker 写入，语义检索消费）────
-- vector 存 float32 数组的 BLOB；量级（数百篇 × ~100 chunks）SQLite + numpy 暴力余弦足够。
CREATE TABLE IF NOT EXISTS embeddings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_id        INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    chunk_id        TEXT NOT NULL,             -- '{paper_id}:{chunk_index}'
    chunk_index     INTEGER NOT NULL,
    text            TEXT NOT NULL,
    vector          BLOB NOT NULL,
    dim             INTEGER NOT NULL,
    model           TEXT,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (paper_id, chunk_index)
);
CREATE INDEX IF NOT EXISTS idx_embeddings_paper ON embeddings(paper_id);

-- ── 文档修订史（自建公示层：编辑器每次保存记录 hash + 快照）────
-- 接替 2.0 的 outline_revisions（已停用，保留历史数据）。
-- doc_type: en|zh|summary；source: auto(流水线产出) | web_edit(阅读器编辑) | agent
CREATE TABLE IF NOT EXISTS doc_revisions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_id        INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    doc_type        TEXT NOT NULL,            -- en|zh|summary
    content_hash    TEXT NOT NULL,            -- md5(内容)
    source          TEXT NOT NULL,            -- auto|web_edit|agent
    size_bytes      INTEGER,
    payload_path    TEXT,                     -- 完整内容快照（回滚用）
    created_by_user_id INTEGER,
    ts              TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_doc_revisions_paper ON doc_revisions(paper_id, doc_type);

-- ── Outline 同步日志（3.0 起停用；保留历史数据供回溯）────
CREATE TABLE IF NOT EXISTS outline_revisions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_id        INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    doc_type        TEXT NOT NULL,            -- container|en|zh|summary
    outline_doc_id  TEXT NOT NULL,
    pushed_hash     TEXT,                     -- 本地推上去的 md5
    outline_hash    TEXT,                     -- 拉回时见到的 md5
    source          TEXT NOT NULL,            -- local|outline
    payload_path    TEXT,                     -- 完整内容快照（便于回滚）
    ts              TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_outline_rev_paper ON outline_revisions(paper_id);

-- ── 标签（沿用旧表）─────────────────────────────────────
CREATE TABLE IF NOT EXISTS tags (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    name    TEXT NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS paper_tags (
    paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
    tag_id   INTEGER NOT NULL REFERENCES tags(id)   ON DELETE CASCADE,
    PRIMARY KEY (paper_id, tag_id)
);

-- ── 扫描日志 ───────────────────────────────────────────
CREATE TABLE IF NOT EXISTS scan_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    scanned_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    total       INTEGER NOT NULL,
    added       INTEGER NOT NULL,
    updated     INTEGER NOT NULL,
    errors      INTEGER NOT NULL,
    details     TEXT
);

-- 注：3.11.2-open 起不再创建 users / user_roles / auth_sessions / mcp_grants /
-- auth_audit 等认证专用表（无登录体系）。旧库中已存在的这些表保留原样，
-- 业务代码不读取、不写入、不删除。业务表的操作者字段
-- （created_by_user_id / updated_by_user_id / execution_subject /
-- required_permission）保留，新写入固定为 NULL / 'system'。
