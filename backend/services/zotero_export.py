"""Zotero 可选导出：BibTeX / RIS。

定位：本地 library 是真相源，Zotero 是消费方。导出文件供 Zotero 导入；
配合 linked-file base dir 指向 library_root 可在 Zotero 中直接打开 PDF。

- BibTeX：优先用 papers.bibtex（Crossref content negotiation 的权威条目），
  缺失时从元数据现场合成（conference/journal 条目）。
- RIS：从元数据合成（Zotero 原生支持导入；T2/L2 字段可带 file attachment 相对路径，
  配合 Zotero linked-file base dir 生效）。
"""
import json
import re
from pathlib import Path

from backend.config import CONFIG
from backend.db.conn import get_conn


def _bib_citekey(paper: dict) -> str:
    author = re.sub(r"[^A-Za-z]", "", paper.get("first_author") or "unknown").lower() or "unknown"
    year = paper.get("year") or 0
    kw = re.sub(r"[^A-Za-z0-9]", "", (paper.get("keyword") or "")).lower()[:12] or "untitled"
    return f"{author}{year}_{kw}"


_BIB_ESCAPE_MAP = {
    "\\": "\\textbackslash{}", "&": "\\&", "%": "\\%", "$": "\\$",
    "#": "\\#", "_": "\\_", "{": "\\{", "}": "\\}",
}
_BIB_ESCAPE_RE = re.compile(r"[\\&%$#_{}]")


def _bib_escape(s: str) -> str:
    # 单遍替换：若先替换 \ 再转义 {}，会把 \textbackslash{} 自带的 {} 二次转义腐蚀
    return _BIB_ESCAPE_RE.sub(lambda m: _BIB_ESCAPE_MAP[m.group(0)], s or "")


def synth_bibtex(paper: dict) -> str:
    """从元数据合成一条 BibTeX（无 Crossref 权威条目时的兜底）。"""
    authors_raw = paper.get("authors")
    if isinstance(authors_raw, str):
        try:
            authors_raw = json.loads(authors_raw)
        except (json.JSONDecodeError, TypeError):
            authors_raw = []
    authors = " and ".join(authors_raw) if isinstance(authors_raw, list) and authors_raw else (paper.get("first_author") or "Unknown")

    is_arxiv = bool(paper.get("arxiv_id")) and not paper.get("journal")
    entry_type = "article" if paper.get("journal") else ("misc" if is_arxiv else "article")
    key = _bib_citekey(paper)

    fields = [
        ("author", authors),
        ("title", paper.get("title") or paper.get("keyword") or "Untitled"),
        ("year", str(paper.get("year") or "")),
    ]
    if paper.get("journal"):
        fields.append(("journal", paper["journal"]))
    if paper.get("doi"):
        fields.append(("doi", paper["doi"]))
    if paper.get("arxiv_id"):
        fields.append(("eprint", paper["arxiv_id"]))
        fields.append(("archivePrefix", "arXiv"))
    if paper.get("url"):
        fields.append(("url", paper["url"]))

    lines = [f"@{entry_type}{{{key},"]
    for i, (k, v) in enumerate(fields):
        if v:
            comma = "," if i < len(fields) - 1 else ""
            lines.append(f"  {k} = {{{_bib_escape(str(v))}}}{comma}")
    lines.append("}")
    return "\n".join(lines) + "\n"


# ── RIS ─────────────────────────────────────────────────

_RIS_TYPE_MAP = {
    "article": "JOUR",
    "review": "JOUR",
    "book-chapter": "CHAP",
    "book": "BOOK",
    "dissertation": "THES",
    "report": "RPRT",
    "standard": "STD",
    "proceedings-article": "CONF",
    "preprint": "PRE",
}


def _ris_tag(tag: str, value: str) -> str:
    return f"{tag}  - {value}\n"


