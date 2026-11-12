"""论文目录维护：在不删除论文及衍生数据的前提下纠正 dir_name。"""
from pathlib import Path
import re
import shutil

from backend.config import library_root, resolve_path
from backend.db.conn import get_conn
from backend.db.repo import get_paper

_DIR_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_]*$")


def _remap_path(value: str | None, old_dir: Path, new_dir: Path) -> str | None:
    if not value or value.startswith("legacy:"):
        return value
    root = library_root().resolve()
    raw = Path(value)
    absolute = raw if raw.is_absolute() else root / raw
    try:
        relative = absolute.relative_to(old_dir)
    except ValueError:
        return value
    mapped = new_dir / relative
    return str(mapped) if raw.is_absolute() else str(mapped.relative_to(root))


def rename_paper_dir(paper_id: int, new_dir_name: str, *,
                     new_year: int | None = None, dry_run: bool = False) -> dict:
    """把论文的 library 目录改成 new_dir_name，并同步数据库路径。

    new_year 提供时把目录移到 library/<new_year>/ 下并同步 papers.year，用于
    纠正 guess_year 抓错出版年的入库错位（实证：2015 年论文被首页引用
    括号里的 2005 归档进 2005 目录）。
    dry_run 只返回计划，不写磁盘或数据库。冲突和非法目录名抛 ValueError；
    文件移动后若数据库更新失败，会尝试把目录和 PDF 文件名移回原状。
    """
    new_dir_name = (new_dir_name or "").strip()
    if not _DIR_NAME_RE.fullmatch(new_dir_name):
        raise ValueError(
            "dir_name 必须以小写字母或数字开头，且只能含小写字母、数字和下划线"
        )
    conn = get_conn()
    try:
        paper = get_paper(conn, paper_id)
    finally:
        conn.close()
    if not paper:
        raise ValueError(f"paper {paper_id} not found")
    old_dir_name = paper["dir_name"]
    if old_dir_name == new_dir_name:
        raise ValueError("new dir_name 与当前值相同")

    conn = get_conn()
    try:
        clash = conn.execute(
            "SELECT id FROM papers WHERE dir_name = ? AND id != ?",
            (new_dir_name, paper_id),
        ).fetchone()
    finally:
        conn.close()
    if clash:
        raise ValueError(f"dir_name 已被 paper {clash['id']} 使用")

    pdf_abs = resolve_path(paper.get("pdf_path"))
    if not pdf_abs:
        raise ValueError(f"paper {paper_id} 的 PDF 不存在，无法定位目录: {paper.get('pdf_path')}")
    old_dir = pdf_abs.parent.resolve()
    root = library_root().resolve()
    if old_dir.name != old_dir_name or old_dir.parent.parent != root:
        raise ValueError(f"PDF 目录形态不符合 library/<year>/<dir_name>: {old_dir}")
    if new_year is not None and not (1900 <= int(new_year) <= 2100):
        raise ValueError(f"new_year 超出合理范围: {new_year}")
    new_dir = ((root / str(int(new_year))) if new_year is not None else old_dir.parent) / new_dir_name
    if new_dir.exists():
        raise ValueError(f"目标目录已存在: {new_dir}")

    updates = {
        "dir_name": new_dir_name,
        "pdf_path": _remap_path(paper.get("pdf_path"), old_dir, new_dir),
        "parsed_path": _remap_path(paper.get("parsed_path"), old_dir, new_dir),
        "summary_path": _remap_path(paper.get("summary_path"), old_dir, new_dir),
    }
    old_pdf_name = f"{old_dir_name}.pdf"
    new_pdf_name = f"{new_dir_name}.pdf"
    if updates["pdf_path"] and Path(updates["pdf_path"]).name == old_pdf_name:
        updates["pdf_path"] = str(Path(updates["pdf_path"]).with_name(new_pdf_name))
    if old_pdf_name != new_pdf_name and (old_dir / new_pdf_name).exists():
        raise ValueError(f"目录内已存在目标 PDF 文件名: {old_dir / new_pdf_name}")
    plan = {
        "paper_id": paper_id,
        "old_dir_name": old_dir_name,
        "new_dir_name": new_dir_name,
        "new_year": int(new_year) if new_year is not None else None,
        "year_before": paper["year"],
        "old_dir": str(old_dir),
        "new_dir": str(new_dir),
        "updates": updates,
        "rename_pdf_file": old_pdf_name != new_pdf_name,
        "dry_run": dry_run,
    }
    if dry_run:
        return plan

    moved = False
    try:
        shutil.move(str(old_dir), str(new_dir))
        moved = True
        old_pdf = new_dir / old_pdf_name
        new_pdf = new_dir / new_pdf_name
        if old_pdf.exists() and old_pdf != new_pdf:
            if new_pdf.exists():
                raise ValueError(f"目标 PDF 已存在: {new_pdf}")
            old_pdf.rename(new_pdf)

        conn = get_conn()
        try:
            conn.execute("BEGIN")
            if new_year is not None:
                conn.execute(
                    "UPDATE papers SET dir_name = ?, pdf_path = ?, parsed_path = ?,"
                    " summary_path = ?, year = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (updates["dir_name"], updates["pdf_path"], updates["parsed_path"],
                     updates["summary_path"], int(new_year), paper_id),
                )
            else:
                conn.execute(
                    "UPDATE papers SET dir_name = ?, pdf_path = ?, parsed_path = ?,"
                    " summary_path = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (updates["dir_name"], updates["pdf_path"], updates["parsed_path"],
                     updates["summary_path"], paper_id),
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
    except Exception:
        if moved:
            current_pdf = new_dir / new_pdf_name
            original_pdf = new_dir / old_pdf_name
            if current_pdf.exists() and current_pdf != original_pdf:
                current_pdf.rename(original_pdf)
            shutil.move(str(new_dir), str(old_dir))
        raise

    return {**plan, "ok": True}


