"""3.11.2-open 运行环境与容器配置测试。

覆盖：PAPER_DATA_DIR 统一定位、PAPER_CONFIG_PATH、常用环境变量覆盖、
非回环监听的 PAPER_ALLOW_REMOTE 显式允许、配置无 OAuth 残留、
版本显示与 pyproject 主依赖完整性、容器资产静态检查。
"""
import tomllib
from pathlib import Path

import pytest

from backend import config as cfg
from backend.version import get_version

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

_ENV_KEYS = (
    "PAPER_CONFIG_PATH", "PAPER_DATA_DIR", "PAPER_ALLOW_REMOTE",
    "PAPER_BIND", "PAPER_HOST", "PAPER_PORT", "PAPER_HEALTH_URL",
    "PAPER_CORS_ORIGINS", "PAPER_MINERU_MODE", "PAPER_MINERU_API_BASE",
    "PAPER_MINERU_LOCAL_API_BASE", "PAPER_MINERU_LOCAL_CONTAINER",
    "PAPER_LLM_API_URL", "PAPER_LLM_MODEL",
    "PAPER_RETRIEVAL_EMBED_BASE_URL", "PAPER_RETRIEVAL_EMBED_MODEL",
    "PAPER_RETRIEVAL_RERANK_BASE_URL", "PAPER_RETRIEVAL_RERANK_MODEL",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in _ENV_KEYS:
        monkeypatch.delenv(name, raising=False)


def test_data_dir_relocates_all_paths(tmp_path, monkeypatch):
    """PAPER_DATA_DIR 把 db/cache/logs/library/inbox 统一定位到其下。"""
    monkeypatch.setenv("PAPER_DATA_DIR", str(tmp_path / "data-root"))
    cfg_loaded = cfg.load_config()
    paths = cfg_loaded["paths"]
    root = str(tmp_path / "data-root")
    assert paths["data_dir"] == root
    assert paths["db_path"] == f"{root}/papers.db"
    assert paths["cache_dir"] == f"{root}/cache"
    assert paths["log_dir"] == f"{root}/logs"
    assert paths["library_root"] == f"{root}/library"
    assert paths["inbox_dir"] == f"{root}/inbox"


def test_config_path_override(tmp_path, monkeypatch):
    """PAPER_CONFIG_PATH 指定配置文件位置。"""
    custom = tmp_path / "custom.toml"
    custom.write_text('[server]\nport = 6123\n', encoding="utf-8")
    monkeypatch.setenv("PAPER_CONFIG_PATH", str(custom))
    cfg_loaded = cfg.load_config()
    assert cfg_loaded["server"]["port"] == 6123


def test_config_path_falls_back_to_working_directory(tmp_path, monkeypatch):
    """安装入口从项目目录启动时，可以读取当前目录的 config.toml。"""
    monkeypatch.setattr(cfg, "PROJECT_ROOT", tmp_path / "installed-package")
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.toml").write_text('[server]\nport = 6124\n', encoding="utf-8")
    cfg_loaded = cfg.load_config()
    assert cfg_loaded["server"]["port"] == 6124


def test_env_overrides_for_server_mineru_llm_retrieval(monkeypatch):
    """监听、健康检查、MinerU、LLM、托管检索的常用环境变量覆盖生效。"""
    monkeypatch.setenv("PAPER_BIND", "0.0.0.0:5050")
    monkeypatch.setenv("PAPER_HOST", "0.0.0.0")
    monkeypatch.setenv("PAPER_PORT", "5050")
    monkeypatch.setenv("PAPER_HEALTH_URL", "http://127.0.0.1:5050/api/health")
    monkeypatch.setenv("PAPER_CORS_ORIGINS", "http://a.example,http://b.example")
    monkeypatch.setenv("PAPER_MINERU_MODE", "local")
    monkeypatch.setenv("PAPER_MINERU_API_BASE", "https://mineru.example/api/v4")
    monkeypatch.setenv("PAPER_MINERU_LOCAL_API_BASE", "http://mineru:8000")
    monkeypatch.setenv("PAPER_MINERU_LOCAL_CONTAINER", "")
    monkeypatch.setenv("PAPER_LLM_API_URL", "https://llm.example/chat/completions")
    monkeypatch.setenv("PAPER_LLM_MODEL", "test-model")
    monkeypatch.setenv("PAPER_RETRIEVAL_EMBED_BASE_URL", "https://emb.example/v1")
    monkeypatch.setenv("PAPER_RETRIEVAL_EMBED_MODEL", "emb-model")
    monkeypatch.setenv("PAPER_RETRIEVAL_RERANK_BASE_URL", "https://rerank.example/v1")
    monkeypatch.setenv("PAPER_RETRIEVAL_RERANK_MODEL", "rerank-model")
    cfg_loaded = cfg.load_config()
    assert cfg_loaded["server"]["bind"] == "0.0.0.0:5050"
    assert cfg_loaded["server"]["host"] == "0.0.0.0"
    assert cfg_loaded["server"]["port"] == 5050
    assert cfg_loaded["server"]["health_url"] == "http://127.0.0.1:5050/api/health"
    assert cfg_loaded["server"]["cors_origins"] == ["http://a.example", "http://b.example"]
    assert cfg_loaded["mineru"]["mode"] == "local"
    assert cfg_loaded["mineru"]["api_base"] == "https://mineru.example/api/v4"
    assert cfg_loaded["mineru"]["local_api_base"] == "http://mineru:8000"
    assert cfg_loaded["llm"]["api_url"] == "https://llm.example/chat/completions"
    assert cfg_loaded["llm"]["model"] == "test-model"
    assert cfg_loaded["retrieval"]["embed_base_url"] == "https://emb.example/v1"
    assert cfg_loaded["retrieval"]["embed_model"] == "emb-model"
    assert cfg_loaded["retrieval"]["rerank_base_url"] == "https://rerank.example/v1"
    assert cfg_loaded["retrieval"]["rerank_model"] == "rerank-model"


def test_bind_remote_requires_explicit_allow_remote(monkeypatch):
    """非回环监听：无 PAPER_ALLOW_REMOTE 拒绝；显式 =1 放行并告警。"""
    monkeypatch.setitem(cfg.CONFIG["server"], "bind", "0.0.0.0:5050")
    with pytest.raises(SystemExit):
        cfg.validate_bind_security()
    monkeypatch.setenv("PAPER_ALLOW_REMOTE", "1")
    cfg.validate_bind_security()  # 不抛异常


def test_bind_loopback_always_allowed(monkeypatch):
    """回环监听永远允许，不需要 PAPER_ALLOW_REMOTE。"""
    monkeypatch.delenv("PAPER_ALLOW_REMOTE", raising=False)
    for bind in ("127.0.0.1:5050", "localhost:5050", "[::1]:5050"):
        cfg.validate_bind_security(bind)


def test_no_oauth_left_in_config():
    """公开版配置不再有 auth 段与 OAuth/会话密钥。"""
    cfg_loaded = cfg.load_config()
    assert "auth" not in cfg_loaded
    oauth_keys = {
        "gitlab_status_token", "session_secret_key", "mcp_jwt_signing_key",
        "mcp_storage_encryption_key", "mcp_storage_key_id",
        "gitlab_web_oidc_client_id", "gitlab_web_oidc_client_secret",
        "gitlab_mcp_oidc_client_id", "gitlab_mcp_oidc_client_secret",
        "gitlab_probe_oidc_client_id", "gitlab_probe_oidc_client_secret",
    }
    assert oauth_keys.isdisjoint(set(cfg_loaded["secrets"]))


def test_optional_external_api_keys_and_mineru_mode(monkeypatch):
    """免费外部 API 的提额 key 可选，MinerU token 只在 Cloud 模式必需。"""
    from backend.cli import _secret_required

    for key in ("semantic_scholar_key", "sciverse_api_token", "openalex_api_key"):
        assert not _secret_required(key)
    assert _secret_required("llm_api_key")
    monkeypatch.setitem(cfg.CONFIG["mineru"], "mode", "cloud")
    assert _secret_required("mineru_token")
    monkeypatch.setitem(cfg.CONFIG["mineru"], "mode", "local")
    assert not _secret_required("mineru_token")


def test_version_display_and_pyproject():
    """健康检查版本显示 3.11.2-open；pyproject 使用 PEP 440 的 3.11.2+open。"""
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as f:
        declared = str(tomllib.load(f)["project"]["version"])
    assert declared == "3.11.2+open"
    assert get_version() == "3.11.2-open"


def test_pyproject_dependencies_complete_and_pinned():
    """主依赖组完整固定且不含 OAuth 依赖；schema.sql 声明为包数据。"""
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as f:
        doc = tomllib.load(f)
    deps = doc["project"]["dependencies"]
    names = {d.split("=")[0].split(">")[0].split("<")[0].strip() for d in deps}
    for required in ("flask", "flask-cors", "python-dotenv", "pymupdf", "pillow",
                     "fastmcp", "uvicorn", "gunicorn", "requests", "numpy"):
        assert required in names, f"missing dependency: {required}"
    assert "authlib" not in names
    assert "cryptography" not in names
    assert "auth" not in doc["project"].get("optional-dependencies", {})
    for dep in deps:
        assert "==" in dep, f"dependency not pinned: {dep}"
    package_data = doc["tool"]["setuptools"]["package-data"]
    assert "db/schema.sql" in package_data["backend"]


def _read(path: str) -> str:
    return (PROJECT_ROOT / path).read_text(encoding="utf-8")


def test_dockerfile_multi_stage_and_non_root():
    """Dockerfile：Node 24 构建（ALLOW_ROOT_BASE_BUILD）、Python 3.12、
    非 root 运行、healthcheck、Gunicorn 单 worker 配置。"""
    text = _read("Dockerfile")
    assert text.count("\nFROM ") >= 2  # 多阶段
    assert "node:24" in text
    assert "ALLOW_ROOT_BASE_BUILD=1 npm run build" in text
    assert "python:3.12" in text
    assert "\nUSER paper" in text
    assert "HEALTHCHECK" in text
    assert "gunicorn" in text and "backend/gunicorn.conf.py" in text
    assert "PAPER_DATA_DIR=/data" in text
    assert "org.opencontainers.image.source=https://github.com/JohnSpirit/papersys" in text
    assert "org.opencontainers.image.licenses=AGPL-3.0" in text
    assert "COPY LICENSE ./" in text


def test_dockerignore_excludes_env_and_build_artifacts():
    """.dockerignore 排除 .env 与构建产物/依赖目录。"""
    lines = {
        line.strip()
        for line in _read(".dockerignore").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    assert ".env" in lines
    assert "frontend/node_modules/" in lines
    assert "frontend/dist/" in lines


def test_compose_loopback_publish_and_shared_data_volume():
    """Compose：Web 默认 127.0.0.1:8080:5050，MCP HTTP 可选且 127.0.0.1:5051:5051，
    共用 /data 卷，env_file 引用 .env。"""
    text = _read("compose.yaml")
    assert "127.0.0.1:8080:5050" in text
    assert "127.0.0.1:5051:5051" in text
    assert 'profiles: ["mcp"]' in text
    assert text.count("paper-data:/data") >= 2
    assert "env_file:" in text and ".env" in text
    assert "PAPER_ALLOW_REMOTE" in text


def test_compose_mcp_overrides_healthcheck_and_waits_for_web():
    """mcp 覆盖镜像继承的 Web 健康检查（TCP 探真实 MCP 端口 5051），
    并 depends_on web healthy：web 先完成 init_db 初始化共享 /data 卷。"""
    text = _read("compose.yaml")
    web_block = text.split("\n  web:", 1)[1].split("\n  mcp:", 1)[0]
    assert "healthcheck:" not in web_block  # web 用镜像 Dockerfile 的 /api/health 检查
    mcp_block = text.split("\n  mcp:", 1)[1]
    assert "healthcheck:" in mcp_block
    probe = [line for line in mcp_block.splitlines() if "connect_ex" in line]
    assert probe and "5051" in probe[0]
    assert "condition: service_healthy" in mcp_block


def test_compose_files_contain_no_private_paths_or_secrets():
    """Compose 与 Docker 资产不含宿主机私有路径、内网地址或密钥。"""
    forbidden = ("192.168.", "192.0.2.", "internal.example", "/home/example",
                 "~/internal/", "example@", "MINERU_TOKEN=", "LLM_API_KEY=",
                 "FIREWORKS_API_KEY=")
    for path in ("compose.yaml", "compose.mineru.yaml", "Dockerfile",
                 ".dockerignore", "docker/healthcheck.py"):
        text = _read(path)
        for marker in forbidden:
            assert marker not in text, f"{path} leaks {marker}"


def test_mineru_compose_example_is_opt_in_gpu():
    """compose.mineru.yaml 是可选叠加文件：声明 GPU 预留且不改默认 cloud 配置。"""
    text = _read("compose.mineru.yaml")
    assert "nvidia" in text
    assert "capabilities: [gpu]" in text
    assert "PAPER_MINERU_MODE: local" in text
    assert "http://mineru:8000" in text


def test_env_example_has_no_oauth_variables():
    """.env.example 只保留公开版实际支持的密钥与非敏感配置说明。"""
    text = _read(".env.example")
    for marker in ("GITLAB", "PAPER_SESSION_SECRET_KEY", "PAPER_MCP_JWT_SIGNING_KEY",
                   "PAPER_MCP_STORAGE_ENCRYPTION_KEY", "PAPER_GITLAB_STATUS_TOKEN"):
        assert marker not in text, f".env.example still documents {marker}"
    for needed in ("MINERU_TOKEN", "LLM_API_KEY", "FIREWORKS_API_KEY",
                   "SEMANTIC_SCHOLAR_API_KEY", "SCIVERSE_API_TOKEN", "OPENALEX_API_KEY",
                   "PAPER_DATA_DIR", "PAPER_ALLOW_REMOTE", "PAPER_CONFIG_PATH"):
        assert needed in text
