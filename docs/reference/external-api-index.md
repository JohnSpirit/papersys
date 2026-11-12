# 外部依赖 API 文档索引

> 所有 URL 均已实际访问验证。✓ = 文档成功获取并核对内容一致。
> 本文件随 client 代码演化同步更新；client 文件位于 `backend/external/`。

---

## API Key 存储位置（统一）

所有 key 通过 `backend/config.py:load_config()` 读取，存入 `CONFIG["secrets"]` 字典。
底层唯一来源：**项目根目录的 `.env`**（gitignore，不入 git），用 `python-dotenv` 的
`load_dotenv(override=False)`。

### 各服务的 Key env 变量名

| 服务 | env 变量名 | 是否必需 |
|---|---|---|
| MinerU | `MINERU_TOKEN` | ✅ 必需（缺失 raise） |
| LLM（翻译/总结，OpenAI 兼容端点） | `LLM_API_KEY` | ✅ 必需（缺失 raise） |
| Semantic Scholar | `SEMANTIC_SCHOLAR_API_KEY` | 🟡 可选（无 key 走共享慢桶） |
| Sciverse | `SCIVERSE_API_TOKEN` | 🟡 可选（无 key 不加 Authorization 头） |
| OpenAlex | `OPENALEX_API_KEY` | 🟡 可选（无 key 走 $0.10/天低额度） |
| arXiv | 无 | - |
| Crossref | 无（polite pool，未实现 mailto） | - |
| Europe PMC | 无 | - |

---

## 架构共性

- **限频基类**：`backend/external/base.py:RateLimitedClient`（令牌桶 + 磁盘缓存）。OpenAlex / Semantic Scholar / arXiv / Sciverse / Crossref / Europe PMC 继承它
- **不继承的 2 个**：MinerU（二进制上传，缓存无意义）/ LLM（写操作，靠 Retry）
- **磁盘缓存**：数据根目录下 `cache/<service>/<sha1>.json`，TTL 24h（`config.toml [external].cache_ttl_hours`）
- **Session 复用**：每个 client 独立 `requests.Session()`，不跨 Worker 进程共享
- **限频参数集中**：`config.toml [external]` 段（mineru/llm 各有独立段）

---

## 1. OpenAlex ✓

| 项目 | 值 |
|---|---|
| **文档入口** | https://developers.openalex.org/ |
| **API Reference** | https://developers.openalex.org/api-reference/introduction |
| **认证与定价** | https://developers.openalex.org/guides/authentication |
| **LLM 参考摘要** | https://developers.openalex.org/llms.txt |
| **LLM 优化参考页** | https://developers.openalex.org/guides/llm-quick-reference |
| **OpenAPI 3.1 规范** | https://developers.openalex.org/api-reference/openapi.json |
| **错误处理** | https://developers.openalex.org/api-reference/errors |
| **API Key 申请** | https://openalex.org/settings/api |
| **Base URL** | `https://api.openalex.org` |
| **认证方式** | Query param `?api_key=YOUR_KEY` |
| **免费额度** | $1/天（带 key）；$0.10/天（无 key） |
| **项目内 client** | `backend/external/openalex.py` |
| **本系统用法** | 元数据富化、引文网络、Discover 搜索；从 `locations[].landing_page_url` 提取 arxiv_id |
| **项目内限频** | `openalex_rps = 5.0`（`config.toml`） |

### 实体端点（共 22 类）

`/works` `/authors` `/sources` `/institutions` `/topics` `/keywords` `/publishers` `/funders` `/awards` `/domains` `/fields` `/subfields` `/sdgs` `/countries` `/continents` `/languages` `/work-types` `/source-types` `/institution-types` `/licenses` `/concepts`（deprecated）

### 核心操作

List / Get / Filter / Search / Aggregate(group_by) + Autocomplete

### 外部 ID 查询

DOI / PMID / ORCID / ROR / ISSN 均支持 `GET /works/doi:10.xxxx` 格式

### URL 变更说明

旧 URL `https://docs.openalex.org` 现重定向到新域名 `https://developers.openalex.org/`。

---

## 2. Semantic Scholar ✓

