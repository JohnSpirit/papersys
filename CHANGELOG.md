# Changelog

## 3.11.2-open (2026-11-12)

首个公开版本。

### 主要能力

- 文献入库、MinerU 解析、OpenAlex/Crossref/Europe PMC 元数据补全与摘要校验。
- 向量化与托管两阶段语义检索：嵌入召回加精排，支持跨库检索和单篇定位。
- 翻译与总结：按 chunk 独立提交，支持部分成功重试和人工覆盖层。
- 自建阅读器：EN/ZH/Summary 三视图、编辑草稿、修订快照回滚、批注与热词。
- `paper` CLI、Flask HTTP API，以及 MCP stdio 与 HTTP 两种传输。
- Zotero RIS/BibTeX 导出与引文网络。

### 部署

- 移除登录、角色和会话体系，访问边界由网络位置决定，默认只监听回环地址。
- 单应用 Docker 镜像与 Docker Compose 部署，运行数据统一写入 `/data` 卷。
- MinerU 默认使用 Cloud API；本地 GPU 服务由 `compose.mineru.yaml` 作为可选叠加文件提供。
- 非回环监听必须显式设置 `PAPER_ALLOW_REMOTE=1`，未设置时启动失败。

### 安全与发布

- OpenAlex 请求 URL 中的 `api_key` 在异常信息、任务错误、事件和日志写入前统一脱敏。
- `.gitignore` 覆盖 `.env.*`、验证构建、端到端测试与 Playwright 报告产物。
- 端到端测试与前端单测改用虚构论文夹具，不再引用真实文库目录。
- 采用 AGPL-3.0-only 许可证，许可证全文见 `LICENSE`。
