"""入库前去重：DOI 优先，文件大小与 SHA-256 兜底。"""
import hashlib
from pathlib import Path

from backend.config import resolve_path
from backend.db.repo import list_papers_by_doi
from backend.pipeline.pdf_classify import titles_compatible


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def find_duplicate_paper(
    conn,
    *,
    doi: str | None = None,
    file_path: str | Path | None = None,
    title: str | None = None,
    attachment_kind: str | None = None,
) -> tuple[dict | None, str | None]:
    """返回 (已存在论文, 重复原因)；无重复时返回 (None, None)。

    SI 与正文同 DOI 不算重复，允许分别入库；同类别且 DOI、标题兼容时去重。
    文件哈希用于 DOI 缺失或文件名不同的同一 PDF。
    """
    if doi:
        for paper in list_papers_by_doi(conn, doi):
            existing_kind = paper.get("attachment_kind")
            if attachment_kind == "si" and existing_kind != "si":
                continue
            if attachment_kind != "si" and existing_kind == "si":
                continue
            if title and paper.get("title") and not titles_compatible(
                title, paper["title"], min_sim=0.6,
            ):
                continue
            return paper, "doi"

    if file_path:
        source = Path(file_path)
        if source.is_file():
            size = source.stat().st_size
            candidates = conn.execute(
                "SELECT * FROM papers WHERE pdf_size_bytes = ? ORDER BY id", (size,),
            ).fetchall()
            if candidates:
                source_hash = sha256_file(source)
                for row in candidates:
                    existing = resolve_path(row["pdf_path"])
                    if not existing or not existing.is_file():
                        continue
                    try:
                        if sha256_file(existing) == source_hash:
                            return dict(row), "sha256"
                    except OSError:
                        continue
    return None, None


def find_parent_for_si(conn, doi: str | None) -> dict | None:
    """查找 SI 对应的母论文：同 DOI 且未标 SI 的最早一行。"""
    if not doi:
        return None
    for paper in list_papers_by_doi(conn, doi):
        if paper.get("attachment_kind") != "si":
            return paper
    return None
