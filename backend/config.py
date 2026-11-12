"""配置加载：从 config.toml 和 .env 读取。

3.11.2-open 起不再包含任何 OAuth / 会话 / 账户体系配置。新增环境变量：
  * PAPER_CONFIG_PATH  指定配置文件位置（默认 <项目根>/config.toml）
  * PAPER_DATA_DIR     统一数据根目录（SQLite、缓存、日志、文库、inbox 全部落在其下）
  * PAPER_BIND / PAPER_HOST / PAPER_PORT / PAPER_HEALTH_URL / PAPER_CORS_ORIGINS
  * PAPER_MINERU_MODE / PAPER_MINERU_API_BASE / PAPER_MINERU_LOCAL_API_BASE / PAPER_MINERU_LOCAL_CONTAINER
  * PAPER_LLM_API_URL / PAPER_LLM_MODEL
  * PAPER_RETRIEVAL_EMBED_BASE_URL / PAPER_RETRIEVAL_EMBED_MODEL
  * PAPER_RETRIEVAL_RERANK_BASE_URL / PAPER_RETRIEVAL_RERANK_MODEL
  * PAPER_ALLOW_REMOTE 非回环监听的显式允许开关（见 validate_bind_security）
"""
import os
import sys
import tomllib
from pathlib import Path
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _config_path() -> Path:
    override = os.environ.get("PAPER_CONFIG_PATH")
    if override:
        return Path(override).expanduser()
    source_config = PROJECT_ROOT / "config.toml"
    if source_config.is_file():
        return source_config
    cwd_config = Path.cwd() / "config.toml"
    return cwd_config if cwd_config.is_file() else source_config


CONFIG_PATH = _config_path()


def _expand(p):
    return os.path.expanduser(p) if isinstance(p, str) else p


