"""Test: OpenAlexClient.get_cited_by_dois_batch 的 URL 构造与结果解析。

filter 键必须只出现一次（doi:a|b）；doi:a|doi:b 会触发 OpenAlex 400
"OR query between filters is not supported"（2026-10-26 首跑实测，
静默跳过会把全库误报成 oa_missing）。
"""


class _FakeResp:
    def __init__(self, payload, status_code=200):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload

    @property
    def text(self):
        return str(self._payload)


def test_batch_doi_filter_key_appears_once(monkeypatch):
    from backend.external.openalex import OpenAlexClient

    captured = {}

    def fake_get(self, url, timeout=None):  # noqa: ARG001
        captured["url"] = url
        return _FakeResp({"results": [
            {"id": "https://openalex.org/W1", "doi": "https://doi.org/10.1/a",
             "cited_by_count": 3},
            {"id": "https://openalex.org/W2", "doi": "https://doi.org/10.1/B",
             "cited_by_count": 0},
        ]})

    monkeypatch.setattr(OpenAlexClient, "get", fake_get)
    oac = OpenAlexClient()
    out = oac.get_cited_by_dois_batch(["10.1/a", "10.1/B", " "])

    assert captured["url"].count("doi:") == 1
    assert "10.1/a|10.1/B" in captured["url"]
    assert out == {
        "10.1/a": {"openalex_id": "https://openalex.org/W1", "cited_by_count": 3},
        "10.1/b": {"openalex_id": "https://openalex.org/W2", "cited_by_count": 0},
    }


def test_batch_doi_400_raises_instead_of_skip(monkeypatch):
    """400 是构造性错误，必须抛出而不是静默跳过（否则全库误报 oa_missing）。"""
    from backend.external.openalex import OpenAlexClient

    def fake_get(self, url, timeout=None):  # noqa: ARG001
        return _FakeResp({"error": "Invalid query parameters error."},
                         status_code=400)

    monkeypatch.setattr(OpenAlexClient, "get", fake_get)
    import pytest
    with pytest.raises(RuntimeError, match="OpenAlex 400"):
        OpenAlexClient().get_cited_by_dois_batch(["10.1/a"])


def test_batch_doi_403_raises_instead_of_skip(monkeypatch):
    from backend.external.openalex import OpenAlexClient

    def fake_get(self, url, timeout=None):  # noqa: ARG001
        return _FakeResp({"error": "forbidden"}, status_code=403)

    monkeypatch.setattr(OpenAlexClient, "get", fake_get)
    import pytest
    with pytest.raises(RuntimeError, match="OpenAlex 403"):
        OpenAlexClient().get_cited_by_dois_batch(["10.1/a"])


def test_get_work_only_404_means_missing(monkeypatch):
    from backend.external.openalex import OpenAlexClient
    responses = iter([404, 403])

    def fake_get(self, url, timeout=None):  # noqa: ARG001
        return _FakeResp({"error": "request failed"}, status_code=next(responses))

    monkeypatch.setattr(OpenAlexClient, "get", fake_get)
    client = OpenAlexClient()
    assert client.get_work("10.1/missing") == {}

    import pytest
    with pytest.raises(RuntimeError, match="OpenAlex 403"):
        client.get_work("10.1/forbidden")
