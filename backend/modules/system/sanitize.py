"""Bounded log filtering. Users must still review arbitrary free-form text."""
import re

_HEX4 = r'[0-9A-Fa-f]{1,4}'
_PATTERNS = [
    re.compile(r'eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+'),
    # Scoped tokens are odin_ + 43 url-safe characters ('-' and '_' included): the whole token.
    re.compile(r'\bodin_[A-Za-z0-9_-]{40,}'),
    re.compile(r'(?i)([?&](?:token|access_token|api_key|apikey|key|ws_token|oidc_code|code|state|nonce|password|refresh_token|id_token|client_secret)=)[^&\s"\'#]+'),
    re.compile(r'(?i)\bx-api-key\b["\']?\s*[:=]\s*["\']?[^\s"\',;}]+'),
    re.compile(r"(?i)[\"']?\b(?:authorization|proxy-authorization)[\"']?\s*[:=]\s*[\"']?(?:bearer|basic)?\s*[^\s,;\"']+"),
    # Bearer tokens anywhere in a line, not only after an Authorization label.
    re.compile(r'(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+'),
    # Credentials in any scheme://user:pass@ URL (postgresql, mysql+driver, mqtt(s), redis, amqp, http, rtsp, ...).
    re.compile(r'(?i)\b[a-z][a-z0-9+.-]*://[^\s/@:]*:[^\s/@]+@'),
    # Secret labels and env-style assignments: anything containing secret/password/passwd/token,
    # *_KEY (api_key, AWS_SECRET_ACCESS_KEY, ENCRYPTION_KEY), *_PASS / PASS, pw / pwd, access codes,
    # credentials, cookies and serials; snake/kebab-case prefixes included (smtp_password).
    re.compile(r"""(?ix)["']?(?<![A-Za-z0-9])
        (?:[A-Za-z0-9_-]*(?:secret|password|passwd|token|credential)[A-Za-z0-9_-]*
          |(?:[A-Za-z0-9]+[_-])+key|api[_-]?key
          |(?:[A-Za-z0-9]+[_-])*pass|pw|pwd
          |(?:[A-Za-z0-9]+[_-])*(?:access[_-]?code|cookie|serial(?:_number)?))
        (?![A-Za-z0-9])["']?\s*[:=]\s*(?:"[^"\n]*"|'[^'\n]*'|[^\s,;}]+)"""),
    re.compile(r'(?i)\b(?:PASS|USER)\s+[^\s]+'),
    re.compile(r'\b\d{1,3}(?:\.\d{1,3}){3}\b'),
    # IPv6: full 8-group form, or a ::-compressed form that carries at least one
    # 3-4 digit group (fe80::1, fd00:abcd:12::41) -- never a::b, 1:: or hh:mm:ss.
    re.compile(r'(?<![0-9A-Za-z:.])(?:(?:' + _HEX4 + r':){7}' + _HEX4 +
               r'|(?=[0-9A-Fa-f:]*[0-9A-Fa-f]{3})(?:' + _HEX4 + r':){0,6}(?:' + _HEX4 + r')?::(?:' + _HEX4 + r':){0,6}(?:' + _HEX4 + r')?)'
               r'(?:%[0-9A-Za-z]+)?(?![0-9A-Za-z:])'),
    re.compile(r'(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b'),
]


# One-time login material in URLs (OIDC callback ?code=&state=, ODIN's own
# oidc_code, nonces and OAuth tokens). Keeps the parameter name for debugging.
_URL_SECRET_PARAMS = re.compile(
    r'(?i)([?&#;](?:oidc_code|code|state|nonce|id_token|access_token|refresh_token|client_secret)=)[^&\s"\'#;]*')


def sanitize_log_line(line: str) -> str:
    line = _URL_SECRET_PARAMS.sub(r'\1[REDACTED]', line)
    for pattern in _PATTERNS:
        line = pattern.sub('[REDACTED]', line)
    return line


def sanitize_log_lines(lines: list[str]) -> list[str]:
    return [sanitize_log_line(line) for line in lines]


def sanitize_structure(value):
    """Recursively filter exception-buffer strings as well as log lines."""
    if isinstance(value, dict):
        return {k: ('[REDACTED]' if re.search(r'(?i)password|secret|token|access.?code|api.?key|credential|authorization|cookie', str(k))
                    else sanitize_structure(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_structure(v) for v in value]
    return sanitize_log_line(value) if isinstance(value, str) else value
