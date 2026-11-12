"""Test: CrossrefClient.fetch_bibtex content negotiation + 缓存。

不访问网络：mock _session.get 返回假 Response，验证解析、缓存命中、异常输入防御。
"""
import requests
from unittest.mock import patch, MagicMock


BIBTEX_SAMPLE = (
    "@article{Smith_2023,\n"
    " doi = {10.1038/test},\n"
    " title = {Hello World},\n"
    " journal = {Nature},\n"
    " year = {2023}\n"
    "}\n"
)


def _make_client(tmp_path):
    from backend.external.crossref import CrossrefClient
    with patch.object(CrossrefClient, "__init__", lambda self: None):
        c = CrossrefClient()
    c._session = MagicMock()
    c._cache_dir = tmp_path / "crossref"
    c._cache_dir.mkdir(parents=True, exist_ok=True)
    c._last_req = 0.0
    return c


def _fake_response(text, status=200):
    r = requests.Response()
    r.status_code = status
    r._content = text.encode()
    r.encoding = "utf-8"
    return r


def test_empty_doi_returns_none(tmp_path):
    c = _make_client(tmp_path)
    assert c.fetch_bibtex("") is None
    assert c.fetch_bibtex("   ") is None
    c._session.get.assert_not_called()


def test_fetch_success(tmp_path):
    c = _make_client(tmp_path)
    c._session.get.return_value = _fake_response(BIBTEX_SAMPLE)
    result = c.fetch_bibtex("10.1038/test")
    assert result == BIBTEX_SAMPLE.strip()
    c._session.get.assert_called_once()
    url = c._session.get.call_args[0][0]
    assert "10.1038/test" in url
    assert "x-bibtex" in url
    assert c._session.get.call_args[1]["headers"]["Accept"] == "application/x-bibtex"


def test_cache_hit_skips_network(tmp_path):
    c = _make_client(tmp_path)
    c._session.get.return_value = _fake_response(BIBTEX_SAMPLE)
    first = c.fetch_bibtex("10.1038/test")
    assert first == BIBTEX_SAMPLE.strip()
    c._session.get.reset_mock()
    # 第二次应命中缓存，不触网
    second = c.fetch_bibtex("10.1038/test")
    assert second == BIBTEX_SAMPLE.strip()
    c._session.get.assert_not_called()


def test_non_bibtex_response_rejected(tmp_path):
    """错误页 HTML 不应被当作 BibTeX 入库。"""
    c = _make_client(tmp_path)
    c._session.get.return_value = _fake_response("<html>Not Found</html>")
    assert c.fetch_bibtex("10.1038/bad") is None


def test_empty_body_rejected(tmp_path):
    c = _make_client(tmp_path)
    c._session.get.return_value = _fake_response("")
    assert c.fetch_bibtex("10.1038/empty") is None


def test_http_error_returns_none(tmp_path):
    c = _make_client(tmp_path)
    c._session.get.return_value = _fake_response("", status=404)
    assert c.fetch_bibtex("10.1038/missing") is None


def test_failed_fetch_not_cached(tmp_path):
    """404 后不缓存，下次仍会触网（允许后续重试）。"""
    c = _make_client(tmp_path)
    c._session.get.return_value = _fake_response("", status=404)
    assert c.fetch_bibtex("10.1038/missing") is None
    # 缓存目录应为空（未写入）
    assert list(c._cache_dir.iterdir()) == []


def test_strip_jats():
    """JATS 标签剥离 + 实体反转义 + 空白折叠；空输入返回空串。"""
    from backend.external.crossref import strip_jats
    assert strip_jats("<jats:title>Abstract</jats:title><jats:p>x&lt;y &amp; z</jats:p>") == "Abstract x<y & z"
    assert strip_jats("  multiple   spaces\n\nnewlines  ") == "multiple spaces newlines"
    assert strip_jats("") == ""
    assert strip_jats(None) == ""


def test_fetch_abstract_strips_jats(tmp_path):
    """fetch_abstract 经 get_work 取 message.abstract 并剥 JATS 标签。"""
    c = _make_client(tmp_path)
    c.get_work = lambda doi: {
        "abstract": "<jats:p>An abstract about &amp; probes.</jats:p>\n<jats:p>Second sentence.</jats:p>"
    }
    assert c.fetch_abstract("10.1038/test") == "An abstract about & probes. Second sentence."


def test_fetch_abstract_empty_cases(tmp_path):
    """无 DOI / get_work 失败 / message 无 abstract：一律空串不抛异常。"""
    c = _make_client(tmp_path)
    assert c.fetch_abstract("") == ""
    assert c.fetch_abstract("   ") == ""
    c.get_work = lambda doi: None
    assert c.fetch_abstract("10.1038/missing") == ""
    c.get_work = lambda doi: {"title": "No Abstract Here"}
    assert c.fetch_abstract("10.1038/noabs") == ""
