"""仓库（Repository）层：封装常用的论文 SQL 查询，避免 API 层硬编码 SQL。"""
import json
import re
import sqlite3
from typing import Optional

from backend.db.conn import get_conn


PAPER_FIELDS = [
    "id", "dir_name", "year", "first_author", "keyword",
    "title", "authors", "journal", "doi", "arxiv_id", "pmid", "openalex_id", "url",
    "abstract", "cited_by_count", "oa_status", "pub_type", "concepts", "keywords_json",
    "bibtex", "attachment_kind", "parent_paper_id",
    "pdf_path", "parsed_path", "summary_path",
    "pdf_pages", "pdf_size_bytes", "parsed_chars", "mineru_model",
    "status_pdf", "status_parsed", "status_enrich", "status_translated", "status_summary",
    "status_outline", "status_embedded",
    "outline_container_id", "outline_en_id", "outline_zh_id", "outline_summary_id", "outline_url",
    "pdf_title", "pdf_authors", "created_by_user_id",
    "created_at", "updated_at",
]

_PREPRINT_JOURNAL_RE = re.compile(r"biorxiv|arxiv|medrxiv|preprints?\b", re.IGNORECASE)


def is_preprint_record(journal: Optional[str], doi: Optional[str]) -> bool:
    """判断记录是否预印本版本：journal 含预印本服务器名，或 DOI 为 bioRxiv/medRxiv
    前缀（10.1101）。同标题双版本（preprint/正式版）并存时的保留方选择依据
    （3.1.1：同标题正式版与 bioRxiv 预印本均保留，检索
    去重时优先返回正式版）。
    """
    if journal and _PREPRINT_JOURNAL_RE.search(journal):
        return True
    return bool(doi and doi.lower().startswith("10.1101/"))


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    # 把 JSON 字段解码（容错）
    for k in ("authors", "concepts", "keywords_json"):
        v = d.get(k)
        if isinstance(v, str) and v.startswith(("[", "{")):
            try:
                d[k] = json.loads(v)
            except json.JSONDecodeError:
                pass
    return d


def upsert_paper(conn: sqlite3.Connection, data: dict) -> tuple[int, bool]:
    """按 dir_name upsert。返回 (paper_id, is_new)。未指定字段保持原值。"""
    dir_name = data["dir_name"]
    existing = conn.execute(
        "SELECT id FROM papers WHERE dir_name = ?", (dir_name,)
    ).fetchone()

    settable = [f for f in PAPER_FIELDS if f not in ("id", "created_at", "updated_at")]
    values = {f: data.get(f) for f in settable if f in data}

    if existing:
        if not values:
            return existing["id"], False
        sets = ", ".join(f"{k} = ?" for k in values)
        params = list(values.values()) + [dir_name]
        conn.execute(
            f"UPDATE papers SET {sets}, updated_at = CURRENT_TIMESTAMP WHERE dir_name = ?",
            params,
        )
        return existing["id"], False
    else:
        cols = ", ".join(values.keys())
        placeholders = ", ".join("?" for _ in values)
        params = list(values.values())
        cur = conn.execute(
            f"INSERT INTO papers ({cols}) VALUES ({placeholders})", params
        )
        return cur.lastrowid, True


def get_paper(conn: sqlite3.Connection, paper_id: int) -> Optional[dict]:
    row = conn.execute("SELECT * FROM papers WHERE id = ?", (paper_id,)).fetchone()
    return _row_to_dict(row) if row else None


def get_paper_by_dir(conn: sqlite3.Connection, dir_name: str) -> Optional[dict]:
    row = conn.execute("SELECT * FROM papers WHERE dir_name = ?", (dir_name,)).fetchone()
    return _row_to_dict(row) if row else None


def _normalize_doi(s: str) -> str:
    s = (s or "").strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/",
                   "http://dx.doi.org/", "doi.org/", "dx.doi.org/", "doi:"):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    return s


