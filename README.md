# papersys 开发者文档

papersys 是一个本地数据驱动的文献管理系统。它把 PDF 归档、MinerU 解析、元数据补全、语义检索、翻译与总结、人工复核和 MCP 访问放在同一套 Flask、SQLite 与 React 架构中。

本文面向开发者，介绍快速上手、代码结构、CLI、HTTP API、MCP、数据模型和验证方法。版本号以 [pyproject.toml](pyproject.toml) 与 [frontend/package.json](frontend/package.json) 为准。

公开版没有登录、角色和会话体系：所有能访问服务的人共享同一个文献库，访问边界由网络位置决定（默认只监听回环地址）。

## 文档导航

| 文档 | 读者与用途 |
|---|---|
| [docs/README.md](docs/README.md) | 文档总索引 |
| [docs/guides/user.md](docs/guides/user.md) | 使用者：Web 阅读与复核、任务和 MCP 使用 |
| [docs/guides/deployment.md](docs/guides/deployment.md) | 部署人员：容器安装、配置、运行、升级与恢复 |
| [AGENTS.md](AGENTS.md) | 编码智能体：强制约束、代码修改规范和项目内部约定 |
| [docs/reference/context.md](docs/reference/context.md) | 开发者与智能体：CLI 和任务术语 |
| [docs/reference/pitfalls.md](docs/reference/pitfalls.md) | 开发者与智能体：ingest、DOI、翻译、渲染和并发的历史问题 |
| [docs/reference/external-api-index.md](docs/reference/external-api-index.md) | 外部 API 文档索引；实际行为以 client 代码和配置为准 |

## 快速上手（Docker Compose）

前置条件：安装 Docker 与 Compose 插件（`docker compose version` 可用）。PDF 解析默认使用 MinerU Cloud，需要一枚 MinerU API token；翻译、总结和托管检索各需要对应服务的 API key。

```bash
# 1. 获取源码后进入仓库根目录，然后复制密钥模板并限制权限
cp -n .env.example .env
chmod 600 .env

# 2. 编辑 .env，至少填写 MINERU_TOKEN、LLM_API_KEY 与 FIREWORKS_API_KEY
#    （各变量的含义见下文「配置」一节）

# 3. 构建并启动
docker compose up --build
```

启动后：

- Web 界面：`http://127.0.0.1:8080/`
- HTTP API 与健康检查：`http://127.0.0.1:8080/api/health`
- MCP HTTP（可选，默认不启动；`docker compose --profile mcp up -d` 启用）：`http://127.0.0.1:5051/mcp`

Compose 默认把 Web 发布到 `127.0.0.1:8080`；MCP HTTP 是 profile 为 `mcp` 的可选服务，启用后发布到 `127.0.0.1:5051`。两者都只有本机能访问，从其他机器访问请使用 SSH 隧道：

```bash
ssh -L 8080:127.0.0.1:8080 -L 5051:127.0.0.1:5051 <部署主机>
```

所有运行数据（SQLite 数据库、PDF 文库、MinerU 解析结果、缓存、日志、inbox 和修订快照）都保存在 Compose 命名卷挂载的 `/data` 下。升级镜像不会丢失数据；`docker compose down` 不会删除命名卷。

应用容器以非 root 用户运行，不挂载 Docker socket，不包含 MinerU 模型或 GPU 运行时。MinerU 默认走 Cloud API；本地 GPU 部署是可选组件，见 [docs/guides/deployment.md](docs/guides/deployment.md)。

## 系统组成

系统提供三种入口：

- `paper` CLI：服务管理、诊断、任务查询、批量入队、备份和导出。CLI 不直接执行 worker handler。
- Flask HTTP API：前端与脚本使用的论文、阅读器、任务、检索和管理接口。
- MCP：本地 stdio 与 HTTP 两种传输；stdio 19 个工具，HTTP 17 个工具（HTTP 不注册 `ingest_local_file` / `ingest_local_files`）。

主要数据流：

```text
<inbox 投递目录>/
        │
        ▼
IngestWorker ──> <library_root>/{year}/{dir_name}/（PDF 与相对路径）
        │
        ▼
ParseWorker ── MinerU ──> full.md + images/
        │
        ▼
EnrichWorker ── OpenAlex/Crossref/Europe PMC ──> 元数据与摘要检查
        │
        ▼
EmbedWorker ──> embeddings 表 ──> 余弦召回 + 托管精排
        │
        ├── TranslateWorker（可选，按 chunk 独立提交）
        └── SummaryWorker（可选，与翻译相互独立）
        │
        ▼
React DocReader ──> 人工覆盖层、doc_revisions、annotations
```

