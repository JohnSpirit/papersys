"""SQLite 连接与 schema 初始化。"""
import sqlite3
from pathlib import Path
from typing import Iterator
from contextlib import contextmanager

from backend.config import CONFIG

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"
DB_PATH = Path(CONFIG["paths"]["db_path"])


def get_conn() -> sqlite3.Connection:
    """获取连接（自动创建目录、启用 row_factory 与 WAL）。"""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def conn_ctx(*, immediate: bool = False) -> Iterator[sqlite3.Connection]:
    """上下文管理器，自动 commit / close；immediate 时在进入事务时取得写锁。"""
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def init_db() -> None:
    """从 schema.sql 初始化表结构（幂等）。"""
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    conn = get_conn()
    try:
        conn.executescript(sql)
    finally:
        conn.close()


if __name__ == "__main__":
    init_db()
    print(f"✅ 数据库已初始化: {DB_PATH}")