def list_papers_by_doi(conn: sqlite3.Connection, doi: str) -> list[dict]:
    """按 DOI 精确匹配（大小写不敏感，剥 doi.org / doi: 前缀）。
    返回全部命中行（库内存在同一 DOI 重复入库，按 id 升序全返回）。
    lower(doi) 无法使用 idx_papers_doi，全表扫描在数百篇量级下为微秒级。"""
    key = _normalize_doi(doi)
    if not key:
        return []
    rows = conn.execute(
        "SELECT * FROM papers WHERE lower(doi) = ? ORDER BY id", (key,)
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def list_papers_by_dois(conn: sqlite3.Connection, dois: list[str]) -> dict[str, list[dict]]:
    """批量 DOI 查询，返回规范化 DOI -> 论文行列表；一次 SQL 完成。"""
    keys = list(dict.fromkeys(_normalize_doi(d) for d in dois if _normalize_doi(d)))
    if not keys:
        return {}
    placeholders = ", ".join("?" for _ in keys)
    rows = conn.execute(
        f"SELECT * FROM papers WHERE lower(doi) IN ({placeholders}) ORDER BY id",
        keys,
    ).fetchall()
    grouped: dict[str, list[dict]] = {key: [] for key in keys}
    for row in rows:
        grouped.setdefault((row["doi"] or "").lower(), []).append(_row_to_dict(row))
    return grouped


def get_papers_by_ids(conn: sqlite3.Connection, paper_ids: list[int]) -> list[dict]:
    """批量按 id 查询，按 id 升序返回。"""
    ids = list(dict.fromkeys(int(pid) for pid in paper_ids))
    if not ids:
        return []
    placeholders = ", ".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT * FROM papers WHERE id IN ({placeholders}) ORDER BY id", ids,
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def _papers_where(
    conn: sqlite3.Connection,
    q: Optional[str] = None,
    author: Optional[str] = None,
    fulltext: Optional[str] = None,
    year_from: Optional[int] = None,
    year_to: Optional[int] = None,
    status_filter: Optional[dict] = None,
) -> tuple[str, list]:
    """list_papers / count_papers 共用的 WHERE 片段构造。两处过滤条件必须完全
    一致，否则分页总数（count_papers）与列表内容（list_papers）对不上。"""
    sql = ""
    params: list = []

    if q:
        # 逐词 AND 的 8 字段 OR 组：整段连续 LIKE 会漏掉隔词标题（q="synergistic
        # coding columnar" 匹配不到 "Synergistic Coding of Visual Information in
        # Columnar..."，实测）。单词 q 与旧行为完全一致。
        # authors 列存 JSON 数组字符串，子串匹配可命中任意合作者姓名（实测缺陷：
        # 非第一作者检索返回 0）。replace 臂消除标题千分位逗号漏配
        # （q="1024" 匹配不到 "1,024-channel"，LIKE 与 FTS5 分词两条路径都漏）。
        # doi 臂：DOI 在库时元数据直接命中，不再走 FTS 回退错配到参考文献里
        # 含该 DOI 的其他论文；LIKE 对 ASCII 默认大小写不敏感，覆盖大写 DOI。
        for tok in q.split():
            sql += (" AND (title LIKE ? OR dir_name LIKE ? OR first_author LIKE ? OR keyword LIKE ?"
                    " OR abstract LIKE ? OR authors LIKE ? OR replace(title, ',', '') LIKE ?"
                    " OR doi LIKE ?)")
            params += [f"%{tok}%"] * 8
    if author:
        sql += " AND (first_author LIKE ? OR authors LIKE ?)"
        params += [f"%{author}%", f"%{author}%"]
    if fulltext:
        fts_ids = _fts_search(conn, fulltext)
        if fts_ids is not None:
            if fts_ids:
                placeholders = ",".join("?" * len(fts_ids))
                sql += f" AND papers.id IN ({placeholders})"
                params += fts_ids
            else:
                sql += " AND 0"
    if year_from is not None:
        sql += " AND year >= ?"
        params.append(year_from)
    if year_to is not None:
        sql += " AND year <= ?"
        params.append(year_to)

    if status_filter:
        for col, val in status_filter.items():
            if col not in {
                "status_pdf", "status_parsed", "status_enrich", "status_translated",
                "status_summary", "status_outline", "status_embedded",
            }:
                continue
            sql += f" AND {col} = ?"
            params.append(val)

    return sql, params


def list_papers(
    conn: sqlite3.Connection,
    q: Optional[str] = None,
    author: Optional[str] = None,
    fulltext: Optional[str] = None,
    year_from: Optional[int] = None,
    year_to: Optional[int] = None,
    status_filter: Optional[dict] = None,
    limit: int = 200,
    offset: int = 0,
    order_by: str = "year_desc",
) -> list[dict]:
    where_sql, params = _papers_where(
        conn, q=q, author=author, fulltext=fulltext,
        year_from=year_from, year_to=year_to, status_filter=status_filter,
    )

    order_map = {
        "year_desc":  " ORDER BY year DESC, first_author ASC",
        "year_asc":   " ORDER BY year ASC,  first_author ASC",
        "title_asc":  " ORDER BY title ASC",
        "updated":    " ORDER BY updated_at DESC",
    }
    sql = "SELECT * FROM papers WHERE 1=1" + where_sql
    sql += order_map.get(order_by, order_map["year_desc"])
    sql += " LIMIT ? OFFSET ?"
    params += [limit, offset]

    return [_row_to_dict(r) for r in conn.execute(sql, params).fetchall()]


def count_papers(
    conn: sqlite3.Connection,
    q: Optional[str] = None,
    author: Optional[str] = None,
    fulltext: Optional[str] = None,
    year_from: Optional[int] = None,
    year_to: Optional[int] = None,
    status_filter: Optional[dict] = None,
) -> int:
    """与 list_papers 同过滤条件的总数（分页用；过滤条件由 _papers_where 统一构造）。"""
    where_sql, params = _papers_where(
        conn, q=q, author=author, fulltext=fulltext,
        year_from=year_from, year_to=year_to, status_filter=status_filter,
    )
    return conn.execute(
        "SELECT COUNT(*) FROM papers WHERE 1=1" + where_sql, params
    ).fetchone()[0]


def stats(conn: sqlite3.Connection) -> dict:
    g = lambda where: conn.execute(f"SELECT COUNT(*) FROM papers WHERE {where}").fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
    year_range = conn.execute("SELECT MIN(year), MAX(year) FROM papers").fetchone()

    by_year_rows = conn.execute(
        "SELECT year, COUNT(*) as n FROM papers GROUP BY year ORDER BY year"
    ).fetchall()

    return {
        "total":              total,
        "with_pdf":           g("status_pdf = 'ok'"),
        "with_parsed":        g("status_parsed = 'ok'"),
        "with_enrich":        g("status_enrich = 'ok'"),
        "with_summary":       g("status_summary = 'ok'"),
        "with_translation":   g("status_translated = 'ok'"),
        "with_outline":       g("status_outline = 'ok'"),
        "with_embedded":      g("status_embedded = 'ok'"),
        "manual_hold":        g("'manual_hold' IN (status_pdf, status_parsed, status_enrich, status_translated, status_summary, status_outline, status_embedded)"),
        "errors":             g("'error' IN (status_pdf, status_parsed, status_enrich, status_translated, status_summary, status_outline, status_embedded)"),
        "year_range":         [year_range[0], year_range[1]],
        "by_year":            [{"year": r[0], "count": r[1]} for r in by_year_rows],
    }


# ── FTS5 全文搜索 ──────────────────────────────────────────

def _fts_match(conn: sqlite3.Connection, match_query: str) -> Optional[list[int]]:
    """单次 FTS5 MATCH（bm25 升序 = 相关度降序）；语法错误返回 None。"""
    try:
        rows = conn.execute(
            "SELECT paper_id FROM papers_fts WHERE papers_fts MATCH ?"
            " ORDER BY bm25(papers_fts)", (match_query,),
        ).fetchall()
        return [r[0] for r in rows]
    except sqlite3.OperationalError:
        return None


def _fts_search(conn: sqlite3.Connection, query: str) -> Optional[list[int]]:
    """FTS5 MATCH 查询，返回 paper_id 列表（bm25 相关度降序：越小越相关）。

    语义：FTS5 空格分词是隐式 AND——短查询保持严格 AND；AND 落空且词数 > 3
    时降级为 OR + bm25 排序（七八个词的全文查询 AND 必然零命中，实测；
    bm25 让命中词多者靠前）。语法错误时回退到逐词引号包裹 /
    LIKE。

    返回 None 表示 FTS 搜索完全不可用（调用方应跳过 fulltext 过滤）。
    排序供 search_library 的 fulltext 路径重排使用（SQL IN 列表不保序）；
    实测回退结果按 year DESC 会把目标论文埋到较新论文之后。
    """
    tokens = query.replace('"', " ").split()
    res = _fts_match(conn, query)
    if res is None:
        # 逐词引号包裹：中和 - : ( ) 等查询语法字符（连字符会被 FTS5 解析成列过滤，
        # 报 "no such column"），词间保持 AND、词内连字符仍按相邻短语匹配
        # （"high-density" = high 与 density 相邻）。整串引号包裹会把多词查询变成
        # 短语匹配，多词带连字符时必然空结果（S2 实录 q:"Duan high-density probe"）。
        quoted = " ".join(f'"{t}"' for t in tokens)
        res = _fts_match(conn, quoted)
    if res is None:
        # FTS 表不可用：LIKE 兜底
        try:
            like = f"%{query}%"
            rows = conn.execute(
                "SELECT paper_id FROM papers_fts WHERE title LIKE ? OR abstract LIKE ? OR parsed_text LIKE ?",
                (like, like, like),
            ).fetchall()
            return [r[0] for r in rows]
        except sqlite3.OperationalError:
            return None
    # FTS 可用但 AND 落空：长查询降级 OR
    if not res and len(tokens) > 3:
        or_res = _fts_match(conn, " OR ".join(f'"{t}"' for t in tokens))
        if or_res:
            res = or_res
    return res


def update_fts(
    conn: sqlite3.Connection,
    paper_id: int,
    title: str = "",
    abstract: str = "",
    parsed_text: Optional[str] = None,
) -> None:
    """更新 papers_fts 索引行。parsed_text=None 时保留已有值。

    由 ingest_worker / parse_worker / enrich_worker 在 upsert_paper 后调用。
    """
    if parsed_text is None:
        row = conn.execute(
            "SELECT parsed_text FROM papers_fts WHERE paper_id = ?", (paper_id,)
        ).fetchone()
        parsed_text = row["parsed_text"] if row else ""
    conn.execute("DELETE FROM papers_fts WHERE paper_id = ?", (paper_id,))
    conn.execute(
        "INSERT INTO papers_fts(paper_id, title, abstract, parsed_text) VALUES (?, ?, ?, ?)",
        (paper_id, title or "", abstract or "", parsed_text or ""),
    )
