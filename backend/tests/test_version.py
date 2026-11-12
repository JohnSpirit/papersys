"""Test: 版本上报单一来源（pyproject.toml -> get_version() -> MCP serverInfo / health）。"""
import tomllib
from pathlib import Path

import pytest

from backend.version import get_version


@pytest.fixture
def client(temp_db):
    from backend.app import create_app
    app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


def test_get_version_reads_pyproject():
    """get_version() 必须与 pyproject.toml 声明的版本一致，且不是降级值。

    pyproject 声明 PEP 440 本地版本（如 3.11.2+open）；按 version.py 的
    文档约定，对外显示（健康检查 / MCP serverInfo / 发布名）统一使用
    连字符形式（3.11.2-open）。
    """
    pyproject = Path(__file__).resolve().parent.parent.parent / "pyproject.toml"
    with pyproject.open("rb") as f:
        declared = str(tomllib.load(f)["project"]["version"])
    v = get_version()
    assert v == declared.replace("+", "-")
    assert v and v != "unknown"


def test_get_version_falls_back_when_pyproject_missing(tmp_path, monkeypatch):
    """源码文件与安装包元数据都找不到时返回 "unknown"。"""
    from backend import version as version_mod
    monkeypatch.setattr(version_mod, "_PYPROJECT", tmp_path / "absent.toml")
    monkeypatch.setattr(
        version_mod.importlib_metadata,
        "version",
        lambda _name: (_ for _ in ()).throw(
            version_mod.importlib_metadata.PackageNotFoundError(_name)
        ),
    )
    assert version_mod.get_version() == "unknown"


def test_get_version_uses_installed_package_metadata(tmp_path, monkeypatch):
    """安装态没有源码 pyproject.toml 时从 paper-cli 包元数据读版本。"""
    from backend import version as version_mod
    monkeypatch.setattr(version_mod, "_PYPROJECT", tmp_path / "absent.toml")
    monkeypatch.setattr(version_mod.importlib_metadata, "version", lambda _name: "3.11.2+open")
    assert version_mod.get_version() == "3.11.2-open"


def test_health_reports_project_version(client):
    """/api/health 的 version 字段来自 get_version()，不再硬编码。"""
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.get_json()["version"] == get_version()


def test_mcp_server_identity_carries_project_version():
    """FastMCP 实例的 version 即握手应答 serverInfo.version，必须等于项目版本。"""
    from backend.mcp_server import mcp
    assert str(mcp.version) == get_version()
