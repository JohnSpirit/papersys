# papersys 术语表

本文定义 `paper` CLI 与任务的术语边界，供开发者与编码智能体使用。文档入口见 [../../README.md](../../README.md)，智能体约束见 [../../AGENTS.md](../../AGENTS.md)。

公开版没有用户、角色与会话体系；本文不再包含认证术语。

## Language

**运维外壳**:`paper` CLI 的能力边界——仅承担服务启停、任务查询、诊断、入队触发,不独立执行 worker handler。
_Avoid_: 独立运行时、离线引擎

**入队(enqueue)**:调用 `tasks_repo.create_task` 写入 pending task,交由 Flask 内 scheduler 线程池领取执行。
_Avoid_: async(与 Python asyncio 混淆)、直接执行

**僵尸 task**:已入队但 scheduler 未运行的 pending task,永不被领取,污染任务列表。

**批量入队**:CLI 对多篇论文一次性创建多个 pending task,复用 scheduler 限频池(translate/default 池)安全并发。
_Avoid_: 批处理直跑

**worker 命令**:CLI 中触发入队的动词(parse/translate/pipeline 等),与前端按钮等价,增值在批量。
_Avoid_: sync 命令、离线命令

## Relationships

- **worker 命令** 产生若干 **入队** 行,依赖 Flask scheduler 领取
- Flask 未运行时,**入队** 形成 **僵尸 task**;故 worker 命令默认检测 Flask 存活
- **批量入队** 复用 scheduler 限频池,不绕过外部 API 限频
- **运维外壳** 边界:tasks 查询 / db 操作 / 诊断不依赖 Flask;worker 入队依赖 Flask

## Task fields

**执行类别（execution_subject）**：任务按 `system` 或历史遗留的 `user` 标记。公开版没有用户体系，新写入固定为 `system` 或 `NULL`，Worker 不再执行角色检查；该列仅用于审计区分本地操作与派生数据任务。

**操作者（created_by_user_id）**：论文、批注、修订或任务的创建者字段，仅作审计保留。历史记录允许为 NULL；EN 保存后的自动重嵌为 `system` 任务，保留触发者用于审计，但不按该字段做任何权限判断。

## Flagged ambiguities

- "async/sync" 曾区分入队 vs 直跑,CLI 定位为运维外壳后 sync 取消,术语废弃,统一"入队"。
- "watcher" 指 inbox 自动监听常驻服务,归 Flask daemon 线程,非 CLI 命令(CLI 只做一次性 `inbox scan`)。

## Example dialogue

> **Dev:** "`paper translate 1 2 3` 时 Flask 没跑会怎样?"
> **运维:** "那是 **僵尸 task**——入队了但没人领取。所以 worker 命令默认检测 Flask,没跑就提示先 `paper serve`。"
