# papersys 部署手册

本文面向把 papersys 部署为长期运行服务的人员，说明容器安装、配置、运行、升级与恢复。普通使用方法见 [user.md](user.md)；开发接口见 [../../README.md](../../README.md)。

公开版以单应用容器部署：一个镜像同时托管前端静态文件与 Flask API，数据全部保存在 Compose 命名卷挂载的 `/data` 下。MinerU 默认使用 Cloud API；本地 GPU 部署是可选组件。

## 前置条件

- Docker Engine 与 Compose 插件（`docker compose version` 可执行）。
- 外部服务密钥：MinerU Cloud token、LLM API key、托管检索（Fireworks）API key。各 key 的含义见 [../../README.md](../../README.md#配置)。
- 资源建议：1 vCPU / 2 GiB 内存可运行基础流水线；解析大 PDF 或并发翻译时建议更多内存。

## 安装

```bash
# 1. 获取源码并进入仓库根目录
cd <仓库目录>

# 2. 准备密钥文件
cp -n .env.example .env
chmod 600 .env
$EDITOR .env

# 3. 构建并启动
docker compose up --build
```

启动后验证：

```bash
curl http://127.0.0.1:8080/api/health
curl http://127.0.0.1:8080/api/stats
```

Compose 默认发布：

| 端口 | 用途 |
|---|---|
| `127.0.0.1:8080` | Web 界面与 HTTP API（`web` 服务，默认启动） |
| `127.0.0.1:5051` | MCP HTTP（`mcp` 服务，可选，默认不启动） |

默认只有本机能访问。其他机器通过 SSH 隧道使用：

```bash
ssh -L 8080:127.0.0.1:8080 -L 5051:127.0.0.1:5051 <部署主机>
```

MCP HTTP 是 Compose profile 为 `mcp` 的可选服务，默认不启动；需要时执行 `docker compose --profile mcp up -d` 创建，端点路径为 `/mcp`（`http://127.0.0.1:5051/mcp`）。

## 配置

### 密钥（.env）

密钥只通过 `.env` 注入：Compose 使用 `env_file` 读取，`.env` 被 `.dockerignore` 排除，不进入镜像层、构建参数或日志。至少填写：

- `MINERU_TOKEN`：MinerU Cloud 解析。
- `LLM_API_KEY`：翻译与总结（OpenAI Chat Completions 兼容端点）。
- `FIREWORKS_API_KEY`：托管嵌入与精排。

其余可选 key（Semantic Scholar、Sciverse、OpenAlex 提额）见 `.env.example` 注释。

### 非 secret 配置（config.toml）

仓库根目录的 `config.toml` 就是公开配置模板；它在镜像构建时复制进镜像（`/app/config.toml`），也是容器内实际读取的配置。容器内数据根目录统一为 `/data`，一般不需要修改。常用覆盖项：

- `[mineru].mode`：`cloud`（默认）或 `local`（本地 GPU 服务）。
- `[llm].api_url` / `[llm].model`：翻译与总结的端点和模型。
- `[retrieval]` 四元组 `embed_base_url/embed_model/rerank_base_url/rerank_model`：托管检索端点，必须全部设置。

修改 `config.toml` 后必须重新构建镜像（`docker compose up --build -d`）才能生效。不改配置文件、改用环境变量覆盖时无需重建：`PAPER_MINERU_MODE` / `PAPER_MINERU_LOCAL_API_BASE`、`PAPER_LLM_API_URL` / `PAPER_LLM_MODEL`、`PAPER_RETRIEVAL_EMBED_BASE_URL` 等覆盖项在 Compose 的 `environment` 段设置后，执行 `docker compose up -d` 或 `docker compose restart` 即可重新加载。

容器内 Gunicorn 监听 `0.0.0.0`，由 Compose 通过 `PAPER_ALLOW_REMOTE=1` 显式允许；宿主机回环发布仍是实际访问边界。不要把这个变量与对公网开放混为一谈。

### 数据卷

所有运行数据（SQLite、PDF 文库、MinerU 解析结果、缓存、日志、inbox、修订快照）都在 Compose 命名卷挂载的 `/data` 下：

```bash
docker volume ls | grep paper-data   # 实际卷名为 <Compose 项目名>_paper-data
```

`docker compose down` 不删除命名卷。删除数据卷是破坏性操作，执行前必须另行确认。

## MinerU：Cloud 与本地 GPU

### Cloud（默认）

`config.toml [mineru].mode = "cloud"`，密钥为 `.env` 中的 `MINERU_TOKEN`。无需本地 GPU，适合大多数部署。

### 本地 GPU（可选）

`mode = "local"` 时，papersys 调用本机 MinerU HTTP 服务（`[mineru].local_api_base`）。需要：

- 支持 CUDA 的 NVIDIA GPU 与驱动；
- 按 MinerU 官方文档本地部署 MinerU 服务，并使其监听在 `local_api_base` 指定的地址；
- 在 `config.toml` 设置 `local_container`（容器名）以启用 `paper doctor` 的容器 GPU 检查；空字符串禁用该检查。

`scripts/mineru_guard.py` 是可选运维组件：定时检查本地 MinerU 容器的 GPU 与 `/health`，在满足安全条件时重启容器。它通过 Docker CLI 操作宿主机容器，只应在需要自动恢复时、以能访问 Docker 的方式（例如宿主机 cron 或 systemd timer）单独部署；主应用容器不挂载 Docker socket，也不包含该脚本需要的容器运行时。

本地 GPU 模式请在与生产数据隔离的环境中补验后再启用。

## 运行与维护

### 日志

```bash
docker compose logs -f web        # Web 容器标准输出（Gunicorn 与应用日志）
docker compose logs -f mcp        # MCP HTTP 容器标准输出（启用 profile 后）
```

Worker 任务级日志在数据卷 `/data/logs/` 下。容器内没有 `/tmp/flask.log`；源码部署的 `paper serve logs` 只适用于本机开发进程，不适用容器。

### 诊断

```bash
docker compose exec web paper doctor
```

`paper doctor` 检查数据库结构、存储、配置、托管检索端点和 MinerU HTTP 连通性；Cloud 模式不检查本地 GPU 容器。

### 备份

在应用容器内执行：

```bash
docker compose exec web paper backup -o /data/backups/papers_$(date +%Y%m%d).tar.gz
```

备份包含 SQLite 一致性快照、library、修订快照和报告，写入数据卷内 `backups/`。恢复数据库或文库属于破坏性操作：先停止应用容器（`docker compose stop web`），再按恢复点解包，并确认没有进行中的任务写入。

## 升级

```bash
cd <仓库目录>
git pull                          # 或以新源码覆盖
docker compose up --build -d
docker compose exec web paper doctor
```

升级步骤：

1. 升级前执行一次 `paper backup`。
2. 重新构建并启动；镜像构建以前端 `npm ci` + 根路径构建和后端依赖安装为主，耗时取决于网络。
3. 数据库结构变更由 `backend/db/migrations/` 的幂等迁移处理；启动日志出现缺列提示时，按提示执行对应迁移。
4. 验证 `/api/health`、`/api/stats` 与文献库页面。

回滚：停止应用容器，恢复升级前备份的数据库与文库，再以旧版本镜像启动。

## 反向代理（可选）

默认回环监听已经限定只有本机可访问。需要向局域网提供访问时，在更外层放置通用反向代理（Nginx、Caddy 等）：

- 把 `/`（含 `/api`）原样代理到 `127.0.0.1:8080`；应用容器按根路径提供服务，不支持挂载到子路径；
- `/api/events`（SSE）需要关闭响应缓冲并设置长连接超时；
- MCP HTTP（`127.0.0.1:5051/mcp`）如需代理，使用 HTTP/1.1 并关闭缓冲。

公开版没有登录体系，反向代理层必须自行承担访问控制（IP 白名单、mTLS 或等价手段）；不要把服务直接暴露到不可信网络。
