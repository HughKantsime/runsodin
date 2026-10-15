"""Bounded log filtering. Users must still review arbitrary free-form text."""
import re

_PATTERNS = [
    re.compile(r'eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+'),
    # token_urlsafe tokens contain '-' and '_': redact the whole token, not a prefix.
    re.compile(r'odin_[A-Za-z0-9_-]+'),
    re.compile(r'(?i)([?&](?:token|access_token|api_key|apikey|key|ws_token)=)[^&\s"\'#]+'),
    re.compile(r"(?i)[\"']?\b(?:authorization|proxy-authorization)[\"']?\s*[:=]\s*[\"']?(?:bearer|basic)?\s*[^\s,;\"']+"),
    re.compile(r'''(?ix)["']?\b(?:password|passwd|access[_-]?code|api[_-]?key|token|secret|credential|cookie|serial(?:_number)?)\b["']?\s*[:=]\s*(?:"[^"\n]*"|'[^'\n]*'|[^\s,;}]+)'''),
    re.compile(r'(?i)\b(?:PASS|USER)\s+[^\s]+'),
    re.compile(r'(?i)(?:https?|rtsp|ftp)s?://[^\s/@]+:[^\s/@]+@'),
    re.compile(r'\b\d{1,3}(?:\.\d{1,3}){3}\b'),
    re.compile(r'(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b'),
]


def sanitize_log_line(line: str) -> str:
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
