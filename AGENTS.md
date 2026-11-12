# papersys 智能体开发规范

> 本文档只面向在本项目中修改代码、运行命令或维护数据的编码智能体，开始工作前必须完整阅读。人类开发者从 [README.md](README.md) 开始；普通使用者和部署人员分别阅读 [docs/guides/user.md](docs/guides/user.md) 与 [docs/guides/deployment.md](docs/guides/deployment.md)。
>
> 公开版（3.11.2-open）相对内部版的三大变更：**移除登录与角色体系（无 OAuth、无会话）**、**单应用容器化部署（Docker Compose，数据统一在 `/data`）**、**MinerU 默认走 Cloud、本地 GPU 为可选组件**。

---

## 🚨 AI 强制遵循条款（红线）

> 本节条款的引用格式为「红线 N」（如「红线 6」）。
> 违反以下任一红线，必须**立即修正**：

1. **不在项目根目录创建源代码文件**（除已有 `config.toml` / `AGENTS.md` / `README.md` / `CHANGELOG.md` / `pyproject.toml` / `Dockerfile` / `compose.yaml` / `compose.mineru.yaml` / `.env.example` / `.dockerignore` / `.gitignore` 外）。Python 代码进 `backend/`，前端进 `frontend/src/`，运维脚本进 `scripts/`；其他文档按 `docs/README.md` 的分类存放
2. **运行数据只写入配置的数据根目录**（源码部署默认为 `config.toml [paths]` 指定的目录，容器部署为 `/data` 卷）。`papers.pdf_path/parsed_path` 只存**相对 `library_root` 的相对路径**
3. **不在源码中硬编码 API token**。所有 secret 通过 `backend/config.py` 的 `CONFIG["secrets"]` 字典读取，底层只读项目根目录的 `.env`
4. **翻译调用必须进程内直调**：worker 内 `from backend.pipeline.translator import translate_chunk` 直接调用；禁止用 shell（`bash -lic`、`timeout` 命令等）包装翻译调用（历史版本因此反复 hang；教训见 [翻译稳定性约束](#翻译稳定性约束) 与 `docs/reference/pitfalls.md`）
5. **未经用户明确许可不得 `git commit`**。`git mv` 而非 `mv` 移动已追踪文件
6. **不让 LLM 写 DOI / arxiv_id**。这两个字段只接受以下来源：PDF 自身携带的标识符（前两页印刷 DOI / arXiv 水印 / PII，正则提取后经 Crossref 校验）/ Crossref·OpenAlex 候选逐条打分的反查（标题+作者+年份+类型多维验证）/ 用户手动录入。LLM 输出的 DOI 一律丢弃（已是历史教训）
7. **路径字段读取统一走 `backend.config.resolve_path()`**：相对路径 -> library 拼接；绝对路径 -> 原样；历史 `legacy:` 前缀 -> 原样拼接旧存储路径。三种形态磁盘缺失时一律返回 None（**降级不抛异常**）
8. **公示层修订判定只通过 `doc_revisions` 表**。编辑器保存时把人工修改写入覆盖层（`translations` 表 `chunk_index=-1` 行，阅读时优先于自动产出内容）+ 修订快照；回滚走快照文件
9. **在 `.py` / `.tsx` 头部 docstring 中禁止写 `作者:` / `日期:` / `版本:` / `Changelog`**。时间归 git history 管，版本归 `pyproject.toml` / `package.json` 管
10. **Python 必须使用项目指定的 Python 3.12 环境**，禁止向系统或 base 环境安装依赖；缺包一律安装到项目虚拟环境，详见 [环境与命令](#环境与命令)
11. **MCP 工具与 Agent 工具的错误约定**：错误一律以 `{"error": ...}` JSON 字符串返回，**永不抛异常**（防 LLM 循环崩溃）

---

## 环境与命令

### Python 环境

**所有 Python 代码必须运行在 Python 3.12 的项目环境中**（建议使用虚拟环境或 conda 环境）。

| 场景 | 正确做法 |
|---|---|
| 执行脚本 / 单元测试 | 激活项目环境后 `PYTHONPATH=. python3 <script>` |
| pip 安装 | 安装到项目虚拟环境，禁止 `pip install --user` 或写入 base 环境 |
| 已知有的包 | flask, flask-cors, python-dotenv, pymupdf, pillow, fastmcp, requests |
| 前端（Node 24.x） | `cd frontend && npm ci`，与 Python 环境无关 |

### 启动与运维

```bash
paper serve --detach    # 后端（Flask + scheduler + worker 池）
paper frontend --detach # 前端（Vite dev server）
paper mcp               # MCP 服务器（stdio；客户端配置 {"mcpServers": {"paper": {"command": "paper", "args": ["mcp"]}}}）
paper mcp --transport http --host H --port P   # HTTP MCP
paper doctor            # 健康检查（存储 + 托管检索 + MinerU HTTP 校验）
paper backup            # DB + library -> tar.gz
paper zotero-export -f ris -o out.ris   # Zotero 导出（linked-file 工作流见输出提示）
paper embed --all       # 全库向量化（语义检索层）
```

容器部署（推荐）：`docker compose up --build`，详见 [docs/guides/deployment.md](docs/guides/deployment.md)。

完整 CLI 参考（tasks / cache / config / inbox 等子命令）见 [README.md](README.md#cli-参考)。

### 日志

`/tmp/flask.log`（Flask stdout/stderr）、`/tmp/vite.log`（Vite）、数据根目录下 `logs/`（Worker 任务级日志）。调试优先 `tail -20 /tmp/flask.log`。容器部署的日志走标准输出，用 `docker compose logs web` 查看。

### 验证命令

```bash
# 前端（首次运行浏览器测试前执行 npm run test:e2e:install）
cd frontend && npx tsc --noEmit && npm run build:verify && npm run lint && npm test && npm run test:e2e

# 后端
PYTHONPATH=. python3 -m pytest backend/tests/ -q
```

### 验收冒烟

```bash
curl http://127.0.0.1:8080/api/health        # 容器部署；源码开发用 http://127.0.0.1:5050/api/health
curl http://127.0.0.1:8080/api/stats
curl -X PUT http://127.0.0.1:8080/api/papers/1/doc/en -H 'Content-Type: application/json' -d '{"text":"test"}'
curl http://127.0.0.1:8080/api/papers/1/revisions
```

### Bash 工具使用注意

- **每个 bash tool call 是独立 shell**：环境变量、`cd`、history 跨调用不保留（harness 既定行为）
- 后台进程跨调用存活；长任务优先用工具的 run_in_background
- 后端启动需 1-2 秒：`start_backend && curl` 这类链式调用要插入 `sleep 2`，或分两次调用
- 进程内冒烟优先用 `create_app().test_client()`（不依赖端口监听）

---

## 项目概览与架构

### 输入 / 处理 / 输出

**输入**：用户投递到 inbox 目录的 PDF（上传/URL 下载）。
**处理**：MinerU 解析 -> OpenAlex/Crossref 补全元数据 -> 向量化（语义检索层）-> LLM 翻译 + 总结（两个互相独立的可选步骤，面向人类阅读）。
**输出**：本地 SQLite 索引 + 自建阅读器（渲染/编辑/批注/修订史）+ Zotero 可选导出 + MCP 工具。

### 总体角色分工

| 组件 | 负责的数据（以此为准） | 谁可写 |
|---|---|---|
| 数据根目录下 `papers.db` | **流水线状态 + 索引 + 修订史** | 后端 Worker / API / MCP |
| 数据根目录下 `library/` | **PDF 归档 + MinerU 解析结果 + 图片** | IngestWorker / ParseWorker |
| 数据根目录下 `inbox/` | **新增 PDF 入口（本地目录）** | 用户 / API |
| 自建阅读器（React） | **公示与人工复核界面（EN/ZH/Summary/批注）** | 用户（浏览器）+ Web API |

容器部署时以上目录全部位于 `/data` 卷。

### 技术要点

- 后端 Flask + Flask-CORS；任务调度 = APScheduler（**仅做清理**）+ 自研 Worker 线程池
- ORM 用原生 `sqlite3` + Row factory，**不引入 SQLAlchemy**；数据库 SQLite (WAL) 3.40+
- PDF 用 PyMuPDF (`fitz`) 1.27+；MCP 用 FastMCP 3.4.7（stdio 与 HTTP 两种传输）
- Python 3.12；Node 24.x
- 前端 React + TypeScript + Vite + Tailwind v3；markdown 渲染链 react-markdown + remark-gfm + remark-math + rehype-raw + rehype-sanitize（白名单）+ rehype-katex；路由 react-router-dom；图标 lucide-react。具体版本以 `frontend/package.json` / `pyproject.toml` 为准

### 总体数据流

```
inbox/ (用户/MCP)
     │
     ▼
[IngestWorker] ──> library/{year}/{dir}/
     │ pdfinfo + 页面标识符抽取 -> Crossref/OpenAlex 逐条打分反查 -> dir_name
     ▼
[ParseWorker] ── MinerU ──> full.md + images/
     ▼
[EnrichWorker]  OpenAlex 三层交叉验证 + 摘要↔解析文本重合校验（低分仅 events 警告，`backend/services/abstract_check.py`）
     ▼
[EmbedWorker] ──> embeddings 表 ──> semantic_search（两阶段）
     ▼
[TranslateWorker] (chunk 级，可选) / [SummaryWorker]（互相独立，都只读解析出的英文原文）
     │
     ▼
translations/summaries 表
     │
     ▼
自建阅读器（DocReader：渲染/编辑/批注/修订史）
     │
     ▼
doc_revisions + annotations 表
```

> embed 位于 enrich 之后、translate 之前：嵌入只依赖 EN 原文
> （parsed full.md / EN 覆盖层），翻译是面向人类阅读的可选步骤，不阻塞语义检索层。

### `status_*` 状态机

| 状态 | 含义 |
|---|---|
| `missing` | 还没做 |
| `processing` | Worker 正在做 |
| `ok` | 已完成 |
| `partial` | 部分成功（翻译：部分 chunk ok；可重试只跑失败块） |
| `error` | 完全失败，可重试 |
| `manual_hold` | 自动流程无法处理，等用户介入 |
| `skipped` | 该步骤对此类记录禁用（SI 附件的总结步骤，summary_worker 标） |

**七个状态列**：`status_pdf` / `status_parsed` / `status_enrich` / `status_translated` / `status_summary` / `status_outline`(遗留) / `status_embedded`

### 任务调度与恢复

- `backend/workers/runner.py` 先检查线程池空位再 `claim_pending`，pending 任务不会在线程池排队时提前变成 running；`started_at` 只在 worker 实际执行时写入。
- 单论文操作通过 `tasks_repo.create_or_reuse_active_task` 在 `BEGIN IMMEDIATE` 事务内复用同论文、同类型、同参数的 pending/running 任务；不同 force 或步骤参数仍新建任务。`GET /api/tasks` 的 `count` 是当前页长度，`total` 是筛选后的总数。
- `recover_orphans` 对 pipeline/parse/enrich/embed/summary 的 running 任务退回 pending（`retries+1`，默认上限 3），其他任务或超限任务标 error；feed pipeline 已关联 paper_id 时跳过 ingest 并继续后续步骤，避免重读已删除的 inbox 文件。MinerU 超时、`database is locked`、损坏 JSON 缓存也会自动重试。损坏缓存文件删除后按未命中处理，本地 MinerU 调用有总时限看门狗。

### 核心数据约束

1. `dir_name` 唯一标识一篇论文（`{author_lowercase}{year}_{keyword}`）。默认不改动；确需纠正时用 `paper db rename` 显式移动目录并更新路径字段，不删行重投
2. `papers.doi` 是次要标识，**可能为空**；只接受 PDF 页面标识符（DOI/PII/arXiv 正则提取+校验）/ Crossref·OpenAlex 逐条打分的反查 / 手动录入。首页前部实际印出的 DOI 在排版垃圾标题下仍采纳并记警告，文件名派生 DOI 仍要求标题匹配
3. 路径字段三态约定（`resolve_path`，见红线 7）
4. `doc_revisions` 的 `content_hash` 判定内容是否变化；快照 `payload_path` 支撑回滚
5. 入库按 DOI 或文件 SHA-256 去重；SI 与母论文同 DOI 时分别入库，`attachment_kind='si'` 且 `parent_paper_id` 指向母论文

### `translations.chunk_index = -1` 行（人工修改覆盖自动产出）

单行同时承载 EN / ZH 两列的"整体覆盖版"。两列**独立**：

| 列 | 写入者 | 语义 |
|---|---|---|
| `en_text` 非空 | reader API（`PUT /doc/en`，source='web_edit'） | 复核版 EN；翻译输入源优先级高于 `parsed_path` |
| `zh_text` 非空 | reader API（`PUT /doc/zh`） | 人工编辑 ZH；阅读优先于自动 chunk |
| `source` | `web_edit` / `manual_edit` | 人工写入标记 |

**关键约束**：
- EN 保存 SQL `ON CONFLICT ... SET en_text`（**不**覆盖 `zh_text`）；ZH 反之
- `translate_orchestrator` force=True 的 DELETE 必须 `WHERE chunk_index >= 0`（保留 -1 行）
- 保存前若内容 hash 变化 -> 记 `doc_revisions`（快照写数据根目录下 `doc_snapshots/`）

---

## 目录结构与规范

### 目录树

```
<仓库根目录>/
├── config.toml             # 配置（路径/限频/LLM 端点 [llm]/托管检索 [retrieval]；公开配置模板）
├── .env.example            # secret 模板（复制为 .env 后填写）
├── Dockerfile / compose.yaml / compose.mineru.yaml  # 单应用容器与 Compose 部署（mineru 为可选本地 GPU 叠加文件）
├── docker/                 # 容器健康检查脚本（healthcheck.py）
├── .agents/skills/         # kimi code 技能（paper-retrieval：paper MCP 检索与断言纪律）
├── AGENTS.md               # 编码智能体规范
├── README.md               # 人类开发者：环境、架构、CLI、API 与测试
├── CHANGELOG.md            # 版本变更记录
├── pyproject.toml          # 打包（[project.scripts] paper = backend.cli:main）
├── docs/
│   ├── README.md           # 文档总索引与维护规则
│   ├── TODO.md             # 当前待办
│   ├── guides/             # 用户与部署手册
│   ├── reference/          # 术语、踩坑与外部 API 资料
│   └── adr/                # 架构决策
├── backend/
│   ├── cli.py              # paper CLI（argparse 命令行入口）
│   ├── app.py              # Flask 入口（create_app + 路由注册）
│   ├── config.py           # 读 config.toml + .env；resolve_path / check_storage
│   ├── mcp_server.py       # FastMCP 服务器（stdio 19 工具 / HTTP 17 工具）
│   ├── services/           # 服务层（与传输协议无关，Flask/MCP 共用）：zotero_export / semantic_search / redaction
│   ├── db/                 # schema.sql / conn / repo / doc_revisions_repo / migrations/
│   ├── api/                # Flask 蓝图（每文件一个蓝图）：papers / preview / reader / search / actions / tasks / ...
│   ├── workers/            # runner（分池调度）+ ingest/parse/enrich/translate/summary/embed/citation/pipeline
│   ├── external/           # openalex / semantic_scholar / arxiv / sciverse / crossref / mineru / llm / hosted_retrieval
│   ├── pipeline/           # pdf_classify / translator（chunk 切分）
│   └── tests/              # pytest
├── frontend/
│   ├── vite.config.ts      # /api -> 127.0.0.1:5050 代理
│   └── src/
│       ├── lib/api.ts      # TS 类型 + fetch 包装 + reader API
│       ├── components/     # MarkdownView / DocReader / PipelineStepper / PaperDetail ...
│       └── pages/          # Library / Tasks / Glossary / Discover / Citations / Settings
└── scripts/                # redrive_papers（错配重投）/ delete_papers / dedup_papers / abstract_audit / mineru_guard / verify_ingest / zotero_rdf
```

### 目录使用规范

| 目录 | AI 权限 | 说明 |
|---|---|---|
| `backend/`, `frontend/src/`, `scripts/` | ✅ 可写 | 主要代码区 |
| `.agents/skills/` | ✅ 可写 | kimi code 技能（`paper-retrieval` 是 paper MCP 使用与断言纪律的单一事实源；工具语义变更须同步更新） |
| `backend/tests/` | ✅ 可写 | 测试 |
| 数据根目录下 `library/{year}/{dir}/` | ✅ Worker 写归档 | PDF + full.md + images/ |
| 数据根目录下 `inbox/` | ✅ 用户/程序写 | 投递入口 |
| 数据根目录下 `doc_snapshots/` | ✅ 程序写 | 修订快照（可定期清理） |
| 项目根目录 | 🚫 不创建源码/配置文件 | 例外见红线 1 |

### 路径规范

| 场景 | 正确做法 |
|---|---|
| 读 PDF/解析文件 | `backend.config.resolve_path(paper["pdf_path"])`（三态见红线 7） |
| 归档写入 | `library_root() / str(year) / dir_name / f"{dir_name}.pdf"`，DB 存**相对路径** |
| 读 secret | `CONFIG["secrets"]["llm_api_key"]` 等 |
| 数据库连接 | `backend.db.conn.get_conn()` / `conn_ctx()` |
| 测试隔离 library | `monkeypatch.setitem(cfg.CONFIG["paths"], "library_root", str(tmp_path))`（防污染真实库） |

### 强制检查清单（提交前自检）

- 新增 Python 文件 -> 在 `backend/` 下？有 `__init__.py`？
- 新增 API 端点 -> 蓝图 + `app.py` 注册 + `/` 根路由 endpoints 列表 + `lib/api.ts` 类型？
- 新增 MCP 工具 -> `mcp_server.py` 加 `@mcp.tool`？错误返回 `{"error": ...}` 而非抛异常？
- 新增 worker -> `tasks` 表持久化 + events emit + `app.py` import 注册 + pipeline STEP_REGISTRY？
- 新增 schema 字段 -> `schema.sql` + `repo.py:PAPER_FIELDS` + migration 脚本（幂等）+ `lib/api.ts` **5 处同步**？
- 翻译相关改动 -> 进程内 import？每 chunk 独立 commit？保留 chunk_index=-1 行？查过 `docs/reference/pitfalls.md` 翻译小节？

---

## 领域规则

### 翻译稳定性约束

> **翻译是历史上出问题最多的环节**。所有翻译相关代码必须遵守：

**一次成功三规则**：
1. **不用 shell timeout 包装**（bash 卡 tcsetattr，进程 hang）
2. **进程内 import**：`from backend.pipeline.translator import translate_chunk`，直接调用
3. **按 chunk 入队**：100K chars = 20-40 个 chunk task，失败只重试这一块

**必须遵守**：
- API key 通过 `CONFIG["secrets"]["llm_api_key"]` 注入，不在 worker 内重读 .env
- 每 chunk 调用 `requests.post(timeout=180)`
- 占位符逻辑沿用 `pipeline/translator.py`；完整性校验只 emit 警告，不修改文件
- 图片 markdown 在翻译前替换为占位符、译后还原（`pipeline/translator.py` 的 IMG 占位符机制）；当前没有「翻译完成后」的图片处理步骤
- DOI / arxiv_id **绝不**让 LLM 产出（红线 6）
- **chunk 字符上限**：超 `max_chunk_chars`（默认 6000）按段落边界二次切分
- **partial 状态**：部分成功标 `partial`（amber 徽章）；重跑只跑失败块
- **dir_name 默认不改动**：year=0 时 ingest 直接 raise，PDF 留 inbox；确需纠正时用 `paper db rename` 显式移动目录并更新路径字段，不删行重投

历史问题回归测试表（占位符碎片 / 反斜杠双重转义 / 参考文献翻译 / LLM 幻觉 DOI）见 `docs/reference/pitfalls.md` 翻译小节。

### 无登录模式与网络安全

公开版没有用户、角色和会话体系。以下安全约束与登录无关，必须始终生效：

- Compose 默认把 Web 发布到 `127.0.0.1:8080`；MCP HTTP 是 profile 为 `mcp` 的可选服务，默认不启动，启用后发布到 `127.0.0.1:5051`。其他机器默认通过 SSH 隧道访问。
- 容器内 Gunicorn 监听 `0.0.0.0` 需要显式 `PAPER_ALLOW_REMOTE=1`；宿主机回环发布仍是实际访问边界。源码直接运行默认只监听回环地址。
- 浏览器跨站写保护独立生效：同源写请求必须成功，跨站写请求必须被拒绝。
- MCP HTTP 传输始终拒绝 `ingest_local_file` 与 `ingest_local_files`，该限制不依赖任何开关。
- `ingest_by_url` 的内网地址拒绝与下载安全约束继续生效。
- 业务表的操作者字段只用于审计；新写入固定为 `system` 或 `NULL`，Worker 不再执行角色检查。

### 向量检索层

「RAG」（检索增强）在本项目里指以下两部分的组合：EmbedWorker 负责建库（切块、嵌入、写 `embeddings` 表），`backend/services/semantic_search.py` 负责检索。

- `embeddings(paper_id, chunk_index UNIQUE, text, vector BLOB float32, dim, model)`
- **EmbedWorker**（kind=embed）：复用 translator chunk 切分；EN override 优先；批量调 HostedRetrievalClient `embed()`；断点续传（重跑跳过已嵌 chunk）；清理 stale 行
- 嵌入模型：`config.toml [retrieval]` 托管端点（Fireworks qwen3-embedding-8b，1024 维，OpenAI 兼容 /embeddings）
- 检索：`services/semantic_search.py` 两阶段——numpy 归一化余弦召回 top-(topk×candidate_multiplier) -> 托管精排（qwen3-reranker-8b，文档按 rerank_max_chars 截断）-> relevance_score 阈值过滤（默认 0.5，已标定；**仅跨库检索过滤**，paper_id 单篇定位不过滤、低于阈值条目标 `low_confidence`）；候选数 <= topk 或 `rerank_enabled=false` 时短路为纯余弦
- 降噪：内容状态列（`status_pdf`/`status_parsed`/`status_translated`/`status_summary`/`status_outline`/`status_embedded`）任一为 manual_hold 的论文不参与语义检索；`status_enrich` 的 manual_hold 是元数据层失败（解析内容不受影响），不触发检索排除，经由 get_paper/status 徽章/stats 暴露待人工处理；跨库结果按 paper_id + 归一化标题双重去重（同标题重复入库只保留最高分一篇）；单篇模式（`paper_id` 参数）保留同论文多片段——MCP 工具层已暴露 `paper_id` 与 `include_context`
- 入口：`POST /api/semantic_search` / MCP `semantic_search` / `paper embed`（CLI 入队）；结果带 `char_start` / `char_end`（full.md 字符区间），可直接传给 `read_fulltext`；`search_fulltext` 提供篇内字面检索

### 自建公示层

阅读器文档（EN 原文 / ZH 译文 / 总结）的展示、编辑、批注与修订史全部落在本地 SQLite。

**数据结构**：
- `doc_revisions(paper_id, doc_type ∈ {en,zh,summary}, content_hash, source ∈ {auto,web_edit,agent}, size_bytes, payload_path, ts)`
- `annotations(paper_id, type ∈ {highlight,callout,comment}, anchor=doc_type, payload_json{quote,note,position}, source ∈ {web,agent})`

**API**：端点清单见 [README.md](README.md#论文与阅读器)。README 未含的行为规则：
- 保存编辑写覆盖层 + 修订史，**hash 变化才记**（快照写数据根目录下 `doc_snapshots/`）
- Web 保存携带 `base_hash`；服务器内容已变化时返回 409，摘要检查与正文写入在同一个 `BEGIN IMMEDIATE` 事务中完成，旧调用方不传该字段时保持兼容
- EN 保存且此前已嵌向量时自动入队 force re-embed（`status_embedded` 置 missing，任务合并去重）
- 新建批注 highlight 类型必须带 quote；Web 同时保存 `position={start,end}`（渲染正文字符区间），旧批注缺位置时按 quote 首次出现位置定位
- EN 覆盖层优先级覆盖全部读取方：`GET /doc/en`、`GET /preview?type=en`、MCP `read_fulltext`、翻译输入、嵌入输入、总结输入都先读 `chunk_index=-1` 的 `en_text`（单一实现 `backend/services/doc_text.py`）

**前端（DocReader 三视图）**：
- **渲染**：react-markdown + remark-gfm + remark-math + rehype-raw + rehype-sanitize（白名单）+ rehype-katex；图片 `images/x` 重写为 `/api/papers/<id>/images/x`；MinerU `<details><summary>` 块分段解析，块内管道表格仍成为真实表格。公式内 `<`/`>` **不做**实体转义，原文直传（`lib/markdown-pre.ts` 不恢复；完整记录见 `docs/reference/pitfalls.md` 渲染小节）
- **原始**：纯文本 markdown（调试 chunk 边界）；有草稿时显示草稿并明确标记，不冒充已保存内容
- **编辑**：`PaperDetail` 按 `paperId + docType` 保存草稿，页签切换不清空；路由离开与抽屉关闭需确认。保存只替换已提交文本，保存期间的新输入继续保留；冲突时由用户选择重新读取或明确覆盖
- 批注在正文显示高亮并可从列表定位；修订先预览快照再确认回滚。EN 保存后提示"重翻译将使用复核版"（amber 提示，不自动触发）

### MCP 服务器

`backend/mcp_server.py` 使用 FastMCP 3.4.7，`paper mcp` 默认启动 stdio（19 个工具），也支持 HTTP 传输（17 个工具，不注册 `ingest_local_file` / `ingest_local_files`）。工具清单与签名见 [README.md](README.md#mcp)。

会话侧使用纪律（检索顺序、断言分级、任务书模板）固化在 [.agents/skills/paper-retrieval/SKILL.md](.agents/skills/paper-retrieval/SKILL.md)：本仓库会话经项目作用域自动加载，其他工作区经 `~/.kimi-code/config.toml` 顶层的 `extra_skill_dirs` 加载，安装步骤见 [README.md](README.md#kimi-code-技能安装)。

**约定**：所有工具返回 JSON 字符串；错误 `{"error": ...}` 永不抛异常（红线 11）；长文本截断（abstract 800 / fulltext 分页，单次上限 50K 字符，toc 仅随 offset=0 首页返回）。

### 外部依赖与限频约束

> 限频参数集中在 `config.toml [external]`，client 实现时**强制读取**

| 服务 | 限频 | 用途 | 客户端文件 |
|---|---|---|---|
| **OpenAlex** | 5 req/s | 元数据富化、引文网络 | `backend/external/openalex.py` |
| **Semantic Scholar** | **1 req/s 严格** | 引用关系 | `backend/external/semantic_scholar.py` |
| **Sciverse (REST)** | 60 req/min | 语义检索 | `backend/external/sciverse.py` |
| **arXiv** | ~1 req / 3s | 关键词搜索 | `backend/external/arxiv.py` |
| **Crossref** | polite pool 50 req/s | DOI 反查 + BibTeX | `backend/external/crossref.py` |
| **Europe PMC** | 3 req/s（自我限速） | abstract 第三源（OpenAlex/Crossref 对 Springer Nature/Cell 系期刊无摘要，Europe PMC=PubMed 覆盖） | `backend/external/europepmc.py` |
| **MinerU** | 约 5 任务/min（自我限速） | PDF 解析（cloud 或 local） | `backend/external/mineru.py` |
| **LLM chat** | 模型相关 | 翻译/总结（OpenAI 兼容端点，config.toml [llm] 可换提供方） | `backend/external/llm.py` |
| **托管检索** | 模型相关 | 嵌入 qwen3-embedding-8b / 精排 qwen3-reranker-8b（向量化 + 两阶段检索；独立实现，流程设计参考 BrainPilot） | `backend/external/hosted_retrieval.py` |

**通用 client 约束**：
- 继承 `backend/external/base.py:RateLimitedClient`（MinerU/LLM 例外，见各文件头注释）
- 磁盘缓存：数据根目录下 `cache/<service>/<sha1>.json`，TTL 由 config 决定
- 限频跨线程且跨进程：`cache/.ratelimit/<service>.json` 记录上次请求时刻，`flock` 互斥串行
- 429 / 5xx -> 指数退避（`min(60, 2**retry)`），重试 3 次后 raise；429 耗尽的报错带「safe to retry later」
- 请求 `timeout=30`（LLM 180 / embed 60）
- 每个 Worker 进程独立 `requests.Session()`

外部 API 文档索引见 `docs/reference/external-api-index.md`（部分过期，以 client 代码与 `config.toml` 为准）。

---

## 扩展模板

新增能力时遵循以下模板：

### 新增 Worker 模板

1. `backend/workers/<kind>_worker.py`，`@register_worker("<kind>")` 装饰 `(ctx: WorkerContext, payload: dict) -> None`
2. `app.py` 顶部 import 触发注册；长任务每 30s 心跳
3. task 完成由 scheduler 自动标 ok；抛异常自动标 error
4. 断点续传：每个子单元独立 commit，重跑跳过已 ok 项
5. **不调用 shell / 不创建子进程**；外部 API 走 `backend/external/`

### 新增 API 端点模板

1. `backend/api/<module>.py` 顶部 `bp = Blueprint(...)`，用 `@bp.get/post/put/delete` 装饰器
2. `app.py` 注册 + `/` 根路由 endpoints 列表同步
3. 错误统一 `abort(4xx, "msg")`（未单独处理的错误由全局 JSON 错误处理器统一返回）

### 数据库 schema 演化

- 小改动：`schema.sql` + 幂等 migration（`backend/db/migrations/2026XXXX_<topic>.py`，pragma 检查后 ALTER）
- 5 处同步：`schema.sql` / `repo.py:PAPER_FIELDS` / `list_papers` / API 返回 / `lib/api.ts`

---

## 环境速查与踩坑索引

> **改 ingest/DOI 匹配、翻译、渲染、并发相关代码前，先读 `docs/reference/pitfalls.md` 对应小节**（历史问题全表按模块分组在那里）。

| 错误 | 原因 | 解决 |
|---|---|---|
| Vite 502 Bad Gateway | 后端进程死 | `paper serve --detach` 重启 |
| `sqlite3.OperationalError: no such table` | 数据库未跑 init_db | `PYTHONPATH=. python3 -c 'from backend.db.conn import init_db; init_db()'` |
| `no such column: bibtex/status_embedded` | 旧库缺列 | 跑对应 migration（幂等）或 init_db |
| verbatimModuleSyntax 报 type import 错 | TS5 严格模式 | 类型用 `import type {...}` |
| PDF 预览 404 但元数据正常 | 历史 `legacy:` 路径未迁移 | 预期降级；`resolve_path` 返回 None，前端不抛异常 |

---

## 工作标准

| 规范 | 要求 |
|---|---|
| 文件管理 | AI 脚本进 `backend/` / `frontend/src/`；移动用 `git mv`；提交需用户许可 |
| 版本 | `pyproject.toml` / `frontend/package.json` 维护；不在 docstring 写版本号 |
| Secret | `.env`（不入 git）；通过 `CONFIG["secrets"]` 访问；前端/MCP 输出绝不接触 secret |
| Git 提交 | 用户许可后；message 中文/英文均可 |
| 数据缺失 | 流水线 fail-fast + emit event；前端徽章降级 missing/manual_hold 不抛异常 |
| TypeScript | `strict` + `noUnusedLocals`；`baseUrl` + `@/*` 别名 |
| Python | 类型注解非强制；函数签名建议加 |
| 缩进 | Python 4 空格；TS 2 空格 |
| 字符串引号 | Python 双引号；TS 双引号 |

---

## 文档索引

| 文档 | 读者与用途 |
|---|---|
| [README.md](README.md) | 人类开发者：快速上手、架构、CLI、API、MCP、数据模型与测试 |
| [docs/README.md](docs/README.md) | 文档总索引、分类与维护规则 |
| [docs/guides/user.md](docs/guides/user.md) | 使用者：Web、任务与 MCP 使用 |
| [docs/guides/deployment.md](docs/guides/deployment.md) | 部署人员：容器安装、配置、运行、升级与恢复 |
| [docs/reference/context.md](docs/reference/context.md) | CLI 与任务术语 |
| [CHANGELOG.md](CHANGELOG.md) | 版本变更记录 |
| [AGENTS.md](AGENTS.md)（本文） | 编码智能体操作规范 |
| [docs/reference/pitfalls.md](docs/reference/pitfalls.md) | 历史踩坑记录，改对应模块前先读 |
| [docs/TODO.md](docs/TODO.md) | 待测试与待实现需求清单 |
| [docs/reference/external-api-index.md](docs/reference/external-api-index.md) | 外部 API 文档索引，实际行为以 client 代码与配置为准 |
| [config.toml](config.toml) | 非 secret 配置（公开模板） |
| [BrainPilot](https://github.com/NeuroAIHub/BrainPilot) | 托管两阶段检索的流程设计参考（本项目为独立实现） |
| [MinerU](https://github.com/opendatalab/MinerU) | PDF 解析服务（Cloud API 或本地部署） |
