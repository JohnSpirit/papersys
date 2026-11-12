"""status_enrich 迁移的事件顺序与重跑保护。"""
import importlib.util
from pathlib import Path
import sqlite3


def _load_migration():
    path = Path(__file__).resolve().parents[1] / "db" / "migrations" / "20261026_status_enrich.py"
    spec = importlib.util.spec_from_file_location("status_enrich_migration_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _old_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE papers (id INTEGER PRIMARY KEY, attachment_kind TEXT);
        CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            paper_id INTEGER,
            ts TEXT,
            level TEXT,
            msg TEXT
        );
        CREATE TABLE tasks (id INTEGER PRIMARY KEY, kind TEXT, paper_id INTEGER, status TEXT);
    """)
    return conn


def _event(conn, paper_id, level, msg):
    conn.execute(
        "INSERT INTO events (paper_id, ts, level, msg) VALUES (?, ?, ?, ?)",
        (paper_id, "2026-10-26 00:00:00", level, msg),
    )


def test_same_second_events_use_id_order():
    module = _load_migration()
    conn = _old_db()
    try:
        conn.execute("INSERT INTO papers (id) VALUES (1)")
        conn.execute("INSERT INTO papers (id) VALUES (2)")
        _event(conn, 1, "warn", "title mismatch; marking manual_hold")
        _event(conn, 1, "info", "metadata updated")
        _event(conn, 2, "info", "metadata updated")
        _event(conn, 2, "warn", "title mismatch; marking manual_hold")

        result = module.migrate(conn)

        assert result["added_column"] is True
        assert result["backfilled"] == {"ok": 1, "manual_hold": 1}
        rows = conn.execute("SELECT id, status_enrich FROM papers ORDER BY id").fetchall()
        assert [(r["id"], r["status_enrich"]) for r in rows] == [
            (1, "ok"),
            (2, "manual_hold"),
        ]
    finally:
        conn.close()


def test_rerun_preserves_current_status():
    module = _load_migration()
    conn = _old_db()
    try:
        conn.execute("INSERT INTO papers (id) VALUES (1)")
        _event(conn, 1, "info", "metadata updated")
        first = module.migrate(conn)
        assert first["added_column"] is True
        conn.execute("UPDATE papers SET status_enrich = 'error' WHERE id = 1")

        second = module.migrate(conn)

        assert second == {"added_column": False, "backfilled": {}}
        status = conn.execute("SELECT status_enrich FROM papers WHERE id = 1").fetchone()[0]
        assert status == "error"
    finally:
        conn.close()
