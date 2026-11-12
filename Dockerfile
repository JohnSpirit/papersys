# syntax=docker/dockerfile:1
# papersys 3.11.2-open 应用镜像。
# 多阶段：Node 24 构建前端 -> Python 3.12 运行后端（Gunicorn 单 worker gthread）。
# 运行数据全部落在 /data（PAPER_DATA_DIR）；密钥只经环境变量注入，.env 不进镜像。
# 以完整源码树运行：schema.sql 经 package-data 声明随包安装，version.py 直接读
# /app/pyproject.toml；Gunicorn 从 /app 启动，本地 backend/ 包优先于 site-packages。

# ── 阶段 1：前端构建（容器以根路径发布，ALLOW_ROOT_BASE_BUILD 显式允许 base='/'）──
FROM node:24-bookworm-slim AS frontend
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN ALLOW_ROOT_BASE_BUILD=1 npm run build

# ── 阶段 2：Python 运行环境 ──
FROM python:3.12-slim-bookworm AS runtime
LABEL org.opencontainers.image.source=https://github.com/JohnSpirit/papersys \
      org.opencontainers.image.description="Local-first literature research, reading, and retrieval system" \
      org.opencontainers.image.licenses=AGPL-3.0
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /app

# 直接依赖版本固定在 pyproject.toml [project.dependencies]，保证镜像可重复构建。
COPY pyproject.toml ./
COPY LICENSE ./
COPY backend/ ./backend/
RUN pip install --no-cache-dir .

COPY config.toml ./
COPY docker/healthcheck.py ./docker/healthcheck.py
COPY --from=frontend /build/frontend/dist ./frontend/dist

RUN groupadd --system paper && useradd --system --gid paper paper \
    && mkdir -p /data && chown -R paper:paper /data /app

# 镜像默认回环监听（安全缺省）；Compose 以 PAPER_BIND=0.0.0.0:5050 与
# PAPER_ALLOW_REMOTE=1 覆盖，宿主机回环发布是真正的访问边界。
ENV PAPER_DATA_DIR=/data \
    PYTHONPATH=/app \
    PAPER_CONFIG_PATH=/app/config.toml
VOLUME ["/data"]
EXPOSE 5050 5051
USER paper

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "/app/docker/healthcheck.py"]

# 单 Gunicorn worker、gthread 多线程、timeout=0、进程内任务调度（backend/gunicorn.conf.py）。
CMD ["gunicorn", "-c", "backend/gunicorn.conf.py", "backend.app:app"]