翻译和总结都只读取英文原文或人工 EN 覆盖层，不相互依赖。默认新 PDF 流程为 `ingest,parse,enrich,embed,summary`，不包含翻译；调用方可以显式指定步骤。

任务由 Flask 内的 scheduler 和线程池执行，不是独立 worker 进程池。APScheduler 只承担清理工作。SQLite 使用 WAL 和原生 `sqlite3`，项目不使用 SQLAlchemy。

## 配置

配置分为两部分：

- `config.toml`：路径、端口、模型、限频和检索的非 secret 配置；仓库根目录的 `config.toml` 就是公开配置模板。
- `.env`：API key 与服务密钥，经 `backend/config.py` 映射到 `CONFIG["secrets"]`。`.env` 不进入镜像，也不提交到版本库。

主要配置段：

| 配置段 | 内容 |
|---|---|
| `[paths]` | SQLite、library、inbox、缓存和日志目录；容器内统一指向 `/data` 子目录 |
| `[server]` | host、port、debug、CORS、CLI health_url；`PAPER_BIND` 可覆盖 bind |
| `[workers]` | 各任务池并发、重试次数和 chunk 字符上限 |
| `[external]` | 外部 API 限频与缓存 TTL |
| `[mineru]` | cloud/local 模式、端点、模型、超时和 `local_container` 容器名 |
| `[llm]` | OpenAI Chat Completions 兼容端点、模型、温度和输入上限 |
| `[retrieval]` | 托管嵌入、精排、候选倍数、阈值和超时 |

常用 secret 名称：

| 名称 | 用途 |
|---|---|
| `MINERU_TOKEN` | MinerU Cloud 解析；local 模式不需要 |
| `LLM_API_KEY` | 翻译与总结 |
| `FIREWORKS_API_KEY` | 托管嵌入和精排 |
| `BP_EMBED_API_KEY` / `BP_RERANK_API_KEY` / `ARK_API_KEY` | 检索密钥覆盖或回退 |
| `SCIVERSE_API_TOKEN` | Sciverse 检索 |
| `SEMANTIC_SCHOLAR_API_KEY` / `OPENALEX_API_KEY` | 可选的外部 API 提额 |

CLI 健康地址按显式 host/port、`PAPER_HEALTH_URL`、`[server].health_url`、`[server].bind`、host:port 的顺序确定。

容器内 Gunicorn 监听 `0.0.0.0`，由 `PAPER_ALLOW_REMOTE=1` 显式允许；宿主机回环发布（`127.0.0.1:8080`）仍是实际访问边界。源码直接运行时默认只监听 `127.0.0.1`。

## 本地开发

### 环境要求

- Python 3.12（建议使用虚拟环境或 conda 环境）。
- Node.js 24.x，前端依赖由 `frontend/package-lock.json` 固定。
- MinerU Cloud API token，或本地 MinerU Docker 服务。
- 翻译、总结和托管检索所需的外部 API 配置。

### 安装

```bash
pip install -e .
cd frontend && npm ci
```

没有 `.env` 时从模板创建，并限制权限：

```bash
cp -n .env.example .env
chmod 600 .env
```

密钥不得写入源码、文档、前端或 MCP 返回。

### 启动开发服务

```bash
paper serve --detach
paper frontend --detach
```

开发前端为 `http://127.0.0.1:5173/`，Vite 将 `/api` 代理到 `http://127.0.0.1:5050`。

常用诊断：

```bash
paper doctor
paper routes
paper serve status
paper serve logs
```

进程内 HTTP 冒烟可以使用 `create_app().test_client()`，避免依赖端口监听。

## CLI 参考

运行 `paper --help` 或对应子命令的 `--help` 可以查看当前参数。以下为命令分组。

### 服务与诊断

| 命令 | 用途 |
|---|---|
| `paper serve [--detach] [--host H] [--port P]` | 启动 Flask、scheduler 和任务线程池 |
| `paper serve stop / status / logs [-f]` | 开发进程管理 |
| `paper frontend [--detach]` 及 `stop / status / logs` | Vite 开发服务器管理 |
| `paper mcp` | stdio MCP |
| `paper mcp --transport http --host H --port P` | HTTP MCP |
| `paper doctor` | 数据库结构、存储、配置、检索、MinerU HTTP 和健康地址检查 |
| `paper version / routes / test` | 版本、路由和 pytest 入口 |
| `paper config show / check` | 配置检查 |

