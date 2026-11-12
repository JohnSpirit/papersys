"""inbox 扫描前的轻量检查：年份风险、DOI/文件重复与 SI 判定。"""
from pathlib import Path

from backend.pipeline.pdf_classify import (
    extract_first_page_text,
    extract_pages_text,
    extract_title_from_text,
    extract_identifiers,
    filename_author_year,
    filename_derived_dois,
    filename_suggests_si,
    first_author_from_pdf_metadata,
    guess_year,
    headline_suggests_supporting_material,
    pdfinfo,
)
from backend.services.ingest_dedup import find_duplicate_paper


def preflight_pdf(conn, path: str | Path) -> dict:
    """读取 PDF 元数据和前两页，返回投递前检查信息，不访问外部 API。"""
    path = Path(path)
    info = pdfinfo(str(path))
    first_page = extract_first_page_text(str(path))
    front_text = extract_pages_text(str(path), n_pages=2)
    hint_author, hint_year = filename_author_year(path.stem)
    ids = extract_identifiers(front_text, info.get("pdf_title") or "")
    derived = filename_derived_dois(path.stem)
    doi = (ids["dois"] + derived)[0] if (ids["dois"] or derived) else None
    title = info.get("pdf_title") or extract_title_from_text(first_page)
    year = hint_year or guess_year(title or "", path.stem, first_page)
    si = filename_suggests_si(path.stem) or headline_suggests_supporting_material(front_text)
    duplicate, reason = find_duplicate_paper(
        conn,
        doi=doi,
        file_path=path,
        title=title,
        attachment_kind="si" if si else None,
    )
    return {
        "path": str(path),
        "file_name": path.name,
        "title": title,
        "doi": doi,
        "year": year,
        "author": hint_author or first_author_from_pdf_metadata(info.get("pdf_authors")),
        "year_unknown_risk": year is None and not (ids["dois"] or ids["piis"] or derived),
        "si": si,
        "duplicate_paper_id": duplicate["id"] if duplicate else None,
        "duplicate_dir_name": duplicate["dir_name"] if duplicate else None,
        "duplicate_reason": reason,
        "error": info.get("error"),
    }
