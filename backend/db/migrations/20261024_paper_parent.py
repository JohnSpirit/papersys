"""papers 增加 SI 母论文关联 parent_paper_id，并按 DOI 回填。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from backend.db.conn import get_conn  # noqa: E402


def migrate(conn=None) -> dict:
    own = conn is None
    if own:
        conn = get_conn()
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(papers)").fetchall()}
        added = False
        if "parent_paper_id" not in cols:
            conn.execute("ALTER TABLE papers ADD COLUMN parent_paper_id INTEGER REFERENCES papers(id)")
            added = True
        before = conn.execute(
            "SELECT COUNT(*) FROM papers"
            " WHERE attachment_kind = 'si' AND parent_paper_id IS NOT NULL"
        ).fetchone()[0]
        conn.execute(
            """UPDATE papers SET parent_paper_id = (
                   SELECT MIN(p2.id) FROM papers p2
                   WHERE lower(p2.doi) = lower(papers.doi)
                     AND p2.id != papers.id
                     AND COALESCE(p2.attachment_kind, '') != 'si'
               )
               WHERE attachment_kind = 'si' AND parent_paper_id IS NULL
                 AND doi IS NOT NULL AND doi != ''"""
        )
        after = conn.execute(
            "SELECT COUNT(*) FROM papers"
            " WHERE attachment_kind = 'si' AND parent_paper_id IS NOT NULL"
        ).fetchone()[0]
        conn.commit()
        return {"added_column": added, "linked_si": after - before}
    finally:
        if own:
            conn.close()


if __name__ == "__main__":
    print(f"parent_paper_id migration done: {migrate()}")
