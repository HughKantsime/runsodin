"""
Contract test — CORS allow-list includes agent-surface headers (v1.8.9).

Codex pass 10 (2026-04-15) flagged that the new Idempotency-Key and
X-Dry-Run headers were not in the CORS allow_headers list. A
browser cross-origin client would fail preflight before ever sending
the header — silently disabling the new safety primitives exactly in
the deployments where CORS is in play.

This test asserts the app factory passes the correct allow_headers to
CORSMiddleware without needing to spin up a live server.
"""

import sys
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="FastAPI not installed in test venv")

BACKEND_DIR = Path(__file__).resolve().parents[2] / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


def test_cors_allow_headers_includes_agent_surface_headers():
    """The CORS middleware registration passes the new headers."""
    # Read the app factory source and assert the allow_headers list
    # contains every header the agent surface relies on. A live-app
    # test would require booting FastAPI + DB which the contract
    # suite avoids; a source-level assertion is sufficient because the
    # list is a static literal at registration time.
    app_py = (BACKEND_DIR / "core" / "app.py").read_text(encoding="utf-8")

    required = [
        '"Authorization"',
        '"Content-Type"',
        '"X-API-Key"',
        '"Accept"',
        '"Idempotency-Key"',
        '"X-Dry-Run"',
    ]
    for header in required:
        assert header in app_py, (
            f"CORS allow_headers must include {header} — cross-origin "
            f"clients would otherwise fail preflight"
        )


def test_cors_expose_headers_include_idempotent_replay():
    """JS on cross-origin pages must be able to read X-Idempotent-Replay
    so SDKs can surface 'this was replayed' to callers."""
    app_py = (BACKEND_DIR / "core" / "app.py").read_text(encoding="utf-8")
    assert '"X-Idempotent-Replay"' in app_py, (
        "CORS expose_headers must include X-Idempotent-Replay"
    )