def synth_ris(paper: dict, *, include_attachment: bool = True) -> str:
    """从元数据合成一条 RIS 记录。

    include_attachment 时附 `L2  - file:pdf/<相对路径>`：
    Zotero 导入后以 linked-file 形式挂附件（需在 Zotero 中设置
    linked attachment base directory = library_root）。
    """
    out = ""
    pub_type = (paper.get("pub_type") or "").lower()
    ris_type = _RIS_TYPE_MAP.get(pub_type, "JOUR")
    out += _ris_tag("TY", ris_type)
    out += _ris_tag("ID", _bib_citekey(paper))
    out += _ris_tag("TI", paper.get("title") or paper.get("keyword") or "Untitled")

    authors_raw = paper.get("authors")
    if isinstance(authors_raw, str):
        try:
            authors_raw = json.loads(authors_raw)
        except (json.JSONDecodeError, TypeError):
            authors_raw = []
    if isinstance(authors_raw, list):
        for a in authors_raw[:50]:
            out += _ris_tag("AU", str(a))
    elif paper.get("first_author"):
        out += _ris_tag("AU", paper["first_author"])

    if paper.get("year"):
        out += _ris_tag("PY", str(paper["year"]))
    if paper.get("journal"):
        out += _ris_tag("JO", paper["journal"])
        out += _ris_tag("T2", paper["journal"])
    if paper.get("abstract"):
        out += _ris_tag("AB", paper["abstract"])
    if paper.get("doi"):
        out += _ris_tag("DO", paper["doi"])
    if paper.get("arxiv_id"):
        out += _ris_tag("ID", f"arXiv:{paper['arxiv_id']}")
        out += _ris_tag("UR", f"https://arxiv.org/abs/{paper['arxiv_id']}")
    elif paper.get("url"):
        out += _ris_tag("UR", paper["url"])
    elif paper.get("doi"):
        out += _ris_tag("UR", f"https://doi.org/{paper['doi']}")

    kw = paper.get("keyword")
    if kw:
        out += _ris_tag("KW", kw)

    if include_attachment and paper.get("pdf_path") and not str(paper["pdf_path"]).startswith("legacy:"):
        out += _ris_tag("L2", f"file:pdf/{paper['pdf_path']}")

    out += _ris_tag("ER", "")
    return out


# ── 导出入口 ────────────────────────────────────────────


def _ensure_bibtex_column(conn) -> bool:
    """幂等确保 papers.bibtex 列存在（旧库可能未跑过 20260715 migration）。"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(papers)").fetchall()}
    if "bibtex" not in cols:
        conn.execute("ALTER TABLE papers ADD COLUMN bibtex TEXT")
    return True


def _ensure_attachment_kind_column(conn) -> bool:
    """幂等确保 papers.attachment_kind 列存在（旧库可能未跑过 20261015 migration）。"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(papers)").fetchall()}
    if "attachment_kind" not in cols:
        conn.execute("ALTER TABLE papers ADD COLUMN attachment_kind TEXT")
    return True


# SI 附件行不导出：其 title/doi/abstract 取自母论文 DOI 的富化结果，
# 独立导出会在 Zotero 产生同 DOI 双条目（2026-10-15 attachment_kind 迁移）。
_NOT_ATTACHMENT = "COALESCE(attachment_kind, '') != 'si'"


def export_bibtex(
    paper_id: int | None = None,
    year_from: int | None = None,
    year_to: int | None = None,
) -> tuple[str, int]:
    """导出 BibTeX。返回 (bibtex 文本, 条目数)。"""
    conn = get_conn()
    try:
        _ensure_bibtex_column(conn)
        _ensure_attachment_kind_column(conn)
        sql = ("SELECT * FROM papers WHERE (bibtex IS NOT NULL OR title IS NOT NULL)"
               f" AND {_NOT_ATTACHMENT}")
        params: list = []
        if paper_id is not None:
            sql += " AND id = ?"
            params.append(paper_id)
        if year_from is not None:
            sql += " AND year >= ?"
            params.append(year_from)
        if year_to is not None:
            sql += " AND year <= ?"
            params.append(year_to)
        sql += " ORDER BY year, first_author"
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    parts: list[str] = []
    for r in rows:
        p = dict(r)
        bib = p.get("bibtex")
        if bib and bib.strip():
            parts.append(bib.rstrip() + "\n")
        elif p.get("title"):
            parts.append(synth_bibtex(p))
    return "\n".join(parts), len(parts)


def export_ris(
    paper_id: int | None = None,
    year_from: int | None = None,
    year_to: int | None = None,
    include_attachment: bool = True,
) -> tuple[str, int]:
    """导出 RIS。返回 (ris 文本, 条目数)。SI 附件行不导出（同 DOI 双条目）。"""
    sql = f"SELECT * FROM papers WHERE 1=1 AND {_NOT_ATTACHMENT}"
    params: list = []
    if paper_id is not None:
        sql += " AND id = ?"
        params.append(paper_id)
    if year_from is not None:
        sql += " AND year >= ?"
        params.append(year_from)
    if year_to is not None:
        sql += " AND year <= ?"
        params.append(year_to)
    sql += " ORDER BY year, first_author"

    conn = get_conn()
    try:
        _ensure_attachment_kind_column(conn)
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    parts = [synth_ris(dict(r), include_attachment=include_attachment) for r in rows]
    return "".join(parts), len(parts)


def zotero_linked_file_hint() -> str:
    """返回 linked-file 配置提示（library_root 路径）。"""
    return str(CONFIG["paths"]["library_root"])
