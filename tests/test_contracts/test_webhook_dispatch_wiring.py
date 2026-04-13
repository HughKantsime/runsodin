"""
Static contract test — every live webhook dispatch site must go through
core.webhook_utils.safe_post(), never raw httpx.post()/get()/etc.

Guards R8 verification gap from 2026-04-12 review:
    The first R8 fix added resolve_and_check_webhook_url() but only wired
    it into the test-webhook flow. Live dispatch in webhooks.py /
    channels.py / alert_dispatcher.py was still calling httpx.post(...)
    directly, so the SSRF gate was bypassed in production paths.

This test scans the notifications + organizations modules and fails if a
raw httpx outbound call sneaks back in. The intent is to make the next
contributor go through the SSRF wrapper instead of bypassing it.

Allowlisted call sites (the only places raw httpx is okay):
  - core/webhook_utils.py itself (it IS the wrapper)
  - modules/vision/detection_thread.py + modules/archives/timelapse_capture.py
    (hardcoded internal go2rtc / printer snapshot URLs — not user-supplied)
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND = REPO_ROOT / "backend"

# Paths under backend/ that are checked for raw httpx outbound calls.
SCANNED_DIRS = [
    BACKEND / "modules" / "notifications",
    BACKEND / "modules" / "organizations",
]

# Files that legitimately call httpx directly. Each entry is a path
# relative to the repo root.
ALLOWED_RAW_HTTPX = {
    # The wrapper itself.
    "backend/core/webhook_utils.py",
    # Internal-network probes — the URL is hardcoded to the local go2rtc /
    # printer snapshot endpoint, not a user-supplied webhook target.
    "backend/modules/vision/detection_thread.py",
    "backend/modules/archives/timelapse_capture.py",
}

# Patterns that count as a "raw" outbound httpx call.
RAW_HTTPX_PATTERNS = [
    re.compile(r"\bhttpx\.(get|post|put|patch|delete|request)\s*\("),
    re.compile(r"\b_httpx\.(get|post|put|patch|delete|request)\s*\("),
]


def _iter_python_files(roots: list[Path]):
    for root in roots:
        if not root.exists():
            continue
        yield from root.rglob("*.py")


def _violations() -> list[tuple[str, int, str]]:
    out: list[tuple[str, int, str]] = []
    for path in _iter_python_files(SCANNED_DIRS):
        rel = str(path.relative_to(REPO_ROOT))
        if rel in ALLOWED_RAW_HTTPX:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            for pat in RAW_HTTPX_PATTERNS:
                if pat.search(line):
                    out.append((rel, lineno, line.strip()))
                    break
    return out


def test_no_raw_httpx_in_notification_dispatch():
    """Notification + org webhook code must dispatch via safe_post."""
    violations = _violations()
    if violations:
        msg_lines = [
            "Raw httpx outbound call found in webhook dispatch code:",
            "",
        ]
        for rel, lineno, line in violations:
            msg_lines.append(f"  {rel}:{lineno}: {line}")
        msg_lines.extend([
            "",
            "Use core.webhook_utils.safe_post() instead. It performs the",
            "dispatch-time DNS resolution + private-IP rejection that R8",
            "(2026-04-12 adversarial review) requires. If this site is a",
            "legitimate exception (hardcoded internal URL, not user-",
            "supplied), add it to ALLOWED_RAW_HTTPX in this test with a",
            "comment explaining why.",
        ])
        pytest.fail("\n".join(msg_lines))


def test_safe_post_is_actually_called():
    """At least one site in dispatch code must import + call safe_post.

    Catches the case where someone deletes all dispatch and forgets to
    delete this test, leaving a vacuous green.
    """
    found = False
    for path in _iter_python_files(SCANNED_DIRS):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if "safe_post(" in text and "from core.webhook_utils" in text:
            found = True
            break
    assert found, (
        "No dispatch site imports + calls safe_post(). Either dispatch "
        "code was removed (in which case delete this test) or someone "
        "regressed back to raw httpx (in which case fix it)."
    )
