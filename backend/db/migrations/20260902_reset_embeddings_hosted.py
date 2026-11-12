"""Migration：清空旧向量，全库待重嵌（嵌入模型切换 doubao-embedding-vision -> qwen3-embedding-8b）。

3.1 起嵌入走托管 qwen3-embedding-8b（1024 维），旧 2048 维向量语义空间不兼容、
混合维度会让余弦检索报错，必须清空重嵌（存量仅 59 条，重嵌成本极小）。
幂等：重复执行无害。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from backend.config import hosted_retrieval_config  # noqa: E402
from backend.db.conn import get_conn  # noqa: E402


def migrate() -> None:
    cfg, err = hosted_retrieval_config()
    if err:
        raise RuntimeError(err)
    model = cfg["embed_model"]
    conn = get_conn()
    try:
        n = conn.execute("DELETE FROM embeddings WHERE model != ? OR dim != ?",
                         (model, cfg["embed_dimensions"])).rowcount
        conn.execute(
            """UPDATE papers SET status_embedded = 'missing'
               WHERE status_embedded = 'ok'
                 AND id NOT IN (SELECT DISTINCT paper_id FROM embeddings)"""
        )
        conn.commit()
        print(f"removed {n} stale vectors (model/dim mismatch); status_embedded reset")
    finally:
        conn.close()


if __name__ == "__main__":
    migrate()