### PDF 与流水线

入队命令默认检查 Flask 健康状态；`--force` 只跳过检查，不会独立执行任务，可能留下无人领取的 pending 任务。

| 命令 | 用途 |
|---|---|
| `paper parse / enrich / summarize <id... \| --all>` | 入队单个处理步骤 |
| `paper translate <id...> [--retranslate]` | 入队翻译；强制重译保留人工覆盖层 |
| `paper embed <id...> [--reembed]` | 入队向量化 |
| `paper pipeline <id...> [--steps ...]` | 处理已在库中的论文 |
| `paper feed <file> / --url <url>` | 投递新 PDF 并执行 pipeline |
| `paper inbox list / scan / add` | 管理本地投递入口 |
| `paper citation rebuild [--paper-id N]` | 重建引文关系 |
| `paper search "query" [--source S]` | 多源检索，不写库 |

`pipeline` 与 `feed` 都创建 `kind=pipeline` 任务。`pipeline_worker` 在一个任务内顺序调用各 handler，不派生子任务。默认已有论文流程与新 PDF 流程的步骤不同，以 `paper pipeline --help` 和 `paper feed --help` 为准。

### 任务、数据库与导出

| 命令 | 用途 |
|---|---|
| `paper tasks list / show / retry / logs / recover` | 查询、重试和恢复任务 |
| `paper cache clear / prune / stats` | 管理外部 API 磁盘缓存 |
| `paper db init / path / stats / shell` | 初始化或查询 SQLite |
| `paper db backfill_fts / backfill_bibtex` | 重建检索索引或 BibTeX |
| `paper db rename <id> --dir-name <name> [--year Y]` | 显式移动论文目录并更新关联字段 |
| `paper db audit-citations [--fix]` | 核对本地被引数与 OpenAlex |
| `paper backup [-o FILE]` | SQLite 一致性快照、library、修订快照和报告打包 |
| `paper zotero-export -f ris\|bibtex -o FILE` | Zotero 导出 |

`paper db init` 只执行 `CREATE TABLE IF NOT EXISTS`，不会给旧表补列；旧库缺列时运行 `backend/db/migrations/` 下对应的幂等迁移。

## HTTP API

开发地址默认为 `http://127.0.0.1:5050`，容器部署为 `http://127.0.0.1:8080`。蓝图位于 `backend/api/`。错误响应包含 `error`；HTTP 异常通常带数值 `code`。

浏览器跨站写保护始终生效：同源写请求成功，跨站写请求被拒绝，与是否有登录无关。

### 论文与阅读器

| 方法与路径 | 用途 |
|---|---|
| `GET /api/papers` | 关键字、作者、全文、年份、状态筛选与分页 |
| `GET /api/papers/<id>` / `GET /api/papers/by_doi?doi=` | 论文详情 / DOI 精确查询 |
| `GET /api/stats` | 文库统计 |
| `GET /api/papers/<id>/pdf?page=N&dpi=D` | PDF 单页 PNG |
| `GET /api/papers/<id>/preview?type=en\|zh\|summary` | 兼容的分页预览 |
| `GET /api/papers/<id>/images/<name>` | MinerU 图片 |
| `GET /api/papers/<id>/translations` | chunk 级翻译状态 |
| `GET /api/papers/<id>/doc/<en\|zh\|summary>` | 阅读全文，人工覆盖层优先 |
| `PUT /api/papers/<id>/doc/<en\|zh\|summary>` | 保存编辑，支持 `base_hash` 冲突检查 |
| `GET /api/papers/<id>/revisions` / `GET /api/revisions/<id>` | 修订列表与快照 |
| `POST /api/revisions/<id>/restore` | 回滚并追加新修订 |
| `GET/POST /api/papers/<id>/annotations` | 批注列表与创建 |
| `DELETE /api/annotations/<id>` | 删除批注 |

`translations.chunk_index=-1` 行同时承载 EN 和 ZH 人工覆盖文本，两列独立更新。翻译、嵌入、总结和所有读取方都优先使用 EN 覆盖层。强制翻译只删除 `chunk_index>=0` 的自动块。

### 检索、任务与操作