| 项目 | 值 |
|---|---|
| **文档入口** | https://api.semanticscholar.org/api-docs |
| **Swagger JSON（机器可读）** | https://api.semanticscholar.org/graph/v1/swagger.json |
| **子文档** | Graph API: `/api-docs/graph` · Recommendations: `/api-docs/recommendations` · Datasets: `/api-docs/datasets` |
| **API Key 申请** | https://www.semanticscholar.org/product/api#api-key |
| **Base URL** | `https://api.semanticscholar.org/graph/v1` |
| **认证方式** | Header `x-api-key: YOUR_KEY` |
| **项目内 client** | `backend/external/semantic_scholar.py` |
| **本系统用法** | 关键词搜索、按 DOI 查询；返回 citationCount / influentialCitationCount / tldr / openAccessPdf |
| **项目内限频** | `semantic_scholar_rps = 1.0`（**严格**） |

swagger.json 实测可用（123KB，OpenAPI 规范，含 `/paper/search` `/paper/{paper_id}` `/paper/DOI:{doi}` 等 endpoint 定义）。

---

## 3. Sciverse ✓

| 项目 | 值 |
|---|---|
| **文档入口（OpenAPI 3.0.3 规范）** | https://sciverse.opendatalab.com/openapi.json |
| **生产环境 Base URL** | `https://api.sciverse.space` |
| **开发环境 Base URL** | `https://api-dev.sciverse.space` |
| **认证方式** | `Authorization: Bearer YOUR_API_TOKEN`（控制台申请的 API Token） |
| **项目内 client** | `backend/external/sciverse.py` |
| **本系统用法** | `semantic_search`（agentic-search）+ `meta_search`（meta-search） |
| **项目内限频** | `sciverse_rpm = 60`（即 1 req/s） |

### 完整 endpoint 列表（6 个公开接口）

| Endpoint | 方法 | 功能 |
|---|---|---|
| `/agentic-search` | POST | 语义检索，返回 chunk 级命中（含 page_no、offset 定位） |
| `/content` | GET | 读取原文全文或按 Unicode 码点分页片段 |
| `/resource` | GET | 下载图片等二进制资源附件 |
| `/meta-catalog` | GET | 元数据字段目录（filterable/sortable/searchable 字段能力） |
| `/meta-search` | POST | 结构化元数据检索（filter/sort/facets/cursor 深翻页） |
| `/meta-paper-relations` | POST | 论文引用关系分页（CITATIONS/REFERENCES/RELATED_WORKS） |

### 关键发现（项目代码未用到的能力）

- `/content` + `/resource` 端点：可读取全文与图片（`doc_id` 来自 agentic-search 命中）
- `/meta-paper-relations` 端点：完整引文网络分页查询（比 OpenAlex 批量查更直接）
- `filters` 支持复杂结构（gt/gte/lt/lte 范围、IN/NIN 集合、topics 维度过滤）
- `freshness_boost` 新鲜度加权（MILD/STRONG）

### 支持的头

`X-Sciverse-Source`（调用来源标识）、`X-Request-ID`（链路追踪）

---

## 4. arXiv ✓

| 项目 | 值 |
|---|---|
| **文档入口** | https://info.arxiv.org/help/api/index.html |
| **API Basics** | https://info.arxiv.org/help/api/basics.html |
| **User Manual** | https://info.arxiv.org/help/api/user-manual.html |
| **Terms of Use** | https://info.arxiv.org/help/api/tou.html |
| **社区** | https://info.arxiv.org/help/api/basics.html#community |
| **API 源码仓库** | https://github.com/arXiv/arxiv-docs |
| **Base URL** | `https://export.arxiv.org/api/query` |
| **认证方式** | 无需 API Key |
| **项目内 client** | `backend/external/arxiv.py` |
| **本系统用法** | 关键词搜索（Atom XML 解析），返回 PDF 链接 + primary_category |
| **项目内限频** | `arxiv_rps = 0.33`（~1 req/3s） |

响应格式为 Atom XML（项目代码用 `xml.etree.ElementTree` 解析，命名空间 `http://www.w3.org/2005/Atom`）。

### 参数

`search_query`（支持 `all:` / `ti:` / `au:` 等字段前缀）/ `max_results` / `sortBy`（relevance|lastUpdatedDate|submittedDate）/ `sortOrder`

