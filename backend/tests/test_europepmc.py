"""Test: EuropePmcClient 解析与打分制标题反查（3.1.1 P1-3）。

不访问网络：instance 级替换 get / _search 返回假数据，验证 URL 构造、
abstractText 提取（剥标记）与采纳/拒绝规则。
"""
import json

import requests
from unittest.mock import patch


def _make_client(tmp_path):
    from backend.external.europepmc import EuropePmcClient
    with patch.object(EuropePmcClient, "__init__", lambda self: None):
        c = EuropePmcClient()
    c._cache_dir = tmp_path / "europepmc"
    c._cache_dir.mkdir(parents=True, exist_ok=True)
    c._last_req = 0.0
    return c


def _fake_response(payload: dict, status: int = 200) -> requests.Response:
    r = requests.Response()
    r.status_code = status
    r._content = json.dumps(payload).encode()
    r.headers["Content-Type"] = "application/json"
    r.encoding = "utf-8"
    return r


def _work(title="A Paper About Probes", abstract="We study probes.", year="2024",
          doi="10.1/x", authors="Liu B, Sacks J"):
    return {"title": title, "abstractText": abstract, "pubYear": year,
            "doi": doi, "authorString": authors}


def test_get_work_by_doi_builds_url_and_parses(tmp_path):
    c = _make_client(tmp_path)
    calls = []

    def fake_get(url, **kw):
        calls.append(url)
        return _fake_response({"resultList": {"result": [_work()]}})

    c.get = fake_get
    w = c.get_work_by_doi("10.1234/example.2018.22")
    assert w["title"] == "A Paper About Probes"
    assert len(calls) == 1
    assert "query=" in calls[0]
    assert "10.1234%2Fexample.2018.22" in calls[0] or "10.1234/example.2018.22" in calls[0]
    assert "resultType=core" in calls[0]


def test_get_work_by_doi_empty_cases(tmp_path):
    c = _make_client(tmp_path)
    assert c.get_work_by_doi("") == {}
    assert c.get_work_by_doi("   ") == {}
    c.get = lambda url, **kw: _fake_response({"resultList": {"result": []}})
    assert c.get_work_by_doi("10.1/missing") == {}
    c.get = lambda url, **kw: _fake_response({}, status=500)
    assert c.get_work_by_doi("10.1/err") == {}


def test_fetch_abstract_strips_markup(tmp_path):
    c = _make_client(tmp_path)
    c.get_work_by_doi = lambda doi: _work(
        abstract="<h4>Background</h4>We study <i>probes</i> in cortex.")
    assert c.fetch_abstract("10.1/x") == "Background We study probes in cortex."


def test_fetch_abstract_missing(tmp_path):
    c = _make_client(tmp_path)
    c.get_work_by_doi = lambda doi: {}
    assert c.fetch_abstract("10.1/x") == ""
    c.get_work_by_doi = lambda doi: _work(abstract="")
    assert c.fetch_abstract("10.1/x") == ""


def test_title_lookup_accepts_matching_work(tmp_path):
    c = _make_client(tmp_path)
    c._search = lambda query, page_size=5: [
        _work(title="A Paper About Probes", year="2023", authors="Liu B, Sacks J"),
    ]
    w = c.search_work_by_title("a paper about probes", year=2024, author="liu")
    assert w["title"] == "A Paper About Probes"


def test_title_lookup_rejects_mismatches(tmp_path):
    c = _make_client(tmp_path)
    # 标题相似度不足
    c._search = lambda query, page_size=5: [_work(title="Totally Different Subject")]
    assert c.search_work_by_title("A Paper About Probes", year=2024) == {}
    # 年份差超过 1
    c._search = lambda query, page_size=5: [
        _work(title="A Paper About Probes", year="2010"),
    ]
    assert c.search_work_by_title("A Paper About Probes", year=2024) == {}
    # 第一作者姓不在 authorString
    c._search = lambda query, page_size=5: [
        _work(title="A Paper About Probes", year="2024", authors="Golub MD"),
    ]
    assert c.search_work_by_title("A Paper About Probes", year=2024, author="liu") == {}
    # 无候选
    c._search = lambda query, page_size=5: []
    assert c.search_work_by_title("A Paper About Probes") == {}
    # 空标题不触网
    c._search = lambda query, page_size=5: (_ for _ in ()).throw(AssertionError("should not search"))
    assert c.search_work_by_title("") == {}


def test_title_lookup_empty_signals_skip_checks(tmp_path):
    """EPMC 侧年份/作者缺失时不做对应检查（只靠标题相似度）。"""
    c = _make_client(tmp_path)
    c._search = lambda query, page_size=5: [
        _work(title="A Paper About Probes", year="", authors=""),
    ]
    w = c.search_work_by_title("A Paper About Probes", year=2024, author="liu")
    assert w["title"] == "A Paper About Probes"


def test_extract_abstract_static(tmp_path):
    from backend.external.europepmc import EuropePmcClient
    assert EuropePmcClient.extract_abstract(_work(abstract="Plain text.")) == "Plain text."
    assert EuropePmcClient.extract_abstract({}) == ""
    assert EuropePmcClient.extract_abstract(None) == ""


def test_backfill_fetch_abstract_source_order(tmp_path):
    """三源顺序：OpenAlex -> Crossref -> Europe PMC；无标识符走标题反查。"""
    from scripts.backfill_abstracts import _fetch_abstract

    paper = {"doi": "10.1/x", "openalex_id": "", "title": "A Paper About Probes",
             "year": 2024, "first_author": "liu"}

    class _OAC:
        def __init__(self, work):
            self.work = work
        def get_work(self, doi):
            return self.work
        def get_work_by_openalex_id(self, oid):
            return {}
        def extract_work_info(self, work):
            return {"abstract": (work or {}).get("abstractText", ""), "doi": "10.1/x"}
    class _CC:
        def __init__(self, ab):
            self.ab = ab
        def fetch_abstract(self, doi):
            return self.ab
    class _EPMC:
        def __init__(self, ab="", work=None):
            self.ab, self.work = ab, work
        def fetch_abstract(self, doi):
            return self.ab
        def search_work_by_title(self, title, year=None, author=None):
            return self.work or {}
        def extract_abstract(self, work):
            return (work or {}).get("abstractText", "")

    # OpenAlex 命中即止
    ab, src = _fetch_abstract(paper, _OAC({"abstractText": "oa"}), _CC("cr"), _EPMC("ep"))
    assert (ab, src) == ("oa", "openalex")
    # OpenAlex 无 -> Crossref
    ab, src = _fetch_abstract(paper, _OAC({}), _CC("cr"), _EPMC("ep"))
    assert (ab, src) == ("cr", "crossref")
    # 前两源空 -> Europe PMC
    ab, src = _fetch_abstract(paper, _OAC({}), _CC(""), _EPMC("ep"))
    assert (ab, src) == ("ep", "europepmc")
    # 无标识符且三源空 -> 标题反查
    paper2 = {"doi": "", "openalex_id": "", "title": "A Paper About Probes",
              "year": 2024, "first_author": "liu"}
    ab, src = _fetch_abstract(paper2, _OAC({}), _CC(""), _EPMC("", _work(abstract="title hit")))
    assert (ab, src) == ("title hit", "europepmc_title")
    # 有标识符时不走标题反查
    ab, src = _fetch_abstract(paper, _OAC({}), _CC(""), _EPMC("", _work(abstract="title hit")))
    assert (ab, src) == ("", "")
