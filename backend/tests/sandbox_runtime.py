"""沙箱重放的子进程入口；所有路径和外部端点都指向临时目录。"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from urllib.parse import urlsplit

EXAMPLE_DOI = "10.1234/example.2006.01"
EXAMPLE_TITLE = (
    "Population dynamics in association cortex during "
    "a perceptual decision task"
)
SAMPLE_DOI = "10.1234/sample.2005.02"
SAMPLE_TITLE = "Neural activity in parietal cortex during flexible decisions"
OVERLEAF_TITLE = (
    "Distinct population dynamics in prefrontal and premotor cortex "
    "during flexible decisions"
)
FALLBACK_DOI = "10.1234/example.2009.03"
FALLBACK_TITLE = "Factor analysis of single-trial neural population activity"
SCAN_DOI = "10.1234/sample.1992.04"
SCAN_TITLE = "Microstimulation in visual cortex during a detection task"

_RECORDS = {
    EXAMPLE_DOI.lower(): (EXAMPLE_TITLE, 2006, "Example"),
    SAMPLE_DOI.lower(): (SAMPLE_TITLE, 2005, "Sample"),
    FALLBACK_DOI.lower(): (FALLBACK_TITLE, 2009, "Example"),
    SCAN_DOI.lower(): (SCAN_TITLE, 1992, "Sample"),
}


def bootstrap(root: str, stub_port: int, server_port: int = 5099) -> None:
    path_sensitive = (
        "backend.db.conn",
        "backend.db.tasks_repo",
        "backend.external.base",
        "backend.external.mineru",
        "backend.workers.runner",
    )
    premature = [name for name in path_sensitive if name in sys.modules]
    if premature:
        raise RuntimeError(f"sandbox modules imported before path isolation: {premature}")
    from backend.config import CONFIG

    base = Path(root).expanduser().resolve()
    paths = CONFIG["paths"]
    paths.update({
        "data_dir": str(base),
        "db_path": str(base / "papers.db"),
        "cache_dir": str(base / "cache"),
        "log_dir": str(base / "logs"),
        "library_root": str(base / "library"),
        "inbox_dir": str(base / "inbox"),
        "nas_base": str(base / "nas-read-only"),
    })
    CONFIG["workers"].update({
        "default_concurrency": 1,
        "summary_concurrency": 1,
        "translate_concurrency": 1,
        "max_task_retries": 3,
    })
    CONFIG["mineru"].update({
        "mode": "local",
        "local_api_base": f"http://127.0.0.1:{stub_port}",
        "local_timeout": 30,
        "local_backend": "sandbox-parser",
    })
    CONFIG["retrieval"].update({
        "embed_base_url": f"http://127.0.0.1:{stub_port}/v1",
        "rerank_base_url": f"http://127.0.0.1:{stub_port}/v1",
        "embed_model": "sandbox-embed",
        "rerank_model": "sandbox-rerank",
        "embed_dimensions": 2,
        "embed_timeout": 5,
        "rerank_timeout": 5,
        "rerank_threshold": 0.5,
    })
    CONFIG["llm"].update({
        "api_url": f"http://127.0.0.1:{stub_port}/v1/chat/completions",
        "model": "sandbox-summary",
    })
    CONFIG["server"].update({
        "host": "127.0.0.1", "port": server_port, "debug": False,
        "health_url": f"http://127.0.0.1:{server_port}/api/health",
    })
    CONFIG["secrets"] = {key: "sandbox-local-only" for key in CONFIG["secrets"]}
    for key in ("data_dir", "db_path", "cache_dir", "log_dir", "library_root", "inbox_dir"):
        assert Path(paths[key]).is_relative_to(base), (key, paths[key])


def _crossref_record(doi: str) -> dict:
    title, year, family = _RECORDS[doi.lower()]
    return {
        "title": [title],
        "DOI": doi,
        "type": "journal-article",
        "issued": {"date-parts": [[year]]},
        "author": [{"given": "Sandbox", "family": family}],
        "container-title": ["Sandbox Journal"],
        "abstract": "Recorded abstract for sandbox search.",
    }


def _openalex_work(title: str, year: int, family: str, doi: str = "") -> dict:
    return {
        "id": f"https://openalex.org/W{abs(year)}",
        "title": title,
        "doi": f"https://doi.org/{doi}" if doi else "",
        "publication_year": year,
        "type": "article",
        "authorships": (
            [{"author": {"display_name": f"Sandbox {family}"}}] if family else []
        ),
        "primary_location": {"source": {"display_name": "Sandbox Journal"}},
    }


def _work_for_doi(doi: str) -> dict:
    record = _RECORDS.get(doi.lower())
    if not record:
        return {}
    title, year, family = record
    return _openalex_work(title, year, family, doi)


class FixtureCrossrefClient:
    def get_work(self, doi):
        return _crossref_record(doi) if doi.lower() in _RECORDS else {}

    def search_best(self, query, **kwargs):
        return None

    def extract_doi(self, item):
        return item.get("DOI")

    def fetch_bibtex(self, doi):
        return f"@article{{sandbox, doi={{{doi}}}}}"

    def fetch_abstract(self, doi):
        return None


class _OpenAlexMethods:
    pass


class IngestOpenAlexClient(_OpenAlexMethods):
    def get_work(self, doi):
        return {}

    def search_work(self, title, year=None, author=None):
        if title == OVERLEAF_TITLE:
            return _openalex_work(OVERLEAF_TITLE, 2026, "")
        return {}


class EnrichOpenAlexClient(_OpenAlexMethods):
    def get_work(self, doi):
        return _work_for_doi(doi)

    def search_work(self, title, year=None, author=None):
        if title == OVERLEAF_TITLE:
            return _openalex_work(OVERLEAF_TITLE, 2026, "")
        return {}

    def search_work_by_author_year(self, author, year):
        return {}


class SearchOpenAlexClient(_OpenAlexMethods):
    def get_work(self, doi):
        if doi.lower() == FALLBACK_DOI.lower():
            return {}
        return _work_for_doi(doi)


class FixtureSemanticScholarClient:
    def by_doi(self, doi):
        return None

    def search(self, query, *, limit=20, year=None):
        return [{
            "source": "semantic_scholar",
            "title": "Recorded sandbox search result",
            "doi": EXAMPLE_DOI,
            "abstract": "Recorded abstract",
            "tldr": "Recorded summary",
            "authors": ["Sandbox Example"],
            "year": 2006,
        }]


class FixtureEuropePmcClient:
    def fetch_abstract(self, doi):
        return None


def install_fixture_clients() -> None:
    import backend.external.crossref as crossref
    import backend.external.europepmc as europepmc
    import backend.external.openalex as openalex
    import backend.external.semantic_scholar as semantic_scholar
    import backend.workers.enrich_worker as enrich_worker
    import backend.workers.ingest_worker as ingest_worker

    _OpenAlexMethods.extract_work_info = staticmethod(
        openalex.OpenAlexClient.extract_work_info,
    )
    _OpenAlexMethods.title_similarity = staticmethod(
        openalex.OpenAlexClient.title_similarity,
    )
    crossref.CrossrefClient = FixtureCrossrefClient
    openalex.OpenAlexClient = SearchOpenAlexClient
    semantic_scholar.SemanticScholarClient = FixtureSemanticScholarClient
    europepmc.EuropePmcClient = FixtureEuropePmcClient
    ingest_worker.CrossrefClient = FixtureCrossrefClient
    ingest_worker.OpenAlexClient = IngestOpenAlexClient
    enrich_worker.OpenAlexClient = EnrichOpenAlexClient


def install_sandbox_download(stub_port: int) -> None:
    import requests
    import backend.services.pdf_download as download

    original_get = requests.get

    def validate(url):
        if urlsplit(url).hostname == "paper.sandbox.test":
            return url
        raise download.UrlValidationError("sandbox blocked a non-fixture URL")

    def get(url, **kwargs):
        parts = urlsplit(url)
        if parts.hostname != "paper.sandbox.test":
            raise requests.ConnectionError("sandbox blocked a non-fixture URL")
        headers = dict(kwargs.pop("headers", {}) or {})
        headers["X-Sandbox-Original-Url"] = url
        target = f"http://127.0.0.1:{stub_port}{parts.path}"
        if parts.query:
            target += "?" + parts.query
        return original_get(target, headers=headers, **kwargs)

    download._validate_url = validate
    download.requests.get = get


def run_mcp(root: str, stub_port: int) -> None:
    bootstrap(root, stub_port)
    install_fixture_clients()
    install_sandbox_download(stub_port)
    from backend.db.conn import init_db
    from backend.mcp_server import main

    init_db()
    main()


def run_server(root: str, stub_port: int, server_port: int) -> None:
    bootstrap(root, stub_port, server_port)
    install_fixture_clients()
    from backend.workers.runner import scheduler

    scheduler.poll_interval = 0.02
    from backend.app import app

    app.run(host="127.0.0.1", port=server_port, debug=False, use_reloader=False)


def run_cli(root: str, stub_port: int, server_port: int, argv: list[str]) -> None:
    bootstrap(root, stub_port, server_port)
    from backend.cli import main

    sys.argv = ["paper", *argv]
    main()


def run_cache_check(root: str, stub_port: int) -> None:
    import json

    bootstrap(root, stub_port)
    from backend.external import base as base_mod

    assert base_mod.CACHE_DIR.resolve().is_relative_to(Path(root).resolve())

    class CacheClient(base_mod.RateLimitedClient):
        service_name = "sandbox_cache"
        rps = 1000

    client = CacheClient()
    url = f"http://127.0.0.1:{stub_port}/json"
    first = client.get(url).json()
    cache_files = list(client._cache_dir.glob("*.json"))
    assert len(cache_files) == 1
    cache_files[0].write_text('{"truncated":', encoding="utf-8")
    second_response = client.get(url)
    second = second_response.json()
    assert first == second == {"ok": True}
    assert getattr(second_response, "from_cache", False) is not True
    assert json.loads(cache_files[0].read_text(encoding="utf-8")) == {"ok": True}
    print(json.dumps({
        "ok": True,
        "corrupt_cache_discarded": True,
        "cache_dir": str(base_mod.CACHE_DIR),
    }))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("mcp", "serve", "cli", "cache-check"))
    parser.add_argument("--root", required=True)
    parser.add_argument("--stub-port", required=True, type=int)
    parser.add_argument("--server-port", type=int, default=5099)
    ns, extra = parser.parse_known_args()
    if ns.mode == "mcp":
        run_mcp(ns.root, ns.stub_port)
    elif ns.mode == "serve":
        run_server(ns.root, ns.stub_port, ns.server_port)
    elif ns.mode == "cli":
        run_cli(ns.root, ns.stub_port, ns.server_port,
                extra[1:] if extra[:1] == ["--"] else extra)
    else:
        run_cache_check(ns.root, ns.stub_port)


if __name__ == "__main__":
    main()
