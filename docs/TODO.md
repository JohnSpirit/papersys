# 待办清单

> 分「待测试」与「待实现需求」两组；完成后将对应条目移除或标注完成状态。

## 待测试清单

1. **Zotero RIS 导入验证**：导出命令本身已实测（RIS/BibTeX 两种格式）；剩余 `docs/adr/0004-zotero-ris-l2-format-unverified.md` 的最后一步——把 RIS 导入真实 Zotero，检查 L2 附件按「链接附件根目录 = `library_root`」解析。
2. **CLI 命令级自动化测试（最小集）**：当前 `backend/tests/` 命令级用例为 0；历史代码审查曾在 CLI 里发现真实 bug（已修复）。最小集两件，进程内直调 `cmd_*(args)` 即可、不必起子进程（先例：`test_mcp_server.py` 进程内直调、`test_reader_api.py` 用 `create_app().test_client()`）：
   - `test_cli_feed.py`：默认 steps / `--steps` / `--translate-force` 的 payload 构造（monkeypatch `tasks_repo.create_task` 截获 payload；可作上述 bug 的回归）
   - `test_cli_backup.py`：monkeypatch `CONFIG["paths"]` 到 tmp_path 跑 `cmd_backup`，解包断言 tar 内含 papers.db 与 library 文件

## 待实现需求清单

1. **DocReader 图片编辑**：调整图片在文中位置、添加图注的专门界面。当前只能在编辑视图手动编辑 markdown（移动 `![](images/...)` 所在行、手写图注文字）；涉及 `frontend/src/components/DocReader.tsx`（编辑视图为 textarea 全文编辑）与 `frontend/src/components/MarkdownView.tsx`（渲染视图）。
2. **手动纠正论文元数据的入口**：当前 DOI、标题、作者加年份三层自动匹配失败或标题、年份校验不兼容时，`status_enrich` 会变为 `manual_hold`，但 Web 和 CLI 都没有正式的手动 DOI 编辑入口，直接修改 SQLite 对普通用户不安全。后续实现需要满足：
   - 在论文详情页和 `paper` CLI 提供 DOI、标题、第一作者和年份的编辑能力，保存前备份数据库，并保留可追踪的修改事件。
   - DOI 只接受 PDF 印刷标识、Crossref/OpenAlex 验证结果或用户手动确认值；执行格式归一化、重复 DOI 冲突检查，禁止采用 LLM 生成的 DOI。
   - 保存后将 `status_enrich` 重置为 `missing` 并显式创建 enrich 任务；默认 pipeline 会跳过 `manual_hold`，因此不能只依赖普通一键重跑。年份或 `dir_name` 需要变化时复用 `paper db rename` 的目录与衍生数据保留逻辑。
   - 为 DOI 无效、DOI 重复、标题纠正、年份纠正、并发修改和重新 enrich 添加后端与前端测试。相关实现位置包括 `backend/api/papers.py`、`backend/db/repo.py`、`backend/cli.py`、`backend/workers/enrich_worker.py` 和 `frontend/src/components/PaperDetail.tsx`。
