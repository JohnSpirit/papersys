"""paper CLI — 文献调研系统统一命令行入口(运维外壳)。

定位见 docs/adr/0001-cli-ops-shell-no-flask-decoupling.md:
仅承担服务启停 / 任务查询 / 诊断 / 入队触发,不独立执行 worker handler。
worker 命令一律 tasks_repo.create_task 入队,依赖 Flask scheduler 领取。
"""
import argparse
import os
import shutil
import signal
import subprocess
import sys
import time
import tomllib
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FLASK_PID = Path("/tmp/flask.pid")
FLASK_LOG = Path("/tmp/flask.log")
FRONTEND_PID = Path("/tmp/vite.pid")
FRONTEND_LOG = Path("/tmp/vite.log")


# ── helpers ────────────────────────────────────────────────

def _version() -> str:
    pyproject = PROJECT_ROOT / "pyproject.toml"
    if pyproject.exists():
        with open(pyproject, "rb") as f:
            return tomllib.load(f)["project"]["version"]
    return "unknown"


def _pid_alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError):
        return False


def _kill_pid(pid, timeout: int = 3) -> None:
    pid = int(pid)
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    for _ in range(timeout):
        time.sleep(1)
        if not _pid_alive(pid):
            return
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _health_url(host=None, port=None) -> str:
    from backend.config import CONFIG
    server = CONFIG["server"]
    if host is not None or port is not None:
        host = host or server["host"]
        port = port or server["port"]
        return f"http://{host}:{port}/api/health"
    if server.get("health_url"):
        return server["health_url"]
    if server.get("bind"):
        bind = server["bind"]
        bind_host = bind.rsplit(":", 1)[0] if bind.count(":") == 1 else bind
        return f"http://{bind_host}/api/health" if ":" not in bind else f"http://{bind}/api/health"
    return f"http://{server['host']}:{server['port']}/api/health"


