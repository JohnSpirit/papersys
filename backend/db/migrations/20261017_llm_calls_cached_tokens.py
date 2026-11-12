"""Migration：llm_calls 表加 cached_tokens 列（provider prompt cache 命中量）。

背景（2026-10-17）：DeepSeek 系价格分缓存输入 / 非缓存输入 / 输出三档
（用户实配 deepseek-v4.1-flash：0.04 / 2 / 8 元每百万 tokens）。usage 里
prompt_tokens 含缓存命中部分，命中量在 prompt_cache_hit_tokens 字段回传，
此前未记录、费用会按全价高估（缓存价差 50 倍）。翻译 chunk 调用的
system prompt + 术语表前缀恒定，正是缓存命中率高的场景。

幂等：pragma 检查后 ALTER。新库由 schema.sql 自动包含。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from backend.db.conn import get_conn  # noqa: E402


def migrate(conn=None) -> dict:
    own = conn is None
    if own:
        conn = get_conn()
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(llm_calls)").fetchall()}
        added = False
        if "cached_tokens" not in cols:
            conn.execute("ALTER TABLE llm_calls ADD COLUMN cached_tokens INTEGER")
            added = True
        conn.commit()
        return {"added_column": added}
    finally:
        if own:
            conn.close()


if __name__ == "__main__":
    print(f"llm_calls.cached_tokens migration done: {migrate()}")
