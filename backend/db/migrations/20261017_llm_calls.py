"""Migration：新建 llm_calls 表（LLM 调用明细：日志留存 + 用量统计）。

背景（2026-10-17）：翻译/总结此前只在任务事件流留字符数/耗时一行，provider
返回的 usage（prompt/completion tokens）被丢弃，无任何用量累计。本表由
backend/external/llm.py:LLMClient.chat 每次调用落一行（含失败调用 ok=0），
kind 由调用方经 client.call_meta 注入（summary / translate）。

幂等：CREATE TABLE IF NOT EXISTS。新库由 schema.sql 自动包含。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from backend.db.conn import get_conn  # noqa: E402

_DDL = """CREATE TABLE IF NOT EXISTS llm_calls (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    kind              TEXT,
    paper_id          INTEGER REFERENCES papers(id) ON DELETE SET NULL,
    task_id           INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
    model             TEXT,
    prompt_chars      INTEGER,
    completion_chars  INTEGER,
    prompt_tokens     INTEGER,
    completion_tokens INTEGER,
    latency_ms        INTEGER,
    ok                BOOLEAN DEFAULT 1,
    error             TEXT
)"""


def migrate(conn=None) -> dict:
    own = conn is None
    if own:
        conn = get_conn()
    try:
        conn.execute(_DDL)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_llm_calls_ts ON llm_calls(ts)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_llm_calls_paper ON llm_calls(paper_id)")
        conn.commit()
        n = conn.execute("SELECT COUNT(*) AS c FROM llm_calls").fetchone()["c"]
        return {"created": True, "rows": n}
    finally:
        if own:
            conn.close()


if __name__ == "__main__":
    stats = migrate()
    print(f"llm_calls migration done: {stats}")