def _health(host=None, port=None, timeout: float = 2) -> bool:
    try:
        with urllib.request.urlopen(_health_url(host, port), timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def _check_flask(force=False) -> None:
    """worker/inbox 入队前检测 Flask 存活;失败报错(除非 --force)。"""
    if force:
        return
    if not _health():
        print(f"⚠️  Flask 未运行({_health_url()} 无响应)。", file=sys.stderr)
        print("   先 `paper serve --detach`,或加 --force 跳过检测(将成僵尸 task)。", file=sys.stderr)
        sys.exit(1)


def _resolve_paper_ids(args) -> list[int]:
    if args.all:
        return _query_paper_ids(args.status)
    if not args.ids:
        print("⚠️  需指定 paper id 或 --all", file=sys.stderr)
        sys.exit(1)
    return list(args.ids)


def _query_paper_ids(status_filter=None) -> list[int]:
    from backend.db.conn import get_conn
    sql = "SELECT id FROM papers"
    params: list = []
    if status_filter:
        if "=" not in status_filter:
            print(f"⚠️  --status 格式应为 <field>=<value>(如 parsed=missing)", file=sys.stderr)
            sys.exit(1)
        field, value = status_filter.split("=", 1)
        valid = {"pdf", "parsed", "enrich", "translated", "summary", "embedded"}
        if field not in valid:
            print(f"⚠️  --status field 仅支持 {sorted(valid)},got {field}", file=sys.stderr)
            sys.exit(1)
        sql += f" WHERE status_{field} = ?"
        params.append(value)
    sql += " ORDER BY id ASC"
    conn = get_conn()
    try:
        return [r["id"] for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _enqueue(kind: str, payload: dict, paper_id=None) -> int:
    from backend.db import tasks_repo
    from backend.db.conn import conn_ctx
    with conn_ctx(immediate=True) as conn:
        task_id = tasks_repo.create_task(kind=kind, payload=payload, paper_id=paper_id, conn=conn)
    return task_id


def _filter_existing_ids(ids: list[int]) -> tuple[list[int], list[int]]:
    """校验 paper id 存在,返回 (existing, missing)。--all 模式已保证存在,显式 ids 需校验。"""
    from backend.db.conn import get_conn
    from backend.db.repo import get_paper
    existing, missing = [], []
    conn = get_conn()
    try:
        for pid in ids:
            (existing if get_paper(conn, pid) else missing).append(pid)
    finally:
        conn.close()
    return existing, missing


def _mask(s: str) -> str:
    if not s:
        return ""
    if len(s) <= 8:
        return "•" * len(s)
    return s[:4] + "•" * (len(s) - 8) + s[-4:]


def _fmt_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def _count_files(p: Path) -> int:
    return sum(1 for f in p.rglob("*") if f.is_file())


# ── serve ──────────────────────────────────────────────────

def cmd_serve(args):
    from backend.config import CONFIG
    cfg = CONFIG["server"]
    host = args.host or cfg["host"]
    port = args.port or cfg["port"]
    debug = args.debug if args.debug is not None else cfg.get("debug", False)

    if args.detach:
        _serve_detach(host, port, debug, force=args.force)
        return

    from backend.app import app
    app.run(host=host, port=port, debug=debug, threaded=True, use_reloader=False)


def _serve_detach(host, port, debug, force=False):
    if FLASK_PID.exists():
        old = FLASK_PID.read_text().strip()
        if _pid_alive(old):
            if not force:
                print(f"⚠️  Flask 已在运行 (pid={old})。用 --force 重启。", file=sys.stderr)
                sys.exit(1)
            _kill_pid(old)
            FLASK_PID.unlink(missing_ok=True)

    env = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT),
           "PAPER_HOST": host, "PAPER_PORT": str(port),
           "PAPER_DEBUG": "1" if debug else "0"}
    log_fp = open(FLASK_LOG, "ab")
    proc = subprocess.Popen(
        [sys.executable, "-m", "backend.app"],
        env=env, cwd=str(PROJECT_ROOT),
        stdout=log_fp, stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    log_fp.close()

    for _ in range(15):
        time.sleep(1)
        if _health(host, port):
            FLASK_PID.write_text(str(proc.pid))
            print(f"✅ started pid={proc.pid}  (http://{host}:{port}, log={FLASK_LOG})")
            return
    print(f"❌ 启动失败;查 {FLASK_LOG}", file=sys.stderr)
    tail = subprocess.run(["tail", "-20", str(FLASK_LOG)], capture_output=True, text=True)
    print(tail.stdout, file=sys.stderr)
    sys.exit(1)


def cmd_serve_stop(args):
    if not FLASK_PID.exists():
        print("Flask 未运行(pid 文件不存在)")
        return
    pid = FLASK_PID.read_text().strip()
    if not _pid_alive(pid):
        print(f"Flask 未运行(pid={pid} 已死,清理 pid 文件)")
        FLASK_PID.unlink(missing_ok=True)
        return
    _kill_pid(pid)
    FLASK_PID.unlink(missing_ok=True)
    print(f"✅ stopped pid={pid}")


def cmd_serve_status(args):
    pid = FLASK_PID.read_text().strip() if FLASK_PID.exists() else ""
    alive = _pid_alive(pid) if pid else False
    healthy = _health() if alive else False
    state = "running" if healthy else ("alive(unhealthy)" if alive else "stopped")
    print(f"state: {state}  pid: {pid or '-'}  url: {_health_url()}  log: {FLASK_LOG}")


def cmd_serve_logs(args):
    if not FLASK_LOG.exists():
        print(f"日志不存在: {FLASK_LOG}")
        return
    if args.follow:
        os.execvp("tail", ["tail", "-f", str(FLASK_LOG)])
    else:
        os.execvp("tail", ["tail", "-50", str(FLASK_LOG)])


# ── frontend ───────────────────────────────────────────────

def cmd_frontend(args):
    if args.detach:
        _frontend_detach(force=args.force)
        return
    frontend_dir = PROJECT_ROOT / "frontend"
    subprocess.run(["npm", "run", "dev"], cwd=str(frontend_dir))


def _frontend_detach(force=False):
    if FRONTEND_PID.exists():
        old = FRONTEND_PID.read_text().strip()
        if _pid_alive(old):
            if not force:
                print(f"⚠️  frontend 已在运行 (pid={old})。用 --force 重启。", file=sys.stderr)
                sys.exit(1)
            _kill_pid(old)
            FRONTEND_PID.unlink(missing_ok=True)
    frontend_dir = PROJECT_ROOT / "frontend"
    log_fp = open(FRONTEND_LOG, "ab")
    proc = subprocess.Popen(
        ["npm", "run", "dev"],
        cwd=str(frontend_dir),
        stdout=log_fp, stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    log_fp.close()
    time.sleep(3)
    if _pid_alive(proc.pid):
        FRONTEND_PID.write_text(str(proc.pid))
        print(f"✅ started frontend pid={proc.pid}  (log={FRONTEND_LOG})")
    else:
        print(f"❌ 启动失败;查 {FRONTEND_LOG}", file=sys.stderr)
        sys.exit(1)


def cmd_frontend_stop(args):
    if not FRONTEND_PID.exists():
        print("frontend 未运行(pid 文件不存在)")
        return
    pid = FRONTEND_PID.read_text().strip()
    if not _pid_alive(pid):
        print(f"frontend 未运行(pid={pid} 已死,清理)")
        FRONTEND_PID.unlink(missing_ok=True)
        return
    _kill_pid(pid)
    FRONTEND_PID.unlink(missing_ok=True)
    print(f"✅ stopped frontend pid={pid}")


def cmd_frontend_status(args):
    pid = FRONTEND_PID.read_text().strip() if FRONTEND_PID.exists() else ""
    alive = _pid_alive(pid) if pid else False
    print(f"state: {'running' if alive else 'stopped'}  pid: {pid or '-'}  log: {FRONTEND_LOG}")


def cmd_frontend_logs(args):
    if not FRONTEND_LOG.exists():
        print(f"日志不存在: {FRONTEND_LOG}")
        return
    if args.follow:
        os.execvp("tail", ["tail", "-f", str(FRONTEND_LOG)])
    else:
        os.execvp("tail", ["tail", "-50", str(FRONTEND_LOG)])


# ── db ─────────────────────────────────────────────────────

def cmd_db_init(args):
    from backend.db.conn import init_db
    init_db()
    print("✅ 数据库已初始化")


def cmd_db_path(args):
    from backend.db.conn import DB_PATH
    print(DB_PATH)


def cmd_db_stats(args):
    from backend.db.conn import get_conn
    from backend.db.repo import stats
    conn = get_conn()
    try:
        s = stats(conn)
    finally:
        conn.close()
    print(f"总数:         {s['total']}")
    print(f"PDF:          {s['with_pdf']}")
    print(f"解析:         {s['with_parsed']}")
    print(f"翻译:         {s['with_translation']}")
    print(f"总结:         {s['with_summary']}")

    print(f"manual_hold:  {s['manual_hold']}")
    print(f"errors:       {s['errors']}")
    yr = s["year_range"]
    print(f"年份:         {yr[0]}-{yr[1]}")
    print("按年:")
    for row in s["by_year"]:
        print(f"  {row['year']}: {row['count']}")


def cmd_db_shell(args):
    from backend.db.conn import DB_PATH
    os.execvp("sqlite3", ["sqlite3", str(DB_PATH)])


def cmd_db_migrate_v3(args):
    """v3 存储迁移：①路径字段 legacy 化（幂等）；②旧存储恢复后搬运文件到本地 library。

    ② 为可选步骤：--dry-run 只统计。实际搬运要求旧存储已挂载（nas_base 可访问）。
    """
    from backend.config import CONFIG, library_root, LEGACY_PREFIX
    from backend.db.conn import get_conn

    conn = get_conn()
    # 复用 migration 模块的路径标记逻辑
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "localize_paths",
        PROJECT_ROOT / "backend/db/migrations/20260901_localize_paths.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    result = mod.migrate(conn) if not args.dry_run else {"dry_run": True}
    conn.close()

    if args.dry_run:
        nas_prefix = str(Path(CONFIG["paths"].get("nas_base", "") or "").expanduser())
        conn = get_conn()
        try:
            rows = conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
            will_legacy = 0
            if nas_prefix:
                will_legacy = conn.execute(
                    "SELECT COUNT(*) FROM papers WHERE "
                    "(pdf_path LIKE ? AND pdf_path NOT LIKE ?) OR "
                    "(parsed_path LIKE ? AND parsed_path NOT LIKE ?)",
                    (f"{nas_prefix}/%", f"{LEGACY_PREFIX}%",
                     f"{nas_prefix}/%", f"{LEGACY_PREFIX}%"),
                ).fetchone()[0]
            legacy = conn.execute(
                "SELECT COUNT(*) FROM papers WHERE pdf_path LIKE ? OR parsed_path LIKE ?",
                (f"{LEGACY_PREFIX}%", f"{LEGACY_PREFIX}%"),
            ).fetchone()[0]
        finally:
            conn.close()
        print(f"[dry-run] papers={rows} will_mark_legacy={will_legacy} "
              f"already_legacy={legacy}（旧存储挂载后将搬运这些论文的文件）")
        return

    print("✅ 路径 legacy 标记完成:")
    for k, v in result.items():
        print(f"  {k:22} {v}")

    # 文件搬运（旧存储 -> library）
    nas_base = Path(CONFIG["paths"].get("nas_base", "") or "").expanduser()
    if not nas_base.exists():
        print(f"ℹ️  旧存储未挂载（{nas_base}），跳过文件搬运。")
        print("   旧存储恢复后重新执行 `paper db migrate-v3` 完成搬运。")
        return

    moved, missing = _copy_legacy_files(nas_base, library_root(), dry_run=False)
    print(f"✅ 文件搬运完成: {moved} 篇已复制到 library；{len(missing)} 篇源文件缺失")
    if missing:
        for n in missing[:10]:
            print(f"   - {n}")


def _copy_legacy_files(nas_base: Path, root: Path, dry_run: bool):
    """把 legacy: 前缀论文的 pdfs/ 与 parsed/ 内容从旧存储拷到本地 library。

    布局映射：{nas}/pdfs/{year}/{dir}/  ->  {library}/{year}/{dir}/
             {nas}/parsed/{year}/{dir}/ ->  {library}/{year}/{dir}/ （合并）
    递归拷贝整个源目录（含 MinerU 产物的 images/ 子目录，防 full.md 图片死链）。
    幂等：目标文件已存在则跳过（不覆盖）。
    """
    from backend.config import LEGACY_PREFIX
    from backend.db.conn import get_conn

    root = root.resolve()
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id, dir_name, year, pdf_path, parsed_path FROM papers"
        ).fetchall()
    finally:
        conn.close()

    moved, missing = 0, []
    updates: list[tuple[int, str | None, str | None]] = []
    for r in rows:
        pid, dir_name, year = r["id"], r["dir_name"], r["year"]
        year_dir = str(year) if year else "unknown"
        dest_dir = root / year_dir / dir_name
        copied_any = False

        for col in ("pdf_path", "parsed_path"):
            v = r[col]
            if not v or not v.startswith(LEGACY_PREFIX):
                continue
            src = Path(v[len(LEGACY_PREFIX):])
            if not src.exists():
                continue
            dest_dir.mkdir(parents=True, exist_ok=True)
            if not dry_run:
                for f in src.parent.rglob("*"):
                    if f.is_file():
                        target = dest_dir / f.relative_to(src.parent)
                        if not target.exists():
                            target.parent.mkdir(parents=True, exist_ok=True)
                            target.write_bytes(f.read_bytes())
            copied_any = True

        if copied_any:
            # 重写路径字段为相对路径（相对 library_root，与 ingest/parse worker 约定一致）
            pdf_rel = str((dest_dir / f"{dir_name}.pdf").relative_to(root)) \
                if (dest_dir / f"{dir_name}.pdf").exists() else None
            parsed_rel = str((dest_dir / "full.md").relative_to(root)) \
                if (dest_dir / "full.md").exists() else None
            updates.append((pid, pdf_rel, parsed_rel))
            moved += 1
        else:
            if (r["pdf_path"] and r["pdf_path"].startswith(LEGACY_PREFIX)) or \
               (r["parsed_path"] and r["parsed_path"].startswith(LEGACY_PREFIX)):
                missing.append(dir_name)

    if updates and not dry_run:
        conn = get_conn()
        try:
            for pid, pdf_rel, parsed_rel in updates:
                if pdf_rel:
                    conn.execute(
                        "UPDATE papers SET pdf_path = ? WHERE id = ?", (pdf_rel, pid))
                if parsed_rel:
                    conn.execute(
                        "UPDATE papers SET parsed_path = ? WHERE id = ?", (parsed_rel, pid))
            conn.commit()
        finally:
            conn.close()
    return moved, missing


def cmd_db_rename(args):
    from backend.services.paper_maintenance import rename_paper_dir
    try:
        result = rename_paper_dir(
            args.paper_id, args.dir_name,
            new_year=getattr(args, "year", None), dry_run=args.dry_run,
        )
    except (ValueError, OSError) as e:
        print(f"❌ 重命名失败: {e}", file=sys.stderr)
        sys.exit(1)
    if args.dry_run:
        print(f"dry-run: paper {args.paper_id} 将执行以下变更（未写入）")
        print(f"  目录: {result['old_dir']} -> {result['new_dir']}")
        if result.get("new_year") is not None:
            print(f"  year: {result['year_before']} -> {result['new_year']}")
        for field in ("dir_name", "pdf_path", "parsed_path", "summary_path"):
            print(f"  {field}: {result['updates'][field]}")
        return
    print(f"✅ paper {args.paper_id} 已重命名为 {result['new_dir_name']}")
    print(f"   目录: {result['new_dir']}")
    if result.get("new_year") is not None:
        print(f"   year: {result['year_before']} -> {result['new_year']}")
    print("   paper_id 与 translations/summaries/embeddings/annotations/doc_revisions 均保留")


def cmd_db_audit_citations(args):
    from backend.services.paper_maintenance import audit_cited_by_counts
    try:
        result = audit_cited_by_counts(fix=args.fix)
    except Exception as e:  # noqa: BLE001
        print(f"❌ 对账失败: {e}", file=sys.stderr)
        sys.exit(1)
    print(f"对账完成：有 DOI 且存被引值的 {result['checked']} 行"
          f"（另有 {result['no_doi_skipped']} 行无 DOI 跳过）")
    print(f"  一致 {result['match']} | 过期 {result['stale']} | 疑似坏值 {result['suspect']}"
          f" | OpenAlex 无记录 {result['oa_missing']} | 已修正 {result['fixed']}")
    if result["items"]:
        print("\n明细（db -> OpenAlex）：")
        for it in result["items"]:
            mark = {"match": "=", "stale": "~", "suspect": "!!", "oa_missing": "?"}[it["status"]]
            print(f"  [{mark}] #{it['paper_id']} {it['dir_name']}: "
                  f"{it['db_cited_by']} -> {it['oa_cited_by']}")
    if result["oa_missing"]:
        print("\n「OpenAlex 无记录」通常意味着库内 DOI 本身有错，需用 doi.org/PubMed 人工核对。")


def cmd_db_backfill_fts(args):
    from pathlib import Path
    from backend.config import resolve_path
    from backend.db.conn import get_conn
    from backend.db.repo import update_fts
    conn = get_conn()
    try:
        rows = conn.execute("SELECT id, title, abstract, parsed_path FROM papers").fetchall()
        conn.execute("DELETE FROM papers_fts")
        done, skipped = 0, 0
        for r in rows:
            pid = r["id"]
            parsed_text = ""
            pp = resolve_path(r["parsed_path"])
            if pp:
                try:
                    parsed_text = pp.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    pass
            if not parsed_text and not r["title"] and not r["abstract"]:
                skipped += 1
                continue
            update_fts(conn, pid,
                       title=r["title"] or "",
                       abstract=r["abstract"] or "",
                       parsed_text=parsed_text)
            done += 1
        conn.commit()
    finally:
        conn.close()
    print(f"✅ FTS 索引已重建: {done} 篇已索引, {skipped} 篇跳过(无内容)")


def cmd_db_backfill_bibtex(args):
    """幂等为已有库加 bibtex 列，并批量回填（Crossref content negotiation）。

    流程：1) pragma 检查 -> ALTER TABLE 加列（幂等）
          2) 扫描所有有 doi 但 bibtex 为空的 paper，逐个拉取入库。
    """
    from backend.db.conn import get_conn
    from backend.external.crossref import CrossrefClient

    conn = get_conn()
    try:
        # 幂等加列
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(papers)").fetchall()}
        if "bibtex" not in cols:
            conn.execute("ALTER TABLE papers ADD COLUMN bibtex TEXT")
            conn.commit()
            print("✅ 已为 papers 表添加 bibtex 列")
        else:
            print("ℹ️  bibtex 列已存在，跳过 ALTER")

        rows = conn.execute(
            "SELECT id, doi, dir_name FROM papers "
            "WHERE doi IS NOT NULL AND doi != '' AND bibtex IS NULL ORDER BY id"
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        print("✅ 无需回填（所有带 DOI 的论文已有 BibTeX，或无 DOI）")
        return

    print(f"将回填 {len(rows)} 篇（带 DOI 且 bibtex 为空）...")
    cc = CrossrefClient()
    done, failed, skipped = 0, 0, 0
    for r in rows:
        pid = r["id"]
        doi = r["doi"]
        try:
            bibtex = cc.fetch_bibtex(doi)
        except Exception as e:
            print(f"  ❌ [{pid}] {r['dir_name']}: 请求失败 {e}")
            failed += 1
            continue
        if not bibtex:
            print(f"  ⚠️  [{pid}] {r['dir_name']}: Crossref 无 BibTeX (doi={doi})")
            skipped += 1
            continue
        conn = get_conn()
        try:
            conn.execute(
                "UPDATE papers SET bibtex = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (bibtex, pid),
            )
            conn.commit()
        finally:
            conn.close()
        done += 1
        if done % 25 == 0:
            print(f"  …已回填 {done}/{len(rows)}")
    print(f"✅ 回填完成: {done} 成功, {skipped} 无命中, {failed} 失败")


# ── inbox ──────────────────────────────────────────────────

def cmd_inbox_list(args):
    from backend.config import ensure_inbox_dir
    inbox = ensure_inbox_dir()
    pdfs = sorted(inbox.glob("*.pdf"))
    if not pdfs:
        print("(inbox 为空)")
        return
    print(f"{'name':40} {'size':>12} mtime")
    for f in pdfs:
        st = f.stat()
        print(f"{f.name:40} {_fmt_size(st.st_size):>12} "
              f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(st.st_mtime))}")


def cmd_inbox_scan(args):
    dry_run = getattr(args, "dry_run", False)
    if not dry_run:
        _check_flask(args.force)
    from backend.config import ensure_inbox_dir
    from backend.db import tasks_repo
    from backend.db.conn import get_conn
    from backend.pipeline.steps import normalize_feed_steps
    from backend.services.inbox_preflight import preflight_pdf
    try:
        steps = normalize_feed_steps(args.steps)
    except ValueError as e:
        print(f"❌ {e}", file=sys.stderr)
        sys.exit(1)
    inbox = ensure_inbox_dir()
    pdfs = sorted(inbox.glob("*.pdf"))
    if not pdfs:
        print("(inbox 为空)")
        return
    conn = get_conn()
    tids = []
    duplicates = []
    risks = []
    errors = []
    try:
        for f in pdfs:
            try:
                check = preflight_pdf(conn, f)
            except Exception as e:  # noqa: BLE001
                errors.append((f.name, str(e)))
                print(f"❌ {f.name}: 预检失败 {e}")
                continue
            if check["duplicate_paper_id"]:
                duplicates.append(check)
                print(f"⏭  {f.name}: 重复 {check['duplicate_reason']} -> "
                      f"#{check['duplicate_paper_id']} {check['duplicate_dir_name']}")
                continue
            if check["year_unknown_risk"]:
                risks.append(f.name)
                print(f"⚠️  {f.name}: 预计 year unknown（无可用年份或页面标识符）")
            else:
                print(f"✓  {f.name}: year={check['year'] or '?'} "
                      f"author={check['author'] or '?'} doi={check['doi'] or '-'}")
            if dry_run:
                continue
            tid = tasks_repo.create_task(
                kind="pipeline", payload={"inbox_path": str(f), "steps": steps})
            tids.append(tid)
    finally:
        conn.close()
    if dry_run:
        print(f"dry-run: 将入队 {len(pdfs) - len(duplicates) - len(errors)} 个任务；"
              f"重复 {len(duplicates)}，年份风险 {len(risks)}，预检失败 {len(errors)}")
        return
    print(f"✅ 入队 {len(tids)} 个 pipeline task: {tids}")
    print(f"   steps: {steps}")
    if duplicates:
        print(f"   已跳过重复文件 {len(duplicates)} 个")
    if risks:
        print(f"   年份风险 {len(risks)} 个（任务仍已入队，入库阶段会尝试 DOI 年份回退）")


def cmd_inbox_add(args):
    _check_flask(args.force)
    from backend.config import ensure_inbox_dir
    from backend.db import tasks_repo
    from backend.pipeline.steps import normalize_feed_steps
    try:
        steps = normalize_feed_steps(args.steps)
    except ValueError as e:
        print(f"❌ {e}", file=sys.stderr)
        sys.exit(1)
    inbox = ensure_inbox_dir()

    if args.url:
        target = _download_pdf(args.url, inbox)
    else:
        if not args.file:
            print("⚠️  需指定 <file> 或 --url <url>", file=sys.stderr)
            sys.exit(1)
        src = Path(args.file).expanduser()
        if not src.exists():
            print(f"❌ 文件不存在: {src}", file=sys.stderr)
            sys.exit(1)
        target = inbox / src.name
        if target.exists():
            import secrets
            target = inbox / f"{target.stem}_{secrets.token_hex(4)}.pdf"
        target.write_bytes(src.read_bytes())

    tid = tasks_repo.create_task(
        kind="pipeline", payload={"inbox_path": str(target), "steps": steps})
    print(f"✅ 已添加 {target.name} → pipeline task {tid}")
    print(f"   steps: {steps}")


def _download_pdf(url: str, inbox: Path) -> Path:
    """下载 URL 指向的 PDF 到 inbox。

    校验（协议白名单 / SSRF / 重定向逐跳 / 200MB 上限 / PDF 魔数）统一在
    backend/services/pdf_download.py，与 MCP ingest_by_url、/api/inbox/from_url 一致。
    """
    from backend.services.pdf_download import (
        DownloadError, UrlValidationError, stage_pdf_from_url,
    )
    try:
        target, _size = stage_pdf_from_url(url, inbox)
    except (UrlValidationError, DownloadError) as e:
        print(f"❌ 下载失败: {e}", file=sys.stderr)
        sys.exit(1)
    return target


# ── worker 命令(入队) ────────────────────────────────────

def _enqueue_paper_worker(args, kind: str, extra_payload: dict | None = None):
    _check_flask(args.force)
    ids = _resolve_paper_ids(args)
    if not ids:
        print("无匹配 paper")
        return
    existing, missing = _filter_existing_ids(ids)
    tids = []
    for pid in existing:
        payload = {"paper_id": pid}
        if extra_payload:
            payload.update(extra_payload)
        tids.append(_enqueue(kind, payload, paper_id=pid))
    msg = f"✅ 入队 {len(tids)} 个 {kind} task: {tids}"
    if missing:
        msg += f"\n⚠️  跳过不存在的 paper: {missing}"
    print(msg)


def cmd_parse(args):     _enqueue_paper_worker(args, "parse")
def cmd_enrich(args):    _enqueue_paper_worker(args, "enrich")
def cmd_summarize(args): _enqueue_paper_worker(args, "summary")
def cmd_translate(args): _enqueue_paper_worker(args, "translate", {"force": bool(args.retranslate)})
def cmd_embed(args):     _enqueue_paper_worker(args, "embed", {"force": bool(getattr(args, "reembed", False))})


def cmd_citation_rebuild(args):
    _check_flask(args.force)
    if args.paper_id:
        existing, missing = _filter_existing_ids([args.paper_id])
        if missing:
            print(f"❌ paper {args.paper_id} 不存在", file=sys.stderr)
            sys.exit(1)
        tid = _enqueue("citation", {"paper_id": args.paper_id})
        scope = f"paper {args.paper_id}"
    else:
        tid = _enqueue("citation", {"rebuild": True})
        scope = "全库"
    print(f"✅ 入队 citation rebuild({scope}) task: {tid}")


def cmd_pipeline(args):
    _check_flask(args.force)
    ids = _resolve_paper_ids(args)
    if not ids:
        print("无匹配 paper")
        return
    existing, missing = _filter_existing_ids(ids)
    steps = args.steps.split(",") if args.steps else None
    force_steps = args.force_steps.split(",") if args.force_steps else None
    tids = []
    for pid in existing:
        payload = {"paper_id": pid}
        if steps:
            payload["steps"] = steps
        if force_steps:
            payload["force_steps"] = force_steps
        if args.translate_force:
            payload["translate_force"] = True
        if args.on_error:
            payload["on_error"] = args.on_error
        tids.append(_enqueue("pipeline", payload, paper_id=pid))
    msg = f"✅ 入队 {len(tids)} 个 pipeline task: {tids}"
    if missing:
        msg += f"\n⚠️  跳过不存在的 paper: {missing}"
    print(msg)


def cmd_mcp(args):
    """启动本地 stdio 或 HTTP MCP（无登录体系；HTTP 不注册本地文件入库工具）。"""
    from backend.mcp_server import main as mcp_main
    mcp_main(transport=args.transport, host=args.host, port=args.port)


# ── backup ────────────────────────────────────────────────

def cmd_backup(args):
    """备份本地数据：DB（sqlite backup API 一致性快照）+ library + doc_snapshots。

    输出 tar.gz 到 --output（默认 <数据根目录>/backups/papers_YYYYmmdd_HHMMSS.tar.gz）。
    可选 --rclone-target <remote:path> 备份后 rclone copy（外部同步盘/NAS 皆可）。
    """
    import subprocess as _sp
    import sqlite3 as _sq
    import tarfile as _tf
    from backend.config import CONFIG

    paths = CONFIG["paths"]
    data_dir = Path(paths["data_dir"]).expanduser()
    db_path = Path(paths["db_path"]).expanduser()
    library = Path(paths["library_root"]).expanduser()
    snaps = data_dir / "doc_snapshots"
    reports = data_dir / "survey_reports"

    out = Path(args.output).expanduser() if args.output \
        else data_dir / "backups" / f"papers_{time.strftime('%Y%m%d_%H%M%S')}.tar.gz"
    out.parent.mkdir(parents=True, exist_ok=True)

    # 1) DB 一致性快照（backup API，避免直接 tar WAL 中间态）
    tmp_db = out.parent / "papers.db.snapshot"
    src = _sq.connect(db_path)
    try:
        dst = _sq.connect(tmp_db)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()

    # 2) 打包
    n_files = 0
    with _tf.open(out, "w:gz") as tar:
        tar.add(tmp_db, arcname="papers.db")
        n_files += 1
        for root in (library, snaps, reports):
            root = Path(root)
            if not root.exists():
                continue
            for f in root.rglob("*"):
                if f.is_file():
                    tar.add(f, arcname=str(root.name / f.relative_to(root)))
                    n_files += 1
    tmp_db.unlink(missing_ok=True)
    size = out.stat().st_size
    print(f"✅ 备份完成: {out}（{n_files} 文件, {_fmt_size(size)}）")

    # 3) 可选 rclone 同步
    if args.rclone_target:
        try:
            r = _sp.run(["rclone", "copyto", str(out), args.rclone_target],
                        capture_output=True, text=True, timeout=3600)
            if r.returncode == 0:
                print(f"✅ rclone -> {args.rclone_target}")
            else:
                print(f"❌ rclone 失败: {r.stderr[:200]}", file=sys.stderr)
                sys.exit(1)
        except FileNotFoundError:
            print("❌ rclone 未安装", file=sys.stderr)
            sys.exit(1)


# ── zotero 导出 ────────────────────────────────────────────

def cmd_zotero_export(args):
    """导出 BibTeX/RIS 供 Zotero 导入（本地 library 为真相源，Zotero 是消费方）。"""
    from backend.services import zotero_export

    paper_id = args.paper_id
    fmt = args.format
    out_path = Path(args.output).expanduser() if args.output else None

    if fmt == "bibtex":
        content, n = zotero_export.export_bibtex(
            paper_id=paper_id, year_from=args.year_from, year_to=args.year_to)
    else:
        content, n = zotero_export.export_ris(
            paper_id=paper_id, year_from=args.year_from, year_to=args.year_to,
            include_attachment=not args.no_attachment)

    if not content:
        print("⚠️  无可导出条目")
        return

    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(content, encoding="utf-8")
        print(f"✅ 已导出 {n} 条 -> {out_path}")
    else:
        print(content)
        print(f"# {n} entries", file=sys.stderr)

    if fmt == "ris" and not args.no_attachment:
        print(f"\n💡 Zotero linked-file 提示：", file=sys.stderr)
        print(f"   Zotero -> 设置 -> 高级 -> 链接附件的根目录 设为:", file=sys.stderr)
        print(f"   {zotero_export.zotero_linked_file_hint()}", file=sys.stderr)
        print(f"   RIS 中的 L2 file:pdf/<相对路径> 即解析为本地 PDF。", file=sys.stderr)



def cmd_feed(args):
    _check_flask(args.force)
    from backend.config import ensure_inbox_dir
    from backend.db import tasks_repo
    inbox = ensure_inbox_dir()

    if args.url:
        target = _download_pdf(args.url, inbox)
    else:
        if not args.file:
            print("⚠️  需指定 <file> 或 --url <url>", file=sys.stderr)
            sys.exit(1)
        src = Path(args.file).expanduser()
        if not src.exists():
            print(f"❌ 文件不存在: {src}", file=sys.stderr)
            sys.exit(1)
        target = inbox / src.name
        if target.exists():
            import secrets
            target = inbox / f"{target.stem}_{secrets.token_hex(4)}.pdf"
        target.write_bytes(src.read_bytes())

    from backend.pipeline.steps import normalize_feed_steps
    try:
        steps = normalize_feed_steps(args.steps)
    except ValueError as e:
        print(f"❌ {e}", file=sys.stderr)
        sys.exit(1)
    # --translate-force 仅作为 translate 步骤的参数透传；steps 缺 translate 时
    # 该开关无效果，这里补进 steps（置于 summary 之前，与默认流程顺序一致）
    if args.translate_force and "translate" not in steps:
        if "summary" in steps:
            steps.insert(steps.index("summary"), "translate")
        else:
            steps.append("translate")
    payload: dict = {"inbox_path": str(target), "steps": steps}
    if args.on_error:
        payload["on_error"] = args.on_error
    if args.translate_force:
        payload["translate_force"] = True

    tid = tasks_repo.create_task(kind="pipeline", payload=payload)
    print(f"✅ 已投喂 {target.name} -> pipeline task {tid}")
    print(f"   steps: {steps}")
    print(f"   追踪: paper tasks show {tid}")


# ── tasks ──────────────────────────────────────────────────

def cmd_tasks_list(args):
    from backend.db import tasks_repo
    items = tasks_repo.list_tasks(
        status=args.status, kind=args.kind,
        paper_id=args.paper_id, limit=min(args.limit, 500),
    )
    if not items:
        print("无任务")
        return
    print(f"{'id':>6} {'status':10} {'kind':14} {'paper':>5} {'prog':>5} created")
    for t in items:
        prog = t.get("progress") or 0
        print(f"{t['id']:>6} {t['status']:10} {t['kind']:14} "
              f"{str(t.get('paper_id') or '-'):>5} {prog:>4.0%} {t.get('created_at', '')}")


def cmd_tasks_show(args):
    from backend.db import tasks_repo
    t = tasks_repo.get_task(args.id)
    if not t:
        print(f"task {args.id} 不存在")
        sys.exit(1)
    print(f"id:        {t['id']}")
    print(f"kind:      {t['kind']}")
    print(f"status:    {t['status']}")
    print(f"paper_id:  {t.get('paper_id') or '-'}")
    print(f"progress:  {(t.get('progress') or 0):.0%}")
    print(f"retries:   {t.get('retries', 0)}")
    print(f"error:     {t.get('error') or '-'}")
    print(f"created:   {t.get('created_at')}")
    print(f"started:   {t.get('started_at') or '-'}")
    print(f"finished:  {t.get('finished_at') or '-'}")
    print(f"payload:   {t.get('payload')}")
    print("─" * 60)
    print("log:")
    log = tasks_repo.read_task_log(args.id, tail=200)
    print(log if log else "(无日志)")


def cmd_tasks_retry(args):
    _check_flask(args.force)
    from backend.db import tasks_repo
    t = tasks_repo.get_task(args.id)
    if not t:
        print(f"task {args.id} 不存在")
        sys.exit(1)
    if t["status"] not in ("error", "cancelled"):
        print(f"⚠️  仅 error/cancelled 可 retry,当前 status={t['status']}")
        sys.exit(1)
    new_id = tasks_repo.create_task(
        kind=t["kind"], payload=t.get("payload"), paper_id=t.get("paper_id"))
    print(f"✅ 已创建新 task {new_id}(复制自 {args.id})")


def cmd_tasks_logs(args):
    from backend.db import tasks_repo
    if not args.follow:
        log = tasks_repo.read_task_log(args.id, tail=500)
        print(log if log else "(无日志)")
        return
    # follow:轮询 events 表(不依赖 Flask SSE)
    log = tasks_repo.read_task_log(args.id, tail=100)
    if log:
        print(log)
    last_id = 0
    events = tasks_repo.list_events(since_id=0, task_id=args.id, limit=1)
    if events:
        last_id = events[-1]["id"]
    try:
        while True:
            for e in tasks_repo.list_events(since_id=last_id, task_id=args.id, limit=100):
                last_id = e["id"]
                print(f"[{e.get('level', 'info')}] {e.get('msg', '')}")
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n(stopped)")


def cmd_tasks_recover(args):
    from backend.db import tasks_repo
    stats = tasks_repo.recover_orphans()
    print(f"✅ task recovery: requeued={stats['requeued']} failed={stats['failed']}")


# ── cache ──────────────────────────────────────────────────

def _cache_dir() -> Path:
    from backend.config import CONFIG
    return Path(CONFIG["paths"]["cache_dir"])


def cmd_cache_stats(args):
    cache = _cache_dir()
    if not cache.exists():
        print(f"缓存目录不存在: {cache}")
        return
    total = 0
    print(f"{'service':20} {'files':>8} {'size':>12}")
    for sub in sorted(cache.iterdir()):
        if sub.is_dir():
            nfiles = _count_files(sub)
            size = sum(f.stat().st_size for f in sub.rglob("*") if f.is_file())
            total += size
            print(f"{sub.name:20} {nfiles:>8} {_fmt_size(size):>12}")
        elif sub.is_file():
            total += sub.stat().st_size
    print(f"{'total':20} {'':>8} {_fmt_size(total):>12}")


def cmd_cache_clear(args):
    cache = _cache_dir()
    if args.service:
        target = cache / args.service
        if not target.exists():
            print(f"无缓存: {target}")
            return
        n = _count_files(target)
        shutil.rmtree(target)
        print(f"✅ 清除 {target.name}({n} 文件)")
    else:
        if not cache.exists():
            print("无缓存目录")
            return
        n = _count_files(cache)
        shutil.rmtree(cache)
        cache.mkdir(parents=True, exist_ok=True)
        print(f"✅ 清除全部缓存({n} 文件)")


def cmd_cache_prune(args):
    from backend.config import CONFIG
    cache = _cache_dir()
    if not cache.exists():
        print("无缓存目录")
        return
    ttl_hours = args.ttl if args.ttl else CONFIG["external"]["cache_ttl_hours"]
    cutoff = time.time() - ttl_hours * 3600
    removed = 0
    for f in cache.rglob("*"):
        if f.is_file() and f.stat().st_mtime < cutoff:
            f.unlink(missing_ok=True)
            removed += 1
    for sub in sorted(cache.rglob("*"), reverse=True):
        if sub.is_dir() and not any(sub.iterdir()):
            sub.rmdir()
    print(f"✅ 按 TTL {ttl_hours}h 清理 {removed} 文件")


# ── config / doctor ────────────────────────────────────────

def cmd_config_show(args):
    from backend.config import CONFIG
    for section in ("paths", "server", "workers", "external", "mineru", "llm"):
        print(f"{section}:")
        for k, v in CONFIG[section].items():
            print(f"  {k:15} {v}")
    print("secrets:")
    for k, v in CONFIG["secrets"].items():
        print(f"  {k:25} {'(已配置)' if v else '(空)'} [{_mask(v)}]")


# 托管检索链路的备选 key，以及可免费使用的外部 API 提额 key。
_OPTIONAL_SECRET_KEYS = {
    "fireworks_api_key", "bp_embed_api_key", "bp_rerank_api_key", "ark_api_key",
    "semantic_scholar_key", "sciverse_api_token", "openalex_api_key",
}


def _secret_required(key: str) -> bool:
    from backend.config import CONFIG

    if key == "mineru_token" and CONFIG.get("mineru", {}).get("mode") == "local":
        return False
    return key not in _OPTIONAL_SECRET_KEYS


def cmd_config_check(args):
    from backend.config import CONFIG, check_storage, hosted_retrieval_config, retrieval_api_key
    ok = True
    required = [k for k in CONFIG["secrets"] if _secret_required(k)]
    print("secrets:")
    for k in required:
        v = CONFIG["secrets"][k]
        status = "✅" if v else "❌ 空"
        print(f"  {k:25} {status}")
        if not v:
            ok = False
    rcfg, rerr = hosted_retrieval_config()
    if rerr:
        print(f"retrieval: {'❌ ' + rerr}")
        ok = False
    else:
        print(f"retrieval: ✅ embed={rcfg['embed_model']} dims={rcfg['embed_dimensions']}")
        for kind in ("embed", "rerank"):
            key_ok = bool(retrieval_api_key(kind))
            print(f"  {kind}_api_key (chain): {'✅' if key_ok else '❌ 空'}")
            if not key_ok:
                ok = False
    print("dirs:")
    for k in ("data_dir", "cache_dir", "log_dir", "library_root", "inbox_dir"):
        p = Path(CONFIG["paths"][k])
        exists = p.exists()
        print(f"  {k:15} {'✅' if exists else '❌'} {p}")
        if not exists:
            ok = False
    storage = check_storage()
    print(f"storage:           {'✅' if storage['ok'] else '❌'} library_exists={storage['library_exists']}")
    print("─" * 40)
    print("✅ 全部就绪" if ok else "❌ 存在问题(见上)")


def cmd_doctor(args):
    from backend.config import CONFIG, check_storage
    from backend.db.conn import DB_PATH
    print("paper doctor")
    print("─" * 50)
    print(f"DB path:      {DB_PATH}")
    print(f"DB exists:    {'✅' if DB_PATH.exists() else '❌'}")
    if DB_PATH.exists():
        try:
            from backend.db.conn import get_conn
            conn = get_conn()
            n = conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
            from backend.db.authz_schema import inspect_authz_schema
            authz = inspect_authz_schema(conn)
            if authz["status"] == "missing":
                authz_message = "❌ missing " + ", ".join(authz["missing_columns"])
            elif authz["status"] == "not_null_mismatch":
                authz_message = (
                    "⚠️  tasks.execution_subject must be NOT NULL; "
                    "run backend/db/migrations/20261029_task_execution_subject_not_null.py after backup"
                )
            else:
                authz_message = "✅ complete"
            print(f"authz schema: {authz_message}")
            conn.close()
            print(f"DB papers:    ✅ {n}")
            legacy = conn_nas_legacy_count()
            if legacy:
                print(f"legacy paths: ⚠️  {legacy} 篇 PDF/解析路径仍指向旧存储（paper db migrate-v3 可迁移）")
        except Exception as e:
            print(f"DB readable:  ❌ {e}")
    storage = check_storage()
    print(f"storage:      {'✅' if storage['ok'] else '❌'} {storage['library_root']}")
    missing = [k for k, v in CONFIG["secrets"].items() if not v and _secret_required(k)]
    print(f"secrets:      {'✅ 必需项已配置' if not missing else '❌ 缺 ' + ', '.join(missing)}")
    _doctor_retrieval()
    _doctor_mineru()
    alive = _health()
    print(f"Flask alive:  {'✅' if alive else '⚠️  未运行(paper serve --detach)'}")
    from backend.services.doctor import mcp_processes, task_queue_stats
    queue = task_queue_stats()
    counts = queue["counts"]
    print(f"tasks:        pending={counts.get('pending', 0)} "
          f"running={counts.get('running', 0)} error={counts.get('error', 0)}")
    oldest = queue["oldest_running"]
    if oldest:
        print(f"  oldest run: #{oldest['id']} {oldest['kind']} "
              f"started={oldest['started_at'] or oldest['created_at']}")
    recovery = queue["last_recovery"]
    print(f"last recover: requeued={recovery['requeued']} "
          f"failed={recovery['failed']} ts={recovery['ts'] or '-'}")
    processes = mcp_processes()
    print(f"MCP process:  {len(processes)} 个（当前代码 {_version()}）")
    for process in processes:
        stale = " ⚠️  可能为旧代码" if process["stale_suspected"] else ""
        print(f"  pid={process['pid']} started={process['started'] or '?'}{stale}")
    cache = Path(CONFIG["paths"]["cache_dir"])
    if cache.exists():
        size = sum(f.stat().st_size for f in cache.rglob("*") if f.is_file())
        print(f"cache size:   {_fmt_size(size)}")
    print("─" * 50)


def _doctor_mineru():
    from backend.config import CONFIG
    from backend.services import mineru_health

    cfg = CONFIG["mineru"]
    if cfg.get("mode", "cloud") != "local":
        print("mineru:       ⚠️  cloud 模式，跳过本地容器检查")
        return
    http = mineru_health.probe_http(cfg.get("local_api_base", "http://127.0.0.1:8000"))
    if http.get("ok"):
        print(
            f"mineru http:  ✅ {http.get('status')} "
            f"processing={http.get('processing_tasks', '?')} queued={http.get('queued_tasks', '?')}"
        )
    else:
        print(f"mineru http:  ❌ {http.get('error', 'unknown error')}")
    container = cfg.get("local_container", "")
    if not container:
        print("mineru gpu:   ⚠️  未配置 local_container，跳过容器检查")
        return
    gpu_ok, detail = mineru_health.probe_container_gpu(container)
    if gpu_ok:
        print(f"mineru gpu:   ✅ {detail} ({container})")
    else:
        print(f"mineru gpu:   ❌ {detail}")
        print(f"              修复：docker restart {container}")


def _doctor_retrieval():
    """托管检索校验：四模式变量齐备 + key 解析链 + 对嵌入/精排端点各发一次最小请求。

    验证请求同时承担两个职责：确认 key 可用（挂错 key 的形态是逐条 401 而非配置缺失）、
    观察 qwen3-reranker-8b relevance_score 的实际标度（rerank_threshold 标定输入）。
    """
    from backend.config import hosted_retrieval_config, retrieval_api_key
    rcfg, rerr = hosted_retrieval_config()
    if rerr:
        print(f"retrieval:    ❌ {rerr}")
        return
    print(f"retrieval:    ✅ embed={rcfg['embed_model']} dims={rcfg['embed_dimensions']}"
          f" rerank={'on' if rcfg['rerank_enabled'] else 'off'} threshold={rcfg['rerank_threshold']}")
    for kind in ("embed", "rerank"):
        if not retrieval_api_key(kind):
            print(f"  {kind} key:  ❌ 空（BP_{kind.upper()}_API_KEY -> FIREWORKS_API_KEY -> ARK_API_KEY）")
            return
    try:
        from backend.external.hosted_retrieval import HostedRetrievalClient
        client = HostedRetrievalClient()
        vecs = client.embed(["retrieval health check"])
        dim_ok = (not rcfg["embed_dimensions"]) or len(vecs[0]) == rcfg["embed_dimensions"]
        dim_note = "" if dim_ok else f"（期望 {rcfg['embed_dimensions']}）"
        print(f"  embed ep:   {'✅' if dim_ok else '❌'} 返回 {len(vecs[0])} 维{dim_note}")
        ranked = client.rerank("neural population dynamics",
                               ["Neural population dynamics during reaching.", "A recipe for bread."])
        scores = ", ".join(f"{r['score']:.4f}" for r in ranked)
        print(f"  rerank ep:  ✅ relevance_score 样本 [{scores}]（阈值标定参考）")
    except Exception as e:  # noqa: BLE001
        print(f"  endpoint:   ❌ {e}")


def conn_nas_legacy_count() -> int:
    """统计路径字段仍为 legacy: 前缀（旧存储待迁移）的论文数。"""
    from backend.config import LEGACY_PREFIX
    from backend.db.conn import get_conn
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM papers WHERE pdf_path LIKE ? OR parsed_path LIKE ?",
            (f"{LEGACY_PREFIX}%", f"{LEGACY_PREFIX}%"),
        ).fetchone()
        return row[0]
    except Exception:
        return 0
    finally:
        conn.close()


