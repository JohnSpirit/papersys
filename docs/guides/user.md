# papersys 用户手册

本文面向使用 papersys 阅读、检索、复核文献和执行流水线的用户。公开版没有登录、角色和会话体系：所有能访问服务的人共享同一个文献库，所有功能按钮直接可用。访问边界由网络位置决定——默认只监听回环地址，从其他机器访问请使用 SSH 隧道。

部署与升级见 [deployment.md](deployment.md)。开发接口和本地开发环境见 [../../README.md](../../README.md)。

## 访问服务

Compose 部署的默认地址：

- Web 界面：`http://127.0.0.1:8080/`
- HTTP API 与健康检查：`http://127.0.0.1:8080/api/health`
- MCP HTTP（可选服务，默认不启动，启用方式见 [deployment.md](deployment.md)）：`http://127.0.0.1:5051/mcp`

从其他机器访问，在本地建立 SSH 隧道后按回环地址使用：

```bash
ssh -L 8080:127.0.0.1:8080 -L 5051:127.0.0.1:5051 <部署主机>
```

源码部署的开发地址为 `http://127.0.0.1:5173/`（前端）与 `http://127.0.0.1:5050`（API）。

## 使用 Web 界面

### 文献库与检索

“文献库”用于按关键字、作者、全文、年份和流水线状态筛选论文。语义检索用于按自然语言描述查找相关片段，结果可以定位到原文。

“发现”用于检索 arXiv、OpenAlex、Semantic Scholar 和 Sciverse，并可以把命中的 PDF 加入投递目录或创建入库任务。

论文详情显示标题、作者、DOI、流水线状态、原文、译文和总结。PDF、解析图片和文档内容使用相同的 `/api` 前缀加载。

### 文档复核与修订

- 在“渲染”“原始”“编辑”视图之间切换。
- 编辑 EN 原文、ZH 译文或总结并保存。
- 在“历史”中预览修订快照并回滚。
- 使用划词批注记录引用、备注和位置。

页签切换保留草稿。保存时携带读取时的内容摘要；若服务器内容已被其他请求修改，页面会提示冲突，由用户选择重新读取或明确覆盖。EN 保存后，后续翻译会使用复核版，并自动创建系统重嵌任务，避免语义检索继续使用旧向量。

### 热词与引文

“热词库”用于维护翻译术语，可新增、修改和删除。

“引文网络”显示论文之间的引用关系，支持按年份和关键字筛选、查看节点详情和聚焦相邻论文。

### 任务与流水线

“任务”页面可以上传 PDF、扫描投递目录、从 URL 入库，以及执行重新解析、富化、翻译、总结、重嵌、BibTeX 刷新、引文重建和流水线。任务详情与日志在同一页面查看，失败任务可以创建重试任务。

事件流（SSE）持续推送任务和流水线事件。

## 配置并使用 MCP

### 本地 stdio

本地 stdio 保留全部 19 个工具，适合在运行 papersys 的机器上使用：

```json
{
  "mcpServers": {
    "paper": {
      "command": "paper",
      "args": ["mcp"]
    }
  }
}
```

stdio 下的 `ingest_local_file` 和 `ingest_local_files` 可以读取服务器本地 PDF。

### HTTP 传输

HTTP 传输使用独立进程：

```bash
paper mcp --transport http --host 127.0.0.1 --port 5051
```

Compose 部署把它作为可选服务（profile `mcp`），默认不启动；`docker compose --profile mcp up -d` 启用后地址为 `http://127.0.0.1:5051/mcp`。HTTP 传输提供 17 个工具，始终禁止 `ingest_local_file` 和 `ingest_local_files`；服务器本地 PDF 只能通过 stdio 投递。HTTP 传输没有登录保护，不要把它暴露到不可信网络。

### 常见 MCP 操作

- 用 `search_library` 和 `semantic_search` 查本地论文。
- 用 `get_summary`、`read_fulltext` 和 `read_translation` 阅读内容。
- 用 `search_papers` 查外部来源，命中本地 DOI 时可转本地读取。
- 用 `ingest_by_url` 创建入库任务，再用 `get_task` 或 `list_tasks` 查询进度。
- 用 `add_annotation` 添加批注。

所有业务工具错误以 JSON `error` 字段返回。

## 常见问题

- **页面打不开**：确认 `docker compose ps` 中应用为 running，且访问的是回环地址或已建立 SSH 隧道。
- **上传或入库按钮没有反应**：查看“任务”页面的失败原因与事件流；入队类 CLI 依赖后端存活。
- **PDF 预览 404**：记录的历史路径在磁盘上缺失，属预期降级；重新投递该 PDF 即可。
- **检索结果为空**：`search_library` 零命中时换 `semantic_search` 再试；两种检索都为零才说明库内没有。
- **日志与密钥**：日志和 API 不输出 `.env` 中的密钥；排查问题时不要粘贴 `.env` 内容。
