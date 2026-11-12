# CLI 定位为运维外壳,不与 Flask 解耦

scheduler 内嵌 Flask(`backend/workers/runner.py:110`)。`paper` CLI 决定不与 Flask 解耦:worker 命令一律 `tasks_repo.create_task` 入队,依赖 Flask scheduler 领取;tasks / db / cache / config / doctor 命令直读 DB 不依赖 Flask。放弃原方案提议的 `--sync` 直跑模式。

理由:入队复用 scheduler 限频池(translate 池 size=2),既安全又省实现;独立调度需自行处理并发/限频/`WorkerContext` 构造,复杂度高;离线批处理非高频场景。代价:Flask 未运行时 worker 命令需 `--force` 才能入队(否则成僵尸 task)。
