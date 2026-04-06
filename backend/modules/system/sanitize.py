# modules/system/sanitize.py — Log line sanitization
#
# Strips JWTs, API keys, passwords, and IP addresses from log lines
# before they're included in diagnostics output.

import re

_PATTERNS = [
    # JWT tokens (three base64url segments separated by dots)
    re.compile(r'eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+'),
    # ODIN API keys
    re.compile(r'odin_[A-Za-z0-9]+'),
    # Generic API key assignments
    re.compile(r'[Aa]pi[_-]?[Kk]ey[=: ]+\S+'),
    # Passwords
    re.compile(r'[Pp]assword[=: ]+\S+'),
    # IPv4 addresses
    re.compile(r'\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b'),
]

_REDACTED = "[REDACTED]"


def sanitize_log_line(line: str) -> str:
    for pattern in _PATTERNS:
        line = pattern.sub(_REDACTED, line)
    return line


def sanitize_log_lines(lines: list[str]) -> list[str]:
    return [sanitize_log_line(line) for line in lines]
