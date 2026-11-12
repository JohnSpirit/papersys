"""错误信息中的凭据脱敏。

外部 API 可能把密钥放在查询串（OpenAlex 的 api_key）或请求头
（Authorization、x-api-key）。异常信息会写入任务错误、事件与日志，
这些位置对用户和 MCP 客户端可见，因此在落库与输出前统一脱敏。
"""
import re

_REDACTED = "***"

_QUERY_SECRET_RE = re.compile(
    r"(?i)([?&](?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|key)=)"
    r"[^&\s\"'<>]+"
)
_BEARER_RE = re.compile(r"(?i)(Bearer\s+)[A-Za-z0-9._~+/=-]+")
_X_API_KEY_RE = re.compile(r"(?i)(x-api-key\s*[:=]\s*)[^\s,;\"']+")
_AUTH_HEADER_RE = re.compile(r"(?i)(authorization\s*[:=]\s*)(?!Bearer\b)[^\s,;\"']+")


def redact_secrets(text: str) -> str:
    """把文本中的查询串密钥、Bearer token 和 API key 头替换为 ***。"""
    if not text:
        return text
    text = _QUERY_SECRET_RE.sub(rf"\1{_REDACTED}", text)
    text = _BEARER_RE.sub(rf"\1{_REDACTED}", text)
    text = _X_API_KEY_RE.sub(rf"\1{_REDACTED}", text)
    text = _AUTH_HEADER_RE.sub(rf"\1{_REDACTED}", text)
    return text