| 方法与路径 | 用途 |
|---|---|
| `POST /api/search` | 外部多源检索 |
| `POST /api/semantic_search` | 库内两阶段语义检索，支持单篇定位 |
| `GET /api/inbox` / `POST /api/inbox/scan` | 投递列表 / 扫描入队 |
| `POST /api/inbox/upload` / `POST /api/inbox/from_url` | 文件与 URL 投递 |
| `POST /api/papers/<id>/reparse` 等单步端点 | reenrich、retranslate、resummarize、reembed |
| `POST /api/papers/<id>/pipeline` | 多步处理 |
| `POST /api/papers/<id>/bibtex` | 同步刷新 BibTeX |
| `GET /api/citations` / `POST /api/citations/rebuild` | 引文图读取与重建 |
| `GET /api/tasks` / `GET /api/tasks/<id>` | 任务列表与详情 |
| `POST /api/tasks/<id>/retry` | 创建重试任务 |
| `GET /api/events` | SSE 事件流 |
| `GET/POST/PUT/DELETE /api/glossary` | 热词管理 |
| `GET /api/settings` / `GET /api/llm_usage` | 配置状态与模型用量 |
| `GET /api/health` | 公共健康检查，始终返回存储状态 |

`GET /api/tasks` 返回 `items/count/total/limit/offset`，其中 `count` 是当前页长度，`total` 是筛选后的总数。相同论文、类型和参数的活动任务可以复用。

## MCP

### 传输与安全边界

本地 stdio 使用 `paper mcp`，保留全部 19 个工具。HTTP 传输使用 `paper mcp --transport http`，提供 17 个工具（不注册 `ingest_local_file` 与 `ingest_local_files`）；Compose 中它是 profile 为 `mcp` 的可选服务，启用后发布在 `127.0.0.1:5051`，端点路径 `/mcp`。

公开版没有 OAuth 和角色：HTTP 传输的安全边界是回环监听（或部署者自行配置的可信网络）。把 MCP HTTP 暴露到不可信网络前，必须自行评估风险。`ingest_by_url` 始终拒绝内网地址。

HTTP 传输隐藏并拒绝 `ingest_local_file` 与 `ingest_local_files`，与部署方式无关；服务器本地 PDF 只能通过 stdio 投递。

MCP 工具返回 JSON 字符串，业务错误使用 `{"error": ...}`，不把异常直接抛给客户端。全文与翻译按字符分页，单次上限 50K；`toc` 只随 `offset=0` 首页返回。

### 工具清单

| 工具 | 用途 |
|---|---|
| `search_papers` | arXiv、OpenAlex、Semantic Scholar、Sciverse 外部检索；支持 DOI 精确解析 |
| `search_library` / `get_paper` / `get_paper_by_doi` | 本地检索与单篇元数据 |
| `get_papers` / `get_papers_by_dois` | 每次最多 200 条的批量查询 |
| `semantic_search` | 库内或单篇语义检索，返回原文偏移 |
| `search_fulltext` | 单篇英文字面检索，返回命中偏移与上下文 |
| `read_fulltext` / `read_translation` | 英文与中文分页阅读 |
| `get_summary` | 中文总结 |
| `get_citations` | 引用或被引关系 |
| `get_annotations` / `add_annotation` | 批注读取与创建 |
| `get_task` / `list_tasks` | 任务状态与队列 |
| `ingest_by_url` | 远程 URL 投递 |
| `ingest_local_file` / `ingest_local_files` | 仅 stdio 的服务器本地文件投递 |