# ── search ─────────────────────────────────────────────────

def cmd_search(args):
    from backend.api.search import _DISPATCHERS
    dispatcher = _DISPATCHERS.get(args.source)
    if not dispatcher:
        print(f"❌ unknown source: {args.source}; must be {list(_DISPATCHERS)}", file=sys.stderr)
        sys.exit(1)

    print(f"搜索 [{args.source}] '{args.query}' (limit={args.limit}) …")
    try:
        items = dispatcher(args.query, args.limit, args.year_from, args.year_to)
    except Exception as e:  # noqa: BLE001
        print(f"❌ search failed: {e}", file=sys.stderr)
        sys.exit(1)

    if not items:
        print("(无结果)")
        return

    for item in items:
        print("─" * 80)
        print(f"[{item.get('year') or '?'}] {item.get('title') or '(无标题)'}")
        authors = item.get("authors") or []
        if authors:
            print(f"  作者: {', '.join(authors[:5])}"
                  f"{' …(+%d)' % (len(authors) - 5) if len(authors) > 5 else ''}")
        if item.get("journal"):
            print(f"  期刊: {item['journal']}")
        if item.get("doi"):
            print(f"  DOI:  {item['doi']}")
        if item.get("arxiv_id"):
            print(f"  arXiv: {item['arxiv_id']}")
        if item.get("cited_by_count") is not None:
            print(f"  引用: {item['cited_by_count']}")
        if item.get("url"):
            print(f"  链接: {item['url']}")
        if item.get("pdf_url"):
            print(f"  PDF:  {item['pdf_url']}")
        abstract = item.get("abstract") or item.get("snippet") or item.get("tldr") or ""
        if abstract:
            short = abstract[:200] + ("…" if len(abstract) > 200 else "")
            print(f"  摘要: {short}")
    print(f"\n共 {len(items)} 条结果")


