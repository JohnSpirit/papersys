"""pytest 共享 fixture：临时 SQLite 数据库，避免污染真实数据目录。

重写 backend.db.conn.DB_PATH 到 tmp_path，并执行 schema.sql 初始化。
"""
import sqlite3
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_data_dirs(tmp_path, monkeypatch):
    """把数据目录指到 tmp_path，防止测试写真实数据目录（快照/报告/library）。"""
    from backend import config as cfg
    monkeypatch.setitem(cfg.CONFIG["paths"], "data_dir", str(tmp_path / "data"))
    monkeypatch.setitem(cfg.CONFIG["paths"], "library_root", str(tmp_path / "library"))
    yield


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """提供独立的 SQLite DB；返回 db_path（Path 对象）。"""
    from backend.db import conn as conn_mod

    db_path = tmp_path / "test_papers.db"
    monkeypatch.setattr(conn_mod, "DB_PATH", db_path)

    # 应用 schema
    schema_sql = (Path(conn_mod.__file__).parent / "schema.sql").read_text(encoding="utf-8")
    raw = sqlite3.connect(db_path)
    try:
        raw.executescript(schema_sql)
        raw.commit()
    finally:
        raw.close()
    return db_path