MCP 客户端配置见 [docs/guides/user.md](docs/guides/user.md#配置并使用-mcp)。会话侧检索纪律见 [.agents/skills/paper-retrieval/SKILL.md](.agents/skills/paper-retrieval/SKILL.md)。

### Kimi Code 技能安装

项目级技能是 `paper-retrieval`：规定检索顺序、证据分级和全文核实要求。本仓库会话自动加载项目技能；其他工作区在 `~/.kimi-code/config.toml` 顶层加入：

```toml
extra_skill_dirs = ["<仓库路径>/.agents/skills"]
```

新会话的技能清单应出现 `paper-retrieval`。MCP 工具签名或检索语义变化时，必须同步更新该技能文件。

## 数据模型与运行规则

SQLite schema 的权威定义是 [backend/db/schema.sql](backend/db/schema.sql)。主要分组：

| 数据 | 表 |
|---|---|
| 论文与检索 | `papers`、`papers_fts`、`citations`、`embeddings` |
| 阅读与复核 | `translations`、`summaries`、`annotations`、`doc_revisions`、`glossary` |
| 任务 | `tasks`、`events`、`llm_calls` |
| 其他 | 作者、标签和扫描日志 |

七个论文状态列为 `status_pdf`、`status_parsed`、`status_enrich`、`status_translated`、`status_summary`、`status_outline`（遗留）和 `status_embedded`。状态取值为 `missing`、`processing`、`ok`、`partial`、`error`、`manual_hold` 或 `skipped`。

关键约束：

- `dir_name` 是论文的稳定唯一标识，默认不修改；纠正时使用 `paper db rename`，不要删行重投。
- DOI 和 arXiv id 只能来自 PDF 标识符、外部候选多维校验或用户手动录入，不能接受 LLM 生成的值。
- `papers.pdf_path` 与 `parsed_path` 默认存相对 `library_root` 的路径；读取统一走 `resolve_path()`，缺失时降级为 `None`。
- 业务表保留操作者字段用于审计；公开版没有用户体系，新写入固定为 `system` 或 `NULL`，不再执行角色检查。

术语补充见 [docs/reference/context.md](docs/reference/context.md)。

## 前端结构

前端使用 React、TypeScript、Vite 和 Tailwind。主要位置：

- `frontend/src/lib/api.ts`：API 类型与请求包装。
- `frontend/src/components/DocReader.tsx`：EN/ZH/Summary 阅读与编辑。
- `frontend/src/pages/`：文献库、发现、任务、热词、引文和设置。
- `frontend/e2e/`：子路径、阅读器和交互的 Playwright 场景。

Markdown 使用 react-markdown、GFM、数学公式、安全 HTML 白名单和 KaTeX。图片路径 `images/x` 重写为 `/api/papers/<id>/images/x`。容器构建的根路径由 `ALLOW_ROOT_BASE_BUILD=1 npm run build` 产出；验证构建使用 `npm run build:verify`，不要用裸 `npm run build` 覆盖受保护的输出目录。

## 验证

首次运行浏览器测试前安装 Chromium：

```bash
cd frontend
npm run test:e2e:install
```

完整检查：

```bash
# 后端（Python 3.12 环境，已 pip install -e .）
PYTHONPATH=. python3 -m pytest backend/tests/ -q

# 前端
cd frontend
npx tsc --noEmit
npm run build:verify
npm run lint
npm test
npm run test:e2e
npm audit --audit-level=high
```

浏览器项目应实际执行全部场景，不要把 skipped 计为 passed。

修改代码前必须阅读 [AGENTS.md](AGENTS.md)；修改 ingest、DOI、翻译、渲染或并发逻辑前，还要阅读 [docs/reference/pitfalls.md](docs/reference/pitfalls.md) 对应小节。

## 项目结构

```text
<仓库根目录>/
├── backend/
│   ├── cli.py                 # paper 命令入口
│   ├── app.py                 # Flask 应用与蓝图注册
│   ├── config.py              # config.toml、.env 与路径/启动检查
│   ├── mcp_server.py          # stdio 与 HTTP MCP
│   ├── api/                   # Flask 蓝图
│   ├── db/                    # schema、连接、仓库与幂等迁移
│   ├── external/              # 外部 API client 与限频
│   ├── pipeline/              # PDF 分类与 chunk 切分/翻译
│   ├── services/              # 文档文本、语义检索、Zotero 等共享服务
│   ├── workers/               # scheduler、线程池与各处理步骤
│   └── tests/                 # pytest 与隔离沙箱
├── frontend/
│   ├── src/                   # React 页面、组件与 API
│   ├── e2e/                   # Playwright
│   └── public/serve.json      # 子路径静态回退规则
├── scripts/                   # 运维脚本（去重、回填、审计、MinerU 守护等）
├── Dockerfile / compose.yaml / compose.mineru.yaml
├── config.toml / .env.example
├── pyproject.toml / frontend/package.json
└── docs/                      # 总索引、guides、reference、adr 和 TODO
```

生产部署不作为开发启动的替代品。需要长期运行的服务时，按 [docs/guides/deployment.md](docs/guides/deployment.md) 完成容器化部署、备份和恢复演练。

## 许可证

本项目以 GNU Affero General Public License v3.0（AGPL-3.0-only）发布，完整条款见 [LICENSE](LICENSE)。通过网络提供服务时，AGPL 要求向使用者提供对应的源代码。

本项目通过 PyMuPDF 读取 PDF。PyMuPDF 采用 AGPL-3.0 与 Artifex 商业许可双授权；分发本项目的容器镜像或二进制时，必须遵守 AGPL-3.0，或另行取得 PyMuPDF 商业许可。第三方依赖保留各自的许可证。
