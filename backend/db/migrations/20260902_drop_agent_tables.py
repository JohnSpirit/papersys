"""Migration：删除 SurveyAgent 两张表（agent_sessions / agent_messages）。

3.1 起 SurveyAgent 移除（功能与外部调研智能体重叠）。
幂等：DROP TABLE IF EXISTS。新库由 schema.sql 保证不再建这两张表。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from backend.db.conn import get_conn  # noqa: E402


def migrate() -> None:
    conn = get_conn()
    try:
        conn.execute("DROP TABLE IF EXISTS agent_messages")
        conn.execute("DROP TABLE IF EXISTS agent_sessions")
        conn.commit()
    finally:
        conn.close()
    print("drop agent_sessions/agent_messages done")


if __name__ == "__main__":
    migrate()
