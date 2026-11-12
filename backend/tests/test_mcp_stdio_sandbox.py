"""真实 MCP stdio、独立 Flask worker 与平行 SQLite 的端到端重放。"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import fitz
import pytest
from fastmcp.client import Client
from fastmcp.client.transports import StdioTransport

from backend.version import get_version
from backend.tests.sandbox_runtime import (
    EXAMPLE_DOI,
    EXAMPLE_TITLE,
    FALLBACK_DOI,
    FALLBACK_TITLE,
    OVERLEAF_TITLE,
    SAMPLE_DOI,
    SAMPLE_TITLE,
    SCAN_DOI,
    SCAN_TITLE,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
EXPECTED_TOOLS = {
    "add_annotation", "get_annotations", "get_citations", "get_paper",
    "get_paper_by_doi", "get_papers", "get_papers_by_dois", "get_summary",
    "get_task", "ingest_by_url", "ingest_local_file", "ingest_local_files",
    "list_tasks", "read_fulltext", "read_translation", "search_fulltext",
    "search_library", "search_papers", "semantic_search",
}


def _make_pdf(path: Path, text: str, *, title=None, authors=None) -> None:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_textbox(fitz.Rect(48, 48, 560, 760), text, fontsize=10)
    metadata = {}
    if title is not None:
        metadata["title"] = title
    if authors is not None:
        metadata["author"] = authors
    metadata["subject"] = "Sandbox PDF fixture " + "x" * 2048
    doc.set_metadata(metadata)
    doc.save(str(path))
    doc.close()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _StubHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _json(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _body(self):
        return self.rfile.read(int(self.headers.get("Content-Length", "0")))

    def do_GET(self):
        self.server.downloads.append({
            "path": self.path,
            "original_url": self.headers.get("X-Sandbox-Original-Url"),
            "user_agent": self.headers.get("User-Agent"),
            "accept": self.headers.get("Accept"),
        })
        if self.path == "/json":
            self.server.json_calls += 1
            self._json({"ok": True})
            return
        if "PMC0001234" in self.path:
            self._json({"error": "recorded 404"}, status=404)
            return
        if self.path.startswith("/content/"):
            self._json({"error": "recorded 403"}, status=403)
            return
        if self.path == "/download/sample2005.pdf":
            body = self.server.pdf_bytes
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self._json({"error": "unknown sandbox path"}, status=404)

    def do_POST(self):
        raw = self._body()
        if self.path == "/file_parse":
            name = re.search(rb'filename="([^"]+)"', raw)
            self.server.parse_names.append(name.group(1).decode() if name else "unknown")
            self.server.parse_started.set()
            if self.server.block_parsing.is_set():
                self.server.release_parsing.wait(10)
            image = base64.b64encode(b"sandbox-image").decode()
            markdown = (
                "# Sandbox parsed document\n\n## Methods\n\n"
                "The Fano factor is computed from spike-count variance across trials.\n\n"
                "## Results\n\nNeural activity during perceptual decisions.\n\n"
                "![figure](images/figure.png)\n"
            )
            self._json({
                "status": "completed",
                "results": {"sandbox.pdf": {
                    "md_content": markdown,
                    "images": {"figure.png": "data:image/png;base64," + image},
                }},
            })
            return
        payload = json.loads(raw or b"{}")
        if self.path == "/v1/embeddings":
            vectors = [
                [1.0, 0.0] if "fano" in text.lower() else [0.8, 0.2]
                for text in payload["input"]
            ]
            self._json({"data": [
                {"index": index, "embedding": vector}
                for index, vector in enumerate(vectors)
            ]})
            return
        if self.path == "/v1/rerank":
            results = []
            for index, text in enumerate(payload["documents"]):
                score = 0.99 if "fano" in text.lower() else 0.1
                if "neural" in text.lower():
                    score = max(score, 0.95)
                results.append({"index": index, "score": score})
            results.sort(key=lambda item: -item["score"])
            self._json({"results": results})
            return
        if self.path == "/v1/chat/completions":
            self._json({
                "choices": [{"message": {
                    "content": "# 沙箱总结\n\n这是测试服务生成的中文总结。",
                }}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 10},
            })
            return
        self._json({"error": "unknown sandbox path"}, status=404)


@pytest.fixture
def stub_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
    server.daemon_threads = True
    server.downloads = []
    server.parse_names = []
    server.json_calls = 0
    server.parse_started = threading.Event()
    server.block_parsing = threading.Event()
    server.release_parsing = threading.Event()
    server.pdf_bytes = b""
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.release_parsing.set()
        server.shutdown()
        server.server_close()
        thread.join(2)


def _env():
    return {**os.environ, "PYTHONPATH": str(PROJECT_ROOT)}


def _start_server(root: Path, server_port: int, stub_port: int) -> subprocess.Popen:
    log = (root / "flask-sandbox.log").open("a")
    proc = subprocess.Popen([
        sys.executable, "-m", "backend.tests.sandbox_runtime", "serve",
        "--root", str(root), "--stub-port", str(stub_port),
        "--server-port", str(server_port),
    ], cwd=PROJECT_ROOT, env=_env(), stdout=log, stderr=subprocess.STDOUT)
    log.close()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError((root / "flask-sandbox.log").read_text())
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{server_port}/api/health", timeout=0.3,
            ) as response:
                health = json.load(response)
            assert health["version"] == get_version()
            assert health["storage"]["library_root"] == str(root / "library")
            return proc
        except Exception:
            time.sleep(0.05)
    proc.kill()
    raise AssertionError("sandbox Flask did not become healthy")


def _stop_server(proc: subprocess.Popen, *, crash=False) -> None:
    if proc.poll() is not None:
        return
    proc.send_signal(signal.SIGKILL if crash else signal.SIGTERM)
    try:
        proc.wait(5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(5)


def _cli(root: Path, server_port: int, stub_port: int, *args) -> str:
    result = subprocess.run([
        sys.executable, "-m", "backend.tests.sandbox_runtime", "cli",
        "--root", str(root), "--stub-port", str(stub_port),
        "--server-port", str(server_port), "--", *args,
    ], cwd=PROJECT_ROOT, env=_env(), capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def _rows(root: Path, sql: str, params=()) -> list[dict]:
    conn = sqlite3.connect(root / "papers.db")
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(sql, params)]
    finally:
        conn.close()


def _execute(root: Path, sql: str, params=()) -> None:
    conn = sqlite3.connect(root / "papers.db")
    try:
        conn.execute(sql, params)
        conn.commit()
    finally:
        conn.close()


async def _call(client: Client, name: str, arguments=None) -> dict:
    result = await client.call_tool(name, arguments or {})
    assert not result.is_error, (name, result)
    assert result.content and result.content[0].type == "text"
    payload = json.loads(result.content[0].text)
    assert isinstance(payload, dict)
    return payload


async def _wait_task(client: Client, task_id: int, timeout=30) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = await _call(client, "get_task", {"task_id": task_id})
        if task["status"] == "ok":
            return task
        if task["status"] == "error":
            raise AssertionError(f"task {task_id} failed: {task.get('error')}")
        await asyncio.sleep(0.05)
    raise AssertionError(f"task {task_id} did not finish: {task}")


async def _exercise(root: Path, stub, server_port: int) -> None:
    fixtures = root / "fixtures"
    fixtures.mkdir()
    example = fixtures / "example2006.pdf"
    _make_pdf(
        example,
        "SBJNL_EX_0042 101..109\n" + EXAMPLE_DOI + "\n1\n2\n3\n",
        title="SBJNL_EX_0042 101..109",
    )
    sample = fixtures / "sample2005.pdf"
    _make_pdf(sample, SAMPLE_TITLE + "\nSample\n" + SAMPLE_DOI)
    example2026 = fixtures / "2026_example_population_dynamics.pdf"
    _make_pdf(example2026, OVERLEAF_TITLE + "\nTian Example; Alex Sample\n",
              title="Overleaf Example", authors="Tian Example; Alex Sample")
    stub.pdf_bytes = sample.read_bytes()
    stub.block_parsing.set()

    code = (
        "from backend.tests.sandbox_runtime import run_mcp; "
        f"run_mcp({str(root)!r}, {stub.server_address[1]})"
    )
    transport = StdioTransport(
        command=sys.executable,
        args=["-c", code, "backend.mcp_server"],
        cwd=str(PROJECT_ROOT),
        env=_env(),
        log_file=root / "mcp-sandbox.log",
    )
    server = _start_server(root, server_port, stub.server_address[1])
    recovered_retries = None
    try:
        async with Client(transport, timeout=15) as client:
            initialized = await client.initialize()
            assert initialized.serverInfo.version == get_version()
            tools = await client.list_tools()
            assert {tool.name for tool in tools} == EXPECTED_TOOLS

            original_downloads = [
                (
                    "https://www.jneurosci.org/content/26/14/sample.full.pdf",
                    "example2006_neural_dynamics.pdf",
                    "403",
                ),
                (
                    "https://www.ebi.ac.uk/europepmc/webservices/rest/PMC0001234/fullTextPDF",
                    "example2006_neural_dynamics.pdf",
                    "404",
                ),
                (
                    "https://www.jneurosci.org/content/jneuro/26/14/sample.full.pdf",
                    "example2006_neural_dynamics.pdf",
                    "403",
                ),
            ]
            for url, filename, status in original_downloads:
                sandbox_url = url.replace(
                    "https://www.jneurosci.org", "https://paper.sandbox.test",
                ).replace("https://www.ebi.ac.uk", "https://paper.sandbox.test")
                failure = await _call(client, "ingest_by_url", {
                    "url": sandbox_url, "filename": filename, "steps": ["ingest"],
                })
                assert status in failure["error"]
                assert "ingest_local_file" in failure["hint"]
            assert _rows(root, "SELECT id FROM tasks") == []

            batch = await _call(client, "ingest_local_files", {
                "paths": [str(example), str(sample), str(example2026), str(fixtures / "missing.pdf")],
            })
            assert (batch["count"], batch["ok"], batch["failed"]) == (4, 3, 1)
            task_ids = [item["task_id"] for item in batch["items"][:3]]
            assert stub.parse_started.wait(10)
            listed = await _call(client, "list_tasks", {"limit": 10})
            assert [task["status"] for task in listed["items"]].count("running") == 1
            assert [task["status"] for task in listed["items"]].count("pending") == 2
            running = next(task for task in listed["items"] if task["status"] == "running")
            pending = [task for task in listed["items"] if task["status"] == "pending"]
            assert running["started_at"] is not None
            assert all(task["started_at"] is None for task in pending)

            _stop_server(server, crash=True)
            stub.block_parsing.clear()
            stub.release_parsing.set()
            server = _start_server(root, server_port, stub.server_address[1])
            finished = [await _wait_task(client, task_id) for task_id in task_ids]
            recovered_retries = finished[0]["retries"]
            assert recovered_retries == 1
            assert all(task["status"] == "ok" for task in finished)

            example_hit = await _call(client, "get_paper_by_doi", {"doi": EXAMPLE_DOI})
            sample_hit = await _call(client, "get_paper_by_doi", {"doi": SAMPLE_DOI})
            example_id = example_hit["items"][0]["id"]
            sample_id = sample_hit["items"][0]["id"]
            assert example_hit["items"][0]["dir_name"] == "example2006_population_dynamics"
            assert example_hit["items"][0]["title"] == EXAMPLE_TITLE
            assert sample_hit["items"][0]["dir_name"] == "sample2005_neural_activity"
            example2026_hit = await _call(client, "search_library", {"q": "Distinct population"})
            assert example2026_hit["count"] == 1
            assert example2026_hit["items"][0]["dir_name"] == "example2026_distinct_population"
            for paper_id in (example_id, sample_id, example2026_hit["items"][0]["id"]):
                metadata = await _call(client, "get_paper", {"paper_id": paper_id})
                assert metadata["status"]["pdf"] == "ok"
                assert metadata["status"]["parsed"] == "ok"
                assert metadata["status"]["summary"] == "ok"
                paper_dir = root / "library" / str(metadata["year"]) / metadata["dir_name"]
                assert (paper_dir / f"{metadata['dir_name']}.pdf").is_file()
                assert (paper_dir / "full.md").is_file()
                assert (paper_dir / "images" / "figure.png").is_file()

            search = await _call(client, "search_papers", {
                "source": "semantic_scholar", "query": EXAMPLE_DOI, "brief": True,
            })
            assert search["sources_tried"] == ["semantic_scholar", "openalex"]
            assert search["items"][0]["in_library"] == [example_id]
            assert "abstract" not in search["items"][0]
            fallback = await _call(client, "search_papers", {
                "source": "semantic_scholar", "query": FALLBACK_DOI, "brief": True,
            })
            assert fallback["sources_tried"] == ["semantic_scholar", "openalex", "crossref"]
            assert fallback["items"][0]["title"] == FALLBACK_TITLE
            full_search = await _call(client, "search_papers", {
                "source": "semantic_scholar", "query": "recorded sandbox query",
            })
            assert full_search["items"][0]["abstract"] == "Recorded abstract"

            batch_dois = await _call(client, "get_papers_by_dois", {
                "dois": ["https://doi.org/" + SAMPLE_DOI.upper(), "10.1234/not-present"],
            })
            assert batch_dois["items"][0]["ids"] == [sample_id]
            assert batch_dois["items"][1]["ids"] == []
            batch_ids = await _call(client, "get_papers", {
                "ids": [sample_id, example_id, 99999],
            })
            assert batch_ids["count"] == 2
            assert batch_ids["missing"] == [99999]

            hits = await _call(client, "search_fulltext", {
                "paper_id": sample_id, "query": "Fano factor", "context_chars": 30,
            })
            assert hits["total_hits"] == 1
            offset = hits["items"][0]["offset"]
            literal = await _call(client, "read_fulltext", {
                "paper_id": sample_id, "offset": offset, "limit": len("Fano factor"),
            })
            assert literal["text"] == "Fano factor"
            semantic = await _call(client, "semantic_search", {
                "paper_id": sample_id, "query": "Fano factor", "topk": 1,
            })
            assert semantic["count"] == 1, semantic
            item = semantic["items"][0]
            length = item["char_end"] - item["char_start"]
            jumped = await _call(client, "read_fulltext", {
                "paper_id": sample_id, "offset": item["char_start"], "limit": length,
            })
            embedded = _rows(root, """
                SELECT text FROM embeddings WHERE paper_id = ? AND chunk_index = ?
            """, (sample_id, item["chunk_index"]))[0]["text"]
            assert jumped["text"] == embedded

            annotation = await _call(client, "add_annotation", {
                "paper_id": sample_id, "quote": "Fano factor", "note": "sandbox replay",
            })
            assert annotation["id"]
            summary = await _call(client, "get_summary", {"paper_id": sample_id})
            assert summary["available"] is True
            _execute(root, """
                INSERT INTO citations (src_paper_id, dst_paper_id, dst_title, dst_year)
                VALUES (?, ?, 'Self', 2005)
            """, (sample_id, sample_id))
            _execute(root, """
                INSERT INTO citations (src_paper_id, dst_paper_id, dst_title, dst_year)
                VALUES (?, ?, ?, 2006)
            """, (sample_id, example_id, EXAMPLE_TITLE))
            _execute(root, """
                INSERT INTO citations (src_paper_id, dst_paper_id, dst_title, dst_year)
                VALUES (?, ?, ?, 2005)
            """, (example_id, sample_id, SAMPLE_TITLE))
            refs = await _call(client, "get_citations", {
                "paper_id": sample_id, "direction": "refs",
            })
            cited_by = await _call(client, "get_citations", {
                "paper_id": sample_id, "direction": "cited_by",
            })
            assert [row["dst_paper_id"] for row in refs["items"]] == [example_id]
            assert [row["dst_paper_id"] for row in cited_by["items"]] == [example_id]

            new_name = "sample2005_parietal_cortex"
            old_dir = root / "library" / "2005" / "sample2005_neural_activity"
            plan = _cli(root, server_port, stub.server_address[1],
                        "db", "rename", str(sample_id), "--dir-name", new_name, "--dry-run")
            assert "dry-run" in plan
            assert old_dir.is_dir()
            _cli(root, server_port, stub.server_address[1],
                 "db", "rename", str(sample_id), "--dir-name", new_name)
            new_dir = old_dir.parent / new_name
            assert not old_dir.exists()
            assert (new_dir / f"{new_name}.pdf").is_file()
            assert (new_dir / "full.md").is_file()
            assert (new_dir / "images" / "figure.png").is_file()
            renamed = await _call(client, "get_paper", {"paper_id": sample_id})
            assert renamed["dir_name"] == new_name
            assert (await _call(client, "get_summary", {"paper_id": sample_id}))["available"]
            assert (await _call(client, "get_annotations", {"paper_id": sample_id}))["count"] == 1
            assert _rows(root, "SELECT id FROM embeddings WHERE paper_id = ?", (sample_id,))

            duplicate = fixtures / "duplicate.pdf"
            duplicate.write_bytes(example.read_bytes())
            si = fixtures / "SI_example2006.pdf"
            _make_pdf(si, "Supplementary Information\n" + EXAMPLE_DOI)
            dedup = await _call(client, "ingest_local_files", {
                "paths": [str(duplicate), str(si)], "steps": ["ingest"],
            })
            dedup_tasks = [await _wait_task(client, item["task_id"])
                           for item in dedup["items"]]
            assert dedup_tasks[0]["paper_id"] == example_id
            si_id = dedup_tasks[1]["paper_id"]
            assert si_id != example_id
            si_row = _rows(root, """
                SELECT attachment_kind, parent_paper_id, status_summary
                FROM papers WHERE id = ?
            """, (si_id,))[0]
            assert si_row == {
                "attachment_kind": "si", "parent_paper_id": example_id,
                "status_summary": "missing",
            }
            si_duplicate = fixtures / "SI_duplicate.pdf"
            si_duplicate.write_bytes(si.read_bytes())
            repeated = await _call(client, "ingest_local_file", {
                "path": str(si_duplicate), "steps": ["ingest"],
            })
            assert (await _wait_task(client, repeated["task_id"]))["paper_id"] == si_id

            downloaded = await _call(client, "ingest_by_url", {
                "url": "https://paper.sandbox.test/download/sample2005.pdf",
                "filename": "sample2005_download.pdf", "steps": ["ingest"],
            })
            assert "task_id" in downloaded, downloaded
            assert (await _wait_task(client, downloaded["task_id"]))["paper_id"] == sample_id
            successful_download = next(
                call for call in stub.downloads if call["path"] == "/download/sample2005.pdf"
            )
            assert successful_download["user_agent"] == f"paper-survey/{get_version()}"
            assert "application/pdf" in successful_download["accept"]

            inbox = root / "inbox"
            inbox.mkdir(exist_ok=True)
            (inbox / "duplicate.pdf").write_bytes(example.read_bytes())
            scan_pdf = inbox / "sample1992.pdf"
            _make_pdf(scan_pdf, SCAN_TITLE + "\nSample, Coauthor\n" + SCAN_DOI)
            risky = inbox / "no_year.pdf"
            _make_pdf(risky, "short body without identifiers")
            task_count = len(_rows(root, "SELECT id FROM tasks"))
            dry_scan = _cli(root, server_port, stub.server_address[1],
                            "inbox", "scan", "--dry-run", "--steps", "ingest")
            assert "预计 year unknown" in dry_scan
            assert "重复" in dry_scan
            assert len(_rows(root, "SELECT id FROM tasks")) == task_count
            risky.unlink()
            _cli(root, server_port, stub.server_address[1],
                 "inbox", "scan", "--force", "--steps", "ingest")
            new_tasks = _rows(root, "SELECT id FROM tasks WHERE id > ?", (task_count,))
            assert len(new_tasks) == 1
            scan_task = await _wait_task(client, new_tasks[0]["id"])
            assert (await _call(client, "get_paper_by_doi", {
                "doi": SCAN_DOI,
            }))["items"][0]["id"] == scan_task["paper_id"]

            cache_check = subprocess.run([
                sys.executable, "-m", "backend.tests.sandbox_runtime", "cache-check",
                "--root", str(root), "--stub-port", str(stub.server_address[1]),
            ], cwd=PROJECT_ROOT, env=_env(), capture_output=True, text=True, timeout=30)
            assert cache_check.returncode == 0, cache_check.stdout + cache_check.stderr
            cache_result = json.loads(cache_check.stdout)
            assert cache_result["ok"] is True
            assert cache_result["corrupt_cache_discarded"] is True
            assert Path(cache_result["cache_dir"]).is_relative_to(root)
            assert stub.json_calls == 2

            doctor = _cli(root, server_port, stub.server_address[1], "doctor")
            assert f"DB path:      {root / 'papers.db'}" in doctor
            assert "pending=0 running=0" in doctor
            assert "last recover: requeued=1 failed=0" in doctor
            assert "Flask alive:  ✅" in doctor
            assert "返回 2 维" in doctor
            assert "MCP process:" in doctor
            assert "paper 99999 not found" in (
                await _call(client, "get_paper", {"paper_id": 99999})
            )["error"]
            assert "error" in await _call(client, "list_tasks", {"status": "bogus"})

            final_rows = _rows(root, """
                SELECT status, COUNT(*) AS count FROM tasks GROUP BY STATUS
            """)
            assert final_rows == [{"status": "ok", "count": 8}], final_rows
            print("SANDBOX_REPLAY", json.dumps({
                "tools": len(tools),
                "pipeline_tasks": len(task_ids),
                "recovered_retries": recovered_retries,
                "papers": len(_rows(root, "SELECT id FROM papers")),
                "tasks": final_rows,
                "download_replays": len(original_downloads) + 1,
                "mcp_stdio": "ok",
                "db": str(root / "papers.db"),
            }, ensure_ascii=False))
    finally:
        stub.release_parsing.set()
        _stop_server(server)


def test_mcp_stdio_parallel_database_replay(tmp_path, stub_server):
    root = tmp_path / "parallel-runtime"
    root.mkdir()
    asyncio.run(_exercise(root, stub_server, _free_port()))
