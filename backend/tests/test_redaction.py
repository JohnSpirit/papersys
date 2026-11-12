"""错误信息凭据脱敏的单元测试。"""
from backend.services.redaction import redact_secrets


def test_redacts_query_api_key():
    text = "GET https://api.openalex.org/works?search=x&api_key=SECRET -> 429"
    redacted = redact_secrets(text)
    assert "SECRET" not in redacted
    assert "api_key=***" in redacted


def test_redacts_bearer_token():
    text = "Authorization: Bearer abc.def-123"
    assert redact_secrets(text) == "Authorization: Bearer ***"


def test_redacts_x_api_key_header():
    text = "x-api-key: SECRET123"
    assert redact_secrets(text) == "x-api-key: ***"


def test_leaves_normal_text_unchanged():
    text = "GET https://api.example/works?search=memory -> 200"
    assert redact_secrets(text) == text