---

## 5. Crossref ✓

| 项目 | 值 |
|---|---|
| **文档入口（Swagger UI）** | https://api.crossref.org/swagger-ui/index.html |
| **Base URL** | `https://api.crossref.org` |
| **主要 endpoint** | `/works` `/journals` `/funders` `/types` `/licenses` `/members` |
| **认证方式** | 无需 key；用 **polite pool**（加 `mailto` 参数到 User-Agent 或 query） |
| **项目内 client** | `backend/external/crossref.py` |
| **本系统用法** | PDF 标识符校验（`get_work`）+ 打分制反查 DOI（`search_best`）；按 DOI 获取 BibTeX（content negotiation） |
| **项目内限频** | 5 req/s（polite pool） |

### BibTeX 获取（项目已用）

`GET https://api.crossref.org/works/{doi}/transform/application/x-bibtex`，Header `Accept: application/x-bibtex`

### 注意

项目代码当前**未实现** `mailto` polite pool（建议后续在 `crossref.py` 加 `User-Agent: ... (mailto:xxx)` 提速到 50 req/s）。

### URL 变更说明

旧 URL `https://github.com/CrossRef/rest-api-docs` 仓库已归档，Swagger UI 为新权威源。

---

## 6. MinerU ✓

| 项目 | 值 |
|---|---|
| **文档入口** | https://mineru.net/apiManage/docs |
| **API 管理页（创建 Token）** | https://mineru.net/apiManage |
| **输出文件参考** | https://opendatalab.github.io/MinerU/reference/output_files/ |
| **GitHub（开源项目）** | https://github.com/opendatalab/MinerU |
| **Base URL** | `https://mineru.net/api/v4`（精准）/ `/api/v1/agent`（轻量） |
| **认证方式** | Header `Authorization: Bearer YOUR_TOKEN`（精准 API）；轻量 API 免登录 |
| **项目内 client** | `backend/external/mineru.py` |
| **本系统用法** | 精准 API v4：`/file-urls/batch`（批量上传 OSS）-> `/extract-results/batch/{batch_id}`（轮询）-> 下载 ZIP 解压得 `full.md` |
| **项目内限频** | 5 任务/min（类级令牌窗口，跨实例共享） |

### 两套 API 对比

| 维度 | 🎯 精准解析 API | ⚡ Agent 轻量解析 API |
|---|---|---|
| Token | ✅ 需要 | ❌ 免登录（IP 限频） |
| 文件大小 | ≤ 200MB | ≤ 10MB |
| 页数 | ≤ 200 页 | ≤ 20 页 |
| 模型 | pipeline / vlm / MinerU-HTML | pipeline 轻量 |
| 输出 | Zip（MD/JSON/docx/html/latex） | 仅 Markdown（CDN 链接） |
| 批量 | ✅（≤50） | ❌ 单文件 |

### 本系统使用精准 API

`submit_v4` -> `poll_v4` -> `download_zip` -> 解析出 `full.md` + `images/` 目录

### 关键参数

`model_version`（pipeline/vlm/MinerU-HTML）/ `is_ocr` / `enable_formula` / `enable_table` / `language` / `page_ranges` / `data_id` / `callback`+`seed`

### URL 变更说明

旧 URL `https://mineru.net` 为首页，非文档；正确文档入口为 `https://mineru.net/apiManage/docs`。

---

## 7. Europe PMC ✓

| 项目 | 值 |
|---|---|
| **文档入口** | https://europepmc.org/RestfulWebService |
| **Base URL** | `https://www.ebi.ac.uk/europepmc/webservices/rest` |
| **主要 endpoint** | `/search`（`query` + `format=json` + `resultType` + `pageSize`） |
| **认证方式** | 无需 API Key（公开 REST） |
| **项目内 client** | `backend/external/europepmc.py` |
| **本系统用法** | abstract 第三源：按 DOI 取摘要（`fetch_abstract`，`query=DOI:"..."`）；无标识符论文的打分制标题反查（`search_work_by_title`，标题相似度 ≥ 0.90 + 年份 ±1 + 第一作者姓出现在 authorString）。OpenAlex/Crossref 对 Springer Nature / Cell Press 系期刊普遍无 abstract（实测 15/15 抽样双源皆空），Europe PMC 对同批 DOI 全部有摘要 |
| **项目内限频** | `europepmc_rps = 3.0`（官方无硬配额，礼貌使用）（`config.toml`） |