def audit_cited_by_counts(*, fix: bool = False) -> dict:
    """对账 papers.cited_by_count 与 OpenAlex 当前值（按 DOI 批量，每批 50）。

    引入背景（实证）：无 DOI 预印本记录的迁移旧值与 OpenAlex 实际值
    相差三个数量级且被下游照抄——cited_by_count 只在 enrich 时从 OpenAlex
    写入，迁移/历史值从不复核。比对结果四类：

    * match       库内值 = OpenAlex 当前值
    * stale       两值不同（引用随时间自然增长，属预期）
    * suspect     疑似坏值：库内 >= 1000 且 OpenAlex 值 <= 库内值的 1/5
                  （不可解释差距型）
    * oa_missing  OpenAlex 查无此 DOI——通常意味着库内 DOI 本身有错，需人工核对

    fix=True 时把 stale/suspect 改写为 OpenAlex 当前值并写 events（task_id 为
    NULL 的运维事件）；oa_missing 永不改写。无 DOI 的行跳过并计数（无法按 DOI
    精确对账）。
    """
    from backend.external.openalex import OpenAlexClient

    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id, dir_name, doi, cited_by_count FROM papers"
            " WHERE cited_by_count IS NOT NULL AND doi IS NOT NULL AND doi != ''"
            " ORDER BY id"
        ).fetchall()
        no_doi = conn.execute(
            "SELECT COUNT(*) FROM papers"
            " WHERE cited_by_count IS NOT NULL AND (doi IS NULL OR doi = '')"
        ).fetchone()[0]
    finally:
        conn.close()

    oa_by_doi = OpenAlexClient().get_cited_by_dois_batch([r["doi"] for r in rows])

    # 批量未命中的 DOI 逐个回退到 /works/doi: 单发端点：OpenAlex 的 filter=doi:
    # 管道过滤与单发端点行为不一致（实测：批量漏检、单发命中；两处都
    # 404 才是真缺）。回退仍空才判 oa_missing。
    oac = OpenAlexClient()
    for r in rows:
        key = (r["doi"] or "").strip().lower()
        if key in oa_by_doi:
            continue
        work = oac.get_work(r["doi"])
        if work:
            oa_by_doi[key] = {
                "openalex_id": work.get("id", ""),
                "cited_by_count": work.get("cited_by_count", 0),
            }

    items: list[dict] = []
    counts = {"match": 0, "stale": 0, "suspect": 0, "oa_missing": 0}
    fixed = 0
    for r in rows:
        key = (r["doi"] or "").strip().lower()
        oa = oa_by_doi.get(key)
        db_v = r["cited_by_count"]
        if oa is None:
            status, oa_v = "oa_missing", None
        else:
            oa_v = oa["cited_by_count"]
            if db_v == oa_v:
                status = "match"
            elif db_v >= 1000 and oa_v * 5 <= db_v:
                status = "suspect"
            else:
                status = "stale"
        counts[status] += 1
        if status == "match":
            continue
        items.append({
            "paper_id": r["id"], "dir_name": r["dir_name"],
            "doi": r["doi"], "db_cited_by": db_v, "oa_cited_by": oa_v,
            "status": status,
        })
        if status == "oa_missing":
            continue
        if fix:
            conn = get_conn()
            try:
                conn.execute("BEGIN")
                conn.execute(
                    "UPDATE papers SET cited_by_count = ?,"
                    " updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (oa_v, r["id"]),
                )
                level = "warn" if status == "suspect" else "info"
                conn.execute(
                    "INSERT INTO events (paper_id, level, msg) VALUES (?, ?, ?)",
                    (r["id"], level,
                     f"cited_by_count {db_v} -> {oa_v}（paper db audit-citations 对账"
                     f" OpenAlex{'，疑似坏值' if status == 'suspect' else ''}）"),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
            finally:
                conn.close()
            fixed += 1

    return {
        "checked": len(rows), "no_doi_skipped": no_doi,
        "match": counts["match"], "stale": counts["stale"],
        "suspect": counts["suspect"], "oa_missing": counts["oa_missing"],
        "fixed": fixed, "items": items,
    }
