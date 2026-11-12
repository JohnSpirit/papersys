"""Test: 3.0 存储本地化——resolve_path 路径解析约定 + migrate 幂等性。"""
from pathlib import Path

from backend.config import resolve_path, LEGACY_PREFIX


def test_resolve_legacy_missing_returns_none(tmp_path):
    assert resolve_path(f"legacy:{tmp_path}/nope.pdf") is None


def test_resolve_legacy_existing_returns_path(tmp_path):
    f = tmp_path / "ok.pdf"
    f.write_bytes(b"%PDF-1.4")
    assert resolve_path(f"legacy:{f}") == f


def test_resolve_relative_joins_library_root(monkeypatch, tmp_path):
    from backend import config as cfg
    monkeypatch.setitem(cfg.CONFIG["paths"], "library_root", str(tmp_path))
    f = tmp_path / "2024" / "dir" / "paper.pdf"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"%PDF-1.4")
    out = cfg.resolve_path("2024/dir/paper.pdf")
    assert out == tmp_path / "2024/dir/paper.pdf"


def test_resolve_relative_missing_returns_none(monkeypatch, tmp_path):
    """相对路径磁盘缺失同样降级 None（与 legacy/绝对路径三态一致）。"""
    from backend import config as cfg
    monkeypatch.setitem(cfg.CONFIG["paths"], "library_root", str(tmp_path))
    assert cfg.resolve_path("2024/dir/gone.pdf") is None


def test_resolve_absolute_existing(tmp_path):
    f = tmp_path / "abs.md"
    f.write_text("x")
    assert resolve_path(str(f)) == f


def test_resolve_absolute_missing_returns_none(tmp_path):
    assert resolve_path(str(tmp_path / "gone.md")) is None


def test_resolve_none_and_empty():
    assert resolve_path(None) is None
    assert resolve_path("") is None


def test_localize_paths_migration_idempotent(temp_db, monkeypatch, tmp_path):
    """旧存储绝对路径 -> legacy: 前缀；重复执行不再改写。"""
    from backend.db.conn import get_conn
    from backend import config as cfg

    nas = tmp_path / "nas" / "papers"
    lib = tmp_path / "lib"
    monkeypatch.setitem(cfg.CONFIG["paths"], "nas_base", str(nas))
    monkeypatch.setitem(cfg.CONFIG["paths"], "library_root", str(lib))

    pdf = nas / "pdfs" / "2023" / "a2023_x" / "a2023_x.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"%PDF-1.4")

    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT INTO papers (dir_name, year, first_author, pdf_path, parsed_path, summary_path)"
            " VALUES ('a2023_x', 2023, 'a', ?, NULL, ?)",
            (str(pdf), "/old/summary.md"),
        )
        pid = cur.lastrowid

        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "localize_paths",
            Path(cfg.__file__).parent / "db/migrations/20260901_localize_paths.py",
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        r1 = mod.migrate(conn)
        assert r1["marked_legacy"] == 1
        assert r1["cleared_summary_path"] == 1
        row = conn.execute("SELECT pdf_path, summary_path FROM papers WHERE id=?", (pid,)).fetchone()
        assert row["pdf_path"] == f"{LEGACY_PREFIX}{pdf}"
        assert row["summary_path"] is None

        # 二次执行幂等：不再标记
        r2 = mod.migrate(conn)
        assert r2["marked_legacy"] == 0
        assert r2["cleared_summary_path"] == 0
    finally:
        conn.close()


def test_copy_legacy_files_includes_images_dir(temp_db, monkeypatch, tmp_path):
    """migrate-v3 文件搬运必须递归拷贝 images/ 子目录（防 full.md 图片死链）。"""
    from backend.cli import _copy_legacy_files
    from backend.db.conn import get_conn
    from backend import config as cfg

    nas = tmp_path / "nas"
    lib = tmp_path / "lib"
    monkeypatch.setitem(cfg.CONFIG["paths"], "library_root", str(lib))

    d = "a2023_img"
    pdf_dir = nas / "pdfs" / "2023" / d
    parsed_dir = nas / "parsed" / "2023" / d
    img_dir = parsed_dir / "images"
    img_dir.mkdir(parents=True)
    pdf_dir.mkdir(parents=True)
    (pdf_dir / f"{d}.pdf").write_bytes(b"%PDF-1.4")
    (parsed_dir / "full.md").write_text("![f](images/f1.png)", encoding="utf-8")
    (img_dir / "f1.png").write_bytes(b"\x89PNG")

    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO papers (dir_name, year, first_author, pdf_path, parsed_path)"
            " VALUES (?, 2023, 'a', ?, ?)",
            (d, f"{LEGACY_PREFIX}{pdf_dir / f'{d}.pdf'}",
             f"{LEGACY_PREFIX}{parsed_dir / 'full.md'}"),
        )
        conn.commit()
    finally:
        conn.close()

    moved, missing = _copy_legacy_files(nas, lib, dry_run=False)
    assert moved == 1 and not missing
    dest = lib / "2023" / d
    assert (dest / f"{d}.pdf").exists()
    assert (dest / "full.md").exists()
    assert (dest / "images" / "f1.png").read_bytes() == b"\x89PNG"

    # DB 路径字段改写为相对 library_root 的相对路径（与 ingest/parse worker 约定一致）
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT pdf_path, parsed_path FROM papers WHERE dir_name = ?", (d,)
        ).fetchone()
    finally:
        conn.close()
    assert row["pdf_path"] == str(Path("2023") / d / f"{d}.pdf")
    assert row["parsed_path"] == str(Path("2023") / d / "full.md")

    # 幂等：二次搬运时路径已改写为相对路径，不再重复拷贝
    moved2, missing2 = _copy_legacy_files(nas, lib, dry_run=False)
    assert moved2 == 0 and not missing2
    assert (dest / "images" / "f1.png").read_bytes() == b"\x89PNG"


def test_migrate_v3_dry_run_reports_estimates(temp_db, monkeypatch, tmp_path, capsys):
    """dry-run 不执行标记，但输出有意义的预估（将被标记 legacy 的条数）。"""
    from types import SimpleNamespace
    from backend.cli import cmd_db_migrate_v3
    from backend.db.conn import get_conn
    from backend import config as cfg

    nas = tmp_path / "nas" / "papers"
    monkeypatch.setitem(cfg.CONFIG["paths"], "nas_base", str(nas))
    pdf = nas / "pdfs" / "2023" / "b2023_y" / "b2023_y.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"%PDF-1.4")

    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO papers (dir_name, year, first_author, pdf_path)"
            " VALUES ('b2023_y', 2023, 'b', ?)",
            (str(pdf),),
        )
        conn.commit()
    finally:
        conn.close()

    cmd_db_migrate_v3(SimpleNamespace(dry_run=True))
    out = capsys.readouterr().out
    assert "will_mark_legacy=1" in out

    # dry-run 不落库
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT pdf_path FROM papers WHERE dir_name = 'b2023_y'"
        ).fetchone()
    finally:
        conn.close()
    assert row["pdf_path"] == str(pdf)
