"""Test setup for telemetry contract tests.

Ensures `backend/` is on sys.path and required env vars are set so the
flag-routing tests can import route modules. Other telemetry tests
don't depend on this, but they don't conflict with it either.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# backend/ on sys.path — lets `from modules.printers...` imports work
# the same way ODIN's app startup does.
_BACKEND = Path(__file__).resolve().parent.parent.parent / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

# The auth module blows up without these at import time; they're not
# actually used in these tests — just need to be present.
os.environ.setdefault("JWT_SECRET_KEY", "test-telemetry")
os.environ.setdefault("ADMIN_USERNAME", "ci")
os.environ.setdefault("ADMIN_PASSWORD", "ci")
