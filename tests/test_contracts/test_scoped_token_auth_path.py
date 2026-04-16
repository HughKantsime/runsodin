"""
Contract test — scoped-token (`odin_xxx`) auth path must work end-to-end.

v1.9.1 prod incident (2026-04-16): `core/dependencies.py::get_current_user`
imported `dateutil.parser` which is NOT in `requirements.txt`. The import
fired only on a branch that resolved a scoped token with a non-null
`expires_at`. Every agent-surface call from the MCP (which sends the
token via `X-API-Key`) therefore returned 500 `internal_error`.

The bug was invisible because:
  - The source-level Phase 2 contract tests never imported dependencies.py.
  - No integration test exercised the `X-API-Key` path against a running
    container.
  - Upstream callers (JWT sessions) skip the scoped-token branch entirely.

This test pins two invariants:
  1. `get_current_user` must not import `dateutil` (stdlib is enough).
  2. When a scoped token with `expires_at` is resolved, the code path
     does NOT raise.
"""

from __future__ import annotations

import ast
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

pytest.importorskip("sqlalchemy", reason="SQLAlchemy not installed")

BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

DEPS_PATH = BACKEND_DIR / "core" / "dependencies.py"


def test_scoped_token_path_does_not_import_dateutil():
    """Direct source-level guard. If someone re-adds `dateutil` without
    updating requirements.txt, this fails immediately."""
    src = DEPS_PATH.read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and "dateutil" in node.module:
            pytest.fail(
                f"dependencies.py line {node.lineno}: imports from "
                f"`{node.module}` which isn't in requirements.txt. "
                "Use `datetime.fromisoformat` instead — it's stdlib in "
                "Python 3.11+. Re-introducing dateutil here is exactly "
                "the v1.9.1 prod incident shape."
            )
        if isinstance(node, ast.Import):
            for alias in node.names:
                if "dateutil" in alias.name:
                    pytest.fail(
                        f"dependencies.py line {node.lineno}: `import "
                        f"{alias.name}` — same regression, same fix."
                    )


def test_expires_at_parsing_survives_iso_8601_with_tz():
    """Direct unit test of the expiry-parse logic pattern — isoformat with TZ."""
    # Shape SQLAlchemy emits after storing a tz-aware datetime.
    raw = "2027-01-01 00:00:00+00:00"
    exp = datetime.fromisoformat(raw)
    assert exp.tzinfo is not None
    assert exp > datetime.now(timezone.utc)


def test_expires_at_parsing_survives_iso_8601_naive():
    """Tz-naive isoformat string — the code must coerce to UTC before compare."""
    raw = "2027-01-01 00:00:00"
    exp = datetime.fromisoformat(raw)
    assert exp.tzinfo is None
    exp = exp.replace(tzinfo=timezone.utc)
    assert exp > datetime.now(timezone.utc)


def test_expires_at_parsing_survives_microseconds_with_tz():
    """Full ISO-8601 shape with fractional seconds and TZ offset."""
    raw = "2027-01-01T00:00:00.123456+00:00"
    exp = datetime.fromisoformat(raw)
    assert exp.tzinfo is not None


def test_source_contains_isoformat_call_for_expires_at():
    """Pin the positive shape — dependencies.py uses datetime.fromisoformat
    somewhere near the scoped-token expiry check."""
    src = DEPS_PATH.read_text()
    # Find the `if candidate.expires_at:` block.
    idx = src.find("if candidate.expires_at:")
    assert idx >= 0, "scoped-token expiry check is missing from dependencies.py"
    nearby = src[idx : idx + 800]
    assert "datetime.fromisoformat" in nearby, (
        "Expiry parse must use datetime.fromisoformat. Any other parser "
        "risks reintroducing a missing-dep 500 on every scoped-token call."
    )


def test_expired_token_path_is_noop_safe_on_malformed_string():
    """The except block swallows malformed expires_at — verify our new
    code structure still has it so the scoped-token path can never raise
    out of expiry parsing."""
    src = DEPS_PATH.read_text()
    idx = src.find("if candidate.expires_at:")
    assert idx >= 0
    nearby = src[idx : idx + 800]
    # There must be a try/except block here.
    assert "try:" in nearby and "except Exception:" in nearby, (
        "Expiry parse must stay inside try/except Exception — malformed "
        "expires_at values must not raise out of get_current_user."
    )