def _walk_expand(obj):
    if isinstance(obj, dict):
        return {k: _walk_expand(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_walk_expand(v) for v in obj]
    if isinstance(obj, str) and obj.startswith("~"):
        return _expand(obj)
    return obj


def _check_env_permissions(path: Path) -> None:
    import stat
    if not os.path.lexists(path):
        return
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or path.is_symlink():
        raise PermissionError(".env must be a regular file, not a symbolic link")
    if info.st_uid != os.geteuid():
        raise PermissionError(".env must be owned by the current user")
    if stat.S_IMODE(info.st_mode) != 0o600:
        raise PermissionError(".env permissions must be exactly 0600")


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    return default if value is None else value.lower() in {"1", "true", "yes", "on"}


def _apply_data_dir(cfg: dict) -> None:
    """PAPER_DATA_DIR 统一数据根目录：所有运行数据路径落在其下。"""
    root = os.environ.get("PAPER_DATA_DIR")
    if not root:
        return
    root = Path(root).expanduser()
    paths = cfg.setdefault("paths", {})
    paths["data_dir"] = str(root)
    paths["db_path"] = str(root / "papers.db")
    paths["cache_dir"] = str(root / "cache")
    paths["log_dir"] = str(root / "logs")
    paths["library_root"] = str(root / "library")
    paths["inbox_dir"] = str(root / "inbox")


def _apply_env_overrides(cfg: dict) -> None:
    """常用配置项的环境变量覆盖（监听、健康检查、MinerU、LLM、托管检索）。"""
    server = cfg.setdefault("server", {})
    if os.environ.get("PAPER_BIND"):
        server["bind"] = os.environ["PAPER_BIND"]
    if os.environ.get("PAPER_HOST"):
        server["host"] = os.environ["PAPER_HOST"]
    if os.environ.get("PAPER_PORT"):
        server["port"] = int(os.environ["PAPER_PORT"])
    if os.environ.get("PAPER_HEALTH_URL"):
        server["health_url"] = os.environ["PAPER_HEALTH_URL"]
    if os.environ.get("PAPER_CORS_ORIGINS"):
        server["cors_origins"] = [
            o.strip() for o in os.environ["PAPER_CORS_ORIGINS"].split(",") if o.strip()
        ]

    mineru = cfg.setdefault("mineru", {})
    for env_key, cfg_key in (
        ("PAPER_MINERU_MODE", "mode"),
        ("PAPER_MINERU_API_BASE", "api_base"),
        ("PAPER_MINERU_LOCAL_API_BASE", "local_api_base"),
        ("PAPER_MINERU_LOCAL_CONTAINER", "local_container"),
    ):
        if os.environ.get(env_key):
            mineru[cfg_key] = os.environ[env_key]

    llm = cfg.setdefault("llm", {})
    for env_key, cfg_key in (
        ("PAPER_LLM_API_URL", "api_url"),
        ("PAPER_LLM_MODEL", "model"),
    ):
        if os.environ.get(env_key):
            llm[cfg_key] = os.environ[env_key]

    retrieval = cfg.setdefault("retrieval", {})
    for env_key, cfg_key in (
        ("PAPER_RETRIEVAL_EMBED_BASE_URL", "embed_base_url"),
        ("PAPER_RETRIEVAL_EMBED_MODEL", "embed_model"),
        ("PAPER_RETRIEVAL_RERANK_BASE_URL", "rerank_base_url"),
        ("PAPER_RETRIEVAL_RERANK_MODEL", "rerank_model"),
    ):
        if os.environ.get(env_key):
            retrieval[cfg_key] = os.environ[env_key]


def load_config():
    with open(_config_path(), "rb") as f:
        cfg = tomllib.load(f)
    cfg = _walk_expand(cfg)

    env_path = PROJECT_ROOT / ".env"
    _check_env_permissions(env_path)
    if env_path.exists():
        load_dotenv(env_path, override=False)

    _apply_data_dir(cfg)
    _apply_env_overrides(cfg)

    cfg["secrets"] = {
        "mineru_token":           os.environ.get("MINERU_TOKEN", ""),
        "llm_api_key":            os.environ.get("LLM_API_KEY", ""),
        "semantic_scholar_key":   os.environ.get("SEMANTIC_SCHOLAR_API_KEY", ""),
        "sciverse_api_token":     os.environ.get("SCIVERSE_API_TOKEN", ""),
        "openalex_api_key":       os.environ.get("OPENALEX_API_KEY", ""),
        "fireworks_api_key":      os.environ.get("FIREWORKS_API_KEY", ""),
        "bp_embed_api_key":       os.environ.get("BP_EMBED_API_KEY", ""),
        "bp_rerank_api_key":      os.environ.get("BP_RERANK_API_KEY", ""),
        "ark_api_key":            os.environ.get("ARK_API_KEY", ""),
    }
    return cfg


CONFIG = load_config()


def bind_host(value: str) -> str:
    host = str(value).strip()
    if host.startswith("["):
        return host[1:].split("]", 1)[0]
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def validate_bind_security(host: str | None = None) -> None:
    """监听地址安全检查（无认证体系版本）。

    回环地址永远允许。非回环地址会把全部读写接口暴露给网络，必须显式
    PAPER_ALLOW_REMOTE=1 确认；允许时向 stderr 打印警告，提醒真正的访问
    边界应由宿主机回环发布（如 Compose 的 127.0.0.1:8080:5050）或反向
    代理承担。
    """
    configured = host or CONFIG["server"].get("bind") or (
        f"{CONFIG['server'].get('host', '127.0.0.1')}:{CONFIG['server'].get('port', 5050)}"
    )
    address = bind_host(configured).lower()
    loopback = address in {"127.0.0.1", "::1", "localhost"}
    if loopback:
        return
    if not _env_bool("PAPER_ALLOW_REMOTE"):
        raise SystemExit(
            f"refusing to bind non-loopback address {address!r}: 当前版本没有登录体系，"
            "监听非回环地址会把全部接口暴露给网络。确认风险后设置 PAPER_ALLOW_REMOTE=1 "
            "显式允许，并确保宿主机回环发布或防火墙是真正的访问边界。"
        )
    print(
        f"[security] warning: binding non-loopback address {address!r} without "
        "authentication; loopback-only publish or a firewall must be the real "
        "access boundary.",
        file=sys.stderr,
    )


def library_root() -> Path:
    """本地文库真相源根目录。"""
    return Path(CONFIG["paths"]["library_root"])


LEGACY_PREFIX = "legacy:"


def resolve_path(value: str | None) -> Path | None:
    """把 papers 表中的路径字段解析为本地绝对路径。

    存储约定（3.0）：
      * 相对路径 -> 相对 library_root
      * 绝对路径 -> 原样使用（本地路径）
      * "legacy:<绝对路径>" -> 历史部署遗留的外部归档绝对路径；
        本地不存在时视为缺失（仅为兼容历史库保留，新写入不再产生该形态）
      * 三种形态磁盘缺失时一律降级为 None，不抛异常
      * None / 空 -> None
    """
    if not value:
        return None
    if value.startswith(LEGACY_PREFIX):
        p = Path(value[len(LEGACY_PREFIX):])
        return p if p.exists() else None
    p = Path(value)
    if p.is_absolute():
        return p if p.exists() else None
    p = library_root() / p
    return p if p.exists() else None


def check_storage() -> dict:
    """检查本地存储可用性（3.0：无外部依赖，总是可用；仅报告目录状态）。"""
    paths = CONFIG["paths"]
    return {
        "ok": True,
        "library_root": str(paths["library_root"]),
        "library_exists": Path(paths["library_root"]).exists(),
        "inbox": str(paths["inbox_dir"]),
        "inbox_exists": Path(paths["inbox_dir"]).exists(),
    }


def ensure_dirs():
    """启动时确保关键目录存在（全部本地，无挂载语义）。"""
    for k in ("data_dir", "cache_dir", "log_dir", "library_root", "inbox_dir"):
        Path(CONFIG["paths"][k]).mkdir(parents=True, exist_ok=True)


def ensure_inbox_dir() -> Path:
    """运行时确保 inbox 目录存在并返回其 Path（本地目录，无 RuntimeError 分支）。"""
    p = Path(CONFIG["paths"]["inbox_dir"])
    p.mkdir(parents=True, exist_ok=True)
    return p


RETRIEVAL_MODE_KEYS = ("embed_base_url", "embed_model", "rerank_base_url", "rerank_model")


def hosted_retrieval_config() -> tuple[dict, str]:
    """读取 [retrieval] 托管两阶段检索配置。

    返回 (config, "") 或 ({}, error_msg)。四模式变量必须全部设置
    （独立实现，流程设计参考 BrainPilot；paper 不实现本地模式，缺配置
    报错并指明缺失项）。
    """
    r = CONFIG.get("retrieval", {})
    missing = [f"retrieval.{k}" for k in RETRIEVAL_MODE_KEYS if not str(r.get(k, "")).strip()]
    if missing:
        return {}, ("Incomplete hosted config: " + ", ".join(missing)
                    + "（四模式变量必须全部设置，paper 不实现本地模式）")
    return {
        "embed_base_url": str(r["embed_base_url"]).rstrip("/"),
        "embed_model": str(r["embed_model"]).strip(),
        "embed_dimensions": int(r.get("embed_dimensions", 0)) or None,
        "rerank_base_url": str(r["rerank_base_url"]).rstrip("/"),
        "rerank_model": str(r["rerank_model"]).strip(),
        "candidate_multiplier": max(1, int(r.get("candidate_multiplier", 10))),
        "rerank_enabled": bool(r.get("rerank_enabled", True)),
        "rerank_threshold": float(r.get("rerank_threshold", 0.0)),
        "rerank_max_chars": max(200, int(r.get("rerank_max_chars", 4000))),
        "embed_timeout": int(r.get("embed_timeout", 60)),
        "rerank_timeout": int(r.get("rerank_timeout", 120)),
        "embed_max_chars": max(200, int(r.get("embed_max_chars", 6000))),
        "search_max_embeddings": max(1, int(r.get("search_max_embeddings", 200000))),
    }, ""


def retrieval_api_key(kind: str) -> str:
    """托管检索 API key 解析链：BP_<KIND>_API_KEY -> FIREWORKS_API_KEY -> ARK_API_KEY。"""
    s = CONFIG["secrets"]
    return s.get(f"bp_{kind}_api_key") or s.get("fireworks_api_key") or s.get("ark_api_key") or ""