### 关键参数

`query` 支持 `DOI:"10.xxxx/yyy"`、`TITLE:"..."` 等字段限定；**`resultType=core` 才返回
`abstractText`**（lite 只有标题等基本字段）；响应 `resultList.result[]` 含
`title / doi / pubYear / authorString / abstractText`。

### 注意

标题反查只用于补 abstract，**不写 DOI / arxiv_id**（红线 6 的允许来源为
Crossref/OpenAlex 反查与手动录入，不含 Europe PMC）。

---

## 8. LLM（OpenAI Chat Completions 兼容）- 补充

翻译/总结 LLM 与具体提供方解耦：`backend/external/llm.py:LLMClient` 面向
任何兼容 OpenAI chat/completions 协议的端点（Volces ark / DeepSeek / Kimi /
OpenRouter / vLLM / Ollama 等）。端点与模型名在 `config.toml [llm]`，key 在
`.env` 的 `LLM_API_KEY`。
以 Volces ark 为例：

| 项目 | 值 |
|---|---|
| **文档入口** | https://www.volcengine.com/docs/82379 |
| **API 通用规范** | https://www.volcengine.com/docs/6656 |
| **Base URL** | `https://ark.cn-beijing.volces.com/api/plan/v3/chat/completions`（config.toml [llm].api_url，完整含路径） |
| **认证方式** | Header `Authorization: Bearer YOUR_API_KEY` |
| **项目内 client** | `backend/external/llm.py` |
| **本系统用法** | 翻译与总结 LLM（请求体 `{model, messages, temperature}`，解析 `choices[0].message.content`） |
| **项目内限频** | 靠 `Retry` adapter 退避（429/5xx，backoff_factor=2，total=3） |

---

## 速查表：8 个外部依赖一览

| # | 服务 | client 文件 | 功能 | API Key env | 限频 | Base URL | 文档入口 |
|---|---|---|---|---|---|---|---|
| 1 | OpenAlex | `openalex.py` | 元数据富化、引文网络 | `OPENALEX_API_KEY` | 5 req/s | `https://api.openalex.org` | https://developers.openalex.org/ |
| 2 | Semantic Scholar | `semantic_scholar.py` | 关键词搜索、按 DOI 查询 | `SEMANTIC_SCHOLAR_API_KEY` | 1 req/s 严格 | `https://api.semanticscholar.org/graph/v1` | https://api.semanticscholar.org/api-docs |
| 3 | Sciverse | `sciverse.py` | 语义检索 + 结构化搜索 | `SCIVERSE_API_TOKEN` | 60 req/min | `https://api.sciverse.space` | https://sciverse.opendatalab.com/openapi.json |
| 4 | arXiv | `arxiv.py` | 关键词搜索（Atom XML） | 无需 key | ~1 req/3s | `https://export.arxiv.org/api/query` | https://info.arxiv.org/help/api/index.html |
| 5 | Crossref | `crossref.py` | PDF metadata 反查 DOI、BibTeX | 无需 key（polite pool） | 5 req/s | `https://api.crossref.org` | https://api.crossref.org/swagger-ui/index.html |
| 6 | MinerU | `mineru.py` | PDF 解析（精准 API v4） | `MINERU_TOKEN` | 5 任务/min | `https://mineru.net/api/v4` | https://mineru.net/apiManage/docs |
| 7 | Europe PMC | `europepmc.py` | abstract 第三源（DOI 查询 + 标题反查） | 无需 key | 3 req/s | `https://www.ebi.ac.uk/europepmc/webservices/rest` | https://europepmc.org/RestfulWebService |
| 8 | LLM（OpenAI 兼容，可换提供方） | `llm.py` | 翻译与总结 LLM | `LLM_API_KEY` | 模型相关 | config.toml [llm].api_url（完整 chat/completions URL） | 视提供方而定（Volces: https://www.volcengine.com/docs/82379） |
