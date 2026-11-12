# papersys 文档索引

本目录按读者和文档状态组织内容。项目入口见 [../README.md](../README.md)，编码智能体规范见 [../AGENTS.md](../AGENTS.md)，版本变更见 [../CHANGELOG.md](../CHANGELOG.md)。

## 使用与部署手册

[guides/](guides/) 保存面向使用者的现行手册：

| 文档 | 读者与内容 |
|---|---|
| [guides/user.md](guides/user.md) | 使用者：Web 阅读与复核、任务和 MCP 使用 |
| [guides/deployment.md](guides/deployment.md) | 部署人员：容器安装、配置、运行、升级和恢复 |

## 开发参考资料

[reference/](reference/) 保存开发时需要反复查阅的资料：

| 文档 | 内容 |
|---|---|
| [reference/context.md](reference/context.md) | CLI 和任务术语 |
| [reference/pitfalls.md](reference/pitfalls.md) | ingest、DOI、翻译、渲染和并发的历史问题 |
| [reference/external-api-index.md](reference/external-api-index.md) | 外部 API 文档索引，实际行为以 client 代码和配置为准 |

当前待办位于 [TODO.md](TODO.md)。项目环境、架构、CLI、HTTP API、MCP 和测试命令统一维护在 [../README.md](../README.md)，不要在这些分类目录中重复复制完整参考表。

## 架构决策

- [adr/](adr/)：记录已经作出的架构决策及其理由。

## 文档维护规则

1. 新增用户操作放入对应指南，新增开发接口放入根目录 `README.md`，新增智能体约束放入根目录 `AGENTS.md`。
2. 移动已跟踪文件时使用 `git mv`，同时修改链接文本、链接目标和目录索引。
3. 文档内容变化后检查本地文件与章节锚点。
4. `docs/TODO.md` 只记录当前待办，完成项及时移除或更新。
