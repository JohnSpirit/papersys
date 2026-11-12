"""Migration：papers 表加 url 列（源文档链接，展示/导出用标识符）。

背景（2026-09-16）：无 DOI 的会议论文（OpenReview/ICLR 系）此前没有任何可
导出的来源链接；zotero_export.py 的 BibTeX url 字段与 RIS UR 标签已预留
paper.get("url") 分支，本迁移补列后即激活。写入来源：用户手动录入 +
enrich 自 OpenAlex primary_location.landing_page_url 带回（doi.org 跳转链
不存）；url 不参与反查与校验（enrich 只按 DOI/arxiv_id/标题）。

幂等：pragma 检查后 ALTER。新库由 schema.sql 自动包含。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from backend.db.conn import get_conn  # noqa: E402


def migrate() -> None:
    conn = get_conn()
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(papers)").fetchall()}
        if "url" not in cols:
            conn.execute("ALTER TABLE papers ADD COLUMN url TEXT")
            print("added papers.url")
        else:
            print("papers.url exists, skip")
        conn.commit()
    finally:
        conn.close()
    print("url migration done")


if __name__ == "__main__":
    migrate()
