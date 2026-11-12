"""项目版本号：源码读取 pyproject.toml，安装态读取包元数据。"""
import tomllib
from importlib import metadata as importlib_metadata
from pathlib import Path

_PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def get_version() -> str:
    """返回源码或安装包声明的版本，对外用连字符替代本地版本分隔符。"""
    try:
        with _PYPROJECT.open("rb") as f:
            raw = str(tomllib.load(f)["project"]["version"])
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError):
        try:
            raw = importlib_metadata.version("paper-cli")
        except importlib_metadata.PackageNotFoundError:
            return "unknown"
    return raw.replace("+", "-", 1) if "+" in raw else raw