# ── 其他 ───────────────────────────────────────────────────

def cmd_version(args):
    print(_version())


def cmd_routes(args):
    from backend.app import app
    rules = sorted(app.url_map.iter_rules(), key=lambda r: r.rule)
    for r in rules:
        methods = ",".join(sorted(m for m in r.methods if m not in ("HEAD", "OPTIONS")))
        print(f"{methods:20s} {r.rule}")


def cmd_test(args):
    pytest_args = [a for a in args.pytest_args if a != "--"]
    cmd = [sys.executable, "-m", "pytest"] + pytest_args
    env = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT)}
    result = subprocess.run(cmd, cwd=str(PROJECT_ROOT), env=env)
    sys.exit(result.returncode)


# ── argparse ───────────────────────────────────────────────

def _add_paper_args(p):
    p.add_argument("ids", type=int, nargs="*", help="paper id(可多个)")
    p.add_argument("--all", action="store_true", help="对所有 paper 操作")
    p.add_argument("--status", help="--all 时过滤:field=value(如 parsed=missing)")
    p.add_argument("--force", action="store_true", help="跳过 Flask 存活检测")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="paper", description="文献调研系统 CLI(运维外壳)")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="command", required=True)

    # serve
    p_serve = sub.add_parser("serve", help="Flask 服务管理")
    p_serve.add_argument("--host")
    p_serve.add_argument("--port", "-p", type=int)
    g = p_serve.add_mutually_exclusive_group()
    g.add_argument("--debug", dest="debug", action="store_true", default=None)
    g.add_argument("--no-debug", dest="debug", action="store_false")
    p_serve.add_argument("--detach", action="store_true")
    p_serve.add_argument("--force", action="store_true")
    p_serve.set_defaults(func=cmd_serve)
    serve_sub = p_serve.add_subparsers(dest="serve_action")
    p = serve_sub.add_parser("stop"); p.set_defaults(func=cmd_serve_stop)
    p = serve_sub.add_parser("status"); p.set_defaults(func=cmd_serve_status)
    p = serve_sub.add_parser("logs"); p.add_argument("-f", "--follow", action="store_true")
    p.set_defaults(func=cmd_serve_logs)

    # frontend
    p_fe = sub.add_parser("frontend", help="Vite 前端管理")
    p_fe.add_argument("--detach", action="store_true")
    p_fe.add_argument("--force", action="store_true")
    p_fe.set_defaults(func=cmd_frontend)
    fe_sub = p_fe.add_subparsers(dest="frontend_action")
    p = fe_sub.add_parser("stop"); p.set_defaults(func=cmd_frontend_stop)
    p = fe_sub.add_parser("status"); p.set_defaults(func=cmd_frontend_status)
    p = fe_sub.add_parser("logs"); p.add_argument("-f", "--follow", action="store_true")
    p.set_defaults(func=cmd_frontend_logs)

    # db
    p_db = sub.add_parser("db", help="数据库操作")
    db_sub = p_db.add_subparsers(dest="db_action", required=True)
    p = db_sub.add_parser("init"); p.set_defaults(func=cmd_db_init)
    p = db_sub.add_parser("path"); p.set_defaults(func=cmd_db_path)
    p = db_sub.add_parser("stats"); p.set_defaults(func=cmd_db_stats)
    p = db_sub.add_parser("shell"); p.set_defaults(func=cmd_db_shell)
    p = db_sub.add_parser("migrate-v3"); p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_db_migrate_v3)
    p = db_sub.add_parser("backfill_fts"); p.set_defaults(func=cmd_db_backfill_fts)
    p = db_sub.add_parser("backfill_bibtex"); p.set_defaults(func=cmd_db_backfill_bibtex)
    p = db_sub.add_parser("rename", help="重命名论文 dir_name，保留论文与衍生数据")
    p.add_argument("paper_id", type=int)
    p.add_argument("--dir-name", required=True, help="新的 dir_name（小写字母/数字/下划线）")
    p.add_argument("--year", type=int, help="同时移动到 library/<year>/ 并更新 papers.year（纠正入库年份错误）")
    p.add_argument("--dry-run", action="store_true", help="只打印目录和数据库变更计划")
    p.set_defaults(func=cmd_db_rename)
    p = db_sub.add_parser("audit-citations", help="对账 cited_by_count 与 OpenAlex 当前值（按 DOI 批量）")
    p.add_argument("--fix", action="store_true", help="把过期/坏值改写为 OpenAlex 当前值并落 events")
    p.set_defaults(func=cmd_db_audit_citations)

    # inbox
    p_inbox = sub.add_parser("inbox", help="PDF 投递入口")
    inbox_sub = p_inbox.add_subparsers(dest="inbox_action", required=True)
    p = inbox_sub.add_parser("list"); p.set_defaults(func=cmd_inbox_list)
    p = inbox_sub.add_parser("scan")
    p.add_argument("--steps", help="逗号分隔步骤（默认 ingest,parse,enrich,embed,summary）")
    p.add_argument("--force", action="store_true")
    p.add_argument("--dry-run", action="store_true", help="只做预检，不入队")
    p.set_defaults(func=cmd_inbox_scan)
    p = inbox_sub.add_parser("add")
    p.add_argument("file", nargs="?")
    p.add_argument("--url")
    p.add_argument("--steps", help="逗号分隔步骤（默认 ingest,parse,enrich,embed,summary）")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_inbox_add)

    # worker 命令（embed 单独注册：带 --reembed flag）
    for name, func in [("parse", cmd_parse), ("enrich", cmd_enrich),
                       ("summarize", cmd_summarize)]:
        p = sub.add_parser(name, help=f"入队 {name} task")
        _add_paper_args(p)
        p.set_defaults(func=func)
    p = sub.add_parser("translate", help="入队 translate task")
    _add_paper_args(p)
    p.add_argument("--retranslate", action="store_true", help="清空已有 chunks 强制重译")
    p.set_defaults(func=cmd_translate)
    p = sub.add_parser("embed", help="入队 embed task（向量化，RAG 层）")
    _add_paper_args(p)
    p.add_argument("--reembed", action="store_true", help="强制重嵌（清空已有向量）")
    p.set_defaults(func=cmd_embed)

    # citation 嵌套
    p_cite = sub.add_parser("citation", help="引文网络")
    cite_sub = p_cite.add_subparsers(dest="citation_action", required=True)
    p = cite_sub.add_parser("rebuild")
    p.add_argument("--paper-id", type=int)
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_citation_rebuild)

    # pipeline
    p = sub.add_parser("pipeline", help="一键串行流水线")
    _add_paper_args(p)
    p.add_argument("--steps", help="逗号分隔步骤(parse,enrich,translate,summary,embed)")
    p.add_argument("--force-steps", help="逗号分隔强制重跑步骤")
    p.add_argument("--translate-force", action="store_true", help="透传 translate force")
    p.add_argument("--on-error", choices=["stop", "continue"])
    p.set_defaults(func=cmd_pipeline)

    # feed（投喂 PDF + 自动流水线）
    p_feed = sub.add_parser("feed", help="投喂 PDF 并自动执行流水线（入库+解析+富化+总结）")
    p_feed.add_argument("file", nargs="?", help="PDF 文件路径")
    p_feed.add_argument("--url", help="从 URL 下载 PDF")
    p_feed.add_argument("--steps", help="自定义步骤（默认 ingest,parse,enrich,embed,summary）")
    p_feed.add_argument("--translate-force", action="store_true", help="透传 translate force")
    p_feed.add_argument("--on-error", choices=["stop", "continue"])
    p_feed.add_argument("--force", action="store_true", help="跳过 Flask 存活检测")
    p_feed.set_defaults(func=cmd_feed)

    # backup
    p_bk = sub.add_parser("backup", help="备份 DB + library 到 tar.gz（可选 rclone 异地同步）")
    p_bk.add_argument("--output", "-o", help="输出文件（默认 backups/papers_时间戳.tar.gz）")
    p_bk.add_argument("--rclone-target", help="rclone 目标 remote:path（可选）")
    p_bk.set_defaults(func=cmd_backup)

    # mcp
    p_mcp = sub.add_parser("mcp", help="启动 MCP 服务器（默认 stdio，可选 HTTP）")
    p_mcp.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    p_mcp.add_argument("--host", default="127.0.0.1")
    p_mcp.add_argument("--port", type=int, default=5051)
    p_mcp.set_defaults(func=cmd_mcp)

    # zotero 导出
    p_z = sub.add_parser("zotero-export", help="导出 BibTeX/RIS 供 Zotero 导入")
    p_z.add_argument("--format", "-f", choices=["bibtex", "ris"], default="ris")
    p_z.add_argument("--output", "-o", help="输出文件路径（缺省打印到 stdout）")
    p_z.add_argument("--paper-id", type=int, help="仅导出指定论文")
    p_z.add_argument("--year-from", type=int)
    p_z.add_argument("--year-to", type=int)
    p_z.add_argument("--no-attachment", action="store_true", help="RIS 不附 L2 附件路径")
    p_z.set_defaults(func=cmd_zotero_export)

    # search
    p_search = sub.add_parser("search", help="多源文献搜索")
    p_search.add_argument("query", help="搜索关键字")
    p_search.add_argument("--source", default="arxiv",
                          choices=["arxiv", "openalex", "semantic_scholar", "sciverse"],
                          help="搜索源（默认 arxiv）")
    p_search.add_argument("--limit", type=int, default=20)
    p_search.add_argument("--year-from", type=int, default=None)
    p_search.add_argument("--year-to", type=int, default=None)
    p_search.set_defaults(func=cmd_search)

    # tasks
    p_tasks = sub.add_parser("tasks", help="任务队列")
    tasks_sub = p_tasks.add_subparsers(dest="tasks_action", required=True)
    p = tasks_sub.add_parser("list")
    p.add_argument("--status"); p.add_argument("--kind"); p.add_argument("--paper-id", type=int)
    p.add_argument("--limit", type=int, default=100)
    p.set_defaults(func=cmd_tasks_list)
    p = tasks_sub.add_parser("show"); p.add_argument("id", type=int); p.set_defaults(func=cmd_tasks_show)
    p = tasks_sub.add_parser("retry"); p.add_argument("id", type=int)
    p.add_argument("--force", action="store_true"); p.set_defaults(func=cmd_tasks_retry)
    p = tasks_sub.add_parser("logs"); p.add_argument("id", type=int)
    p.add_argument("-f", "--follow", action="store_true"); p.set_defaults(func=cmd_tasks_logs)
    p = tasks_sub.add_parser("recover"); p.set_defaults(func=cmd_tasks_recover)

    # cache
    p_cache = sub.add_parser("cache", help="磁盘缓存")
    cache_sub = p_cache.add_subparsers(dest="cache_action", required=True)
    p = cache_sub.add_parser("clear"); p.add_argument("--service"); p.set_defaults(func=cmd_cache_clear)
    p = cache_sub.add_parser("prune"); p.add_argument("--ttl", type=float); p.set_defaults(func=cmd_cache_prune)
    p = cache_sub.add_parser("stats"); p.set_defaults(func=cmd_cache_stats)

    # config
    p_config = sub.add_parser("config", help="配置查看")
    config_sub = p_config.add_subparsers(dest="config_action", required=True)
    p = config_sub.add_parser("show"); p.set_defaults(func=cmd_config_show)
    p = config_sub.add_parser("check"); p.set_defaults(func=cmd_config_check)

    # doctor
    p = sub.add_parser("doctor", help="综合健康检查"); p.set_defaults(func=cmd_doctor)

    # 单命令
    p = sub.add_parser("version"); p.set_defaults(func=cmd_version)
    p = sub.add_parser("routes"); p.set_defaults(func=cmd_routes)
    p = sub.add_parser("test"); p.add_argument("pytest_args", nargs=argparse.REMAINDER)
    p.set_defaults(func=cmd_test)
    return ap


def main():
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
