"""Migration：papers 表加 attachment_kind 列 + 存量 SI 附件回填。

背景（2026-10-15）：SI（supporting information，出版商要求作者随正文提交的
补充材料文件）PDF 此前与正文一样作为独立 papers 行入库，并经母论文 DOI 富化
携带母论文的标题/摘要。由此产生四个结构性冲突：enrich 同标题重复预警在
正文行与 SI 行之间必然误报；语义检索按归一化标题去重时 SI 与正文互相挤占
名额；Zotero 导出出现同 DOI 双条目；abstract 重合低分警告对 SI 行恒定为
"possible SI file"（母论文摘要本就不会出现在 SI 文本里）。本列标记后由
下游四处查询排除（enrich_worker / semantic_search / zotero_export）。

回填信号（与 scripts/abstract_audit.py 同口径）：
1. tasks.payload_json.inbox_path 原始投递文件名——dir_name 生成时会丢掉
   "SI_" 前缀，必须用原始文件名跑 filename_suggests_si；
2. pdf_title 元数据以 Supporting/Supplementary (Online) Material/Information
   开头（出版社模板标题）；
3. 已解析文本头部 headline_suggests_supporting_material（严格标题形态判定）
   ——覆盖前两页无印刷标识符也无 SI 字样的老附件（实测存在仅靠此信号
   识别的形态）；回填实测存在宽松判定假阳性（正文首页 SI 链接 boilerplate：
   MIT Press 页脚 / Science 页脚 / ACS 导航栏），故用严格版而非
   looks_like_supporting_material。

幂等：pragma 检查后 ALTER；回填只处理 attachment_kind IS NULL 的行，
重复执行不再改写（人工修正过的值不会被覆盖）。新库由 schema.sql 自动包含。
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from backend.db.conn import get_conn  # noqa: E402

_PDF_TITLE_SI_RE = re.compile(
    r"(?i)^(supporting|supplementary)\s+(online\s+)?(material|information)\b"
)


def detect_si(stem: str, pdf_title: str, parsed_head: str) -> bool:
    """三信号 SI 判定（纯函数，供回填与单测共用）。任一命中即判为 SI。"""
    from backend.pipeline.pdf_classify import (
        filename_suggests_si, headline_suggests_supporting_material,
    )
    if stem and filename_suggests_si(stem):
        return True
    if pdf_title and _PDF_TITLE_SI_RE.match(pdf_title.strip()):
        return True
    if parsed_head and headline_suggests_supporting_material(parsed_head):
        return True
    return False


def _stems_by_paper(conn) -> dict:
    """paper_id -> 原始投递文件名 stem（tasks.payload.inbox_path；同 paper 取最新）。"""
    rows = conn.execute(
        "SELECT paper_id, payload_json FROM tasks"
        " WHERE paper_id IS NOT NULL ORDER BY id"
    ).fetchall()
    stems: dict = {}
    for t in rows:
        try:
            payload = json.loads(t["payload_json"])
        except (TypeError, ValueError):
            continue
        inbox = (payload or {}).get("inbox_path")
        if inbox:
            stems[t["paper_id"]] = Path(inbox).stem
    return stems


def _backfill(conn) -> int:
    from backend.config import resolve_path

    stems = _stems_by_paper(conn)
    rows = conn.execute(
        "SELECT id, pdf_title, parsed_path FROM papers WHERE attachment_kind IS NULL"
    ).fetchall()
    marked = 0
    for r in rows:
        head = ""
        if r["parsed_path"]:
            p = resolve_path(r["parsed_path"])
            if p:
                try:
                    head = Path(p).read_text(encoding="utf-8", errors="ignore")[:5000]
                except OSError:
                    head = ""
        if detect_si(stems.get(r["id"], ""), r["pdf_title"] or "", head):
            conn.execute(
                "UPDATE papers SET attachment_kind = 'si' WHERE id = ?", (r["id"],)
            )
            marked += 1
    return marked


def migrate(conn=None) -> dict:
    own = conn is None
    if own:
        conn = get_conn()
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(papers)").fetchall()}
        added = False
        if "attachment_kind" not in cols:
            conn.execute("ALTER TABLE papers ADD COLUMN attachment_kind TEXT")
            added = True
        marked = _backfill(conn)
        conn.commit()
        return {"added_column": added, "marked_si": marked}
    finally:
        if own:
            conn.close()


if __name__ == "__main__":
    stats = migrate()
    print(f"attachment_kind migration done: {stats}")
