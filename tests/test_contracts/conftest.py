"""Shared conftest for contract tests.

Contract tests run without a live backend, but importing backend modules
(e.g. `from core.dependencies import ...`) triggers module-level code that
requires JWT_SECRET_KEY. Provide a safe test value so contract tests
don't depend on shell env.
"""

import os
import sys
from pathlib import Path

# Ensure backend/ is on sys.path for `from core.x import y` style imports.
_BACKEND = Path(__file__).resolve().parents[2] / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

# Provide a test JWT secret if one isn't set. Contract tests never mint
# real tokens with this — it's only consumed at import time. Using the
# same value across tests keeps cached module state consistent.
os.environ.setdefault("JWT_SECRET_KEY", "test-key-for-contract-tests-only")
# TestClient speaks plain HTTP; Secure cookies would never be sent back.
os.environ.setdefault("COOKIE_SECURE", "false")


import pytest

@pytest.fixture(scope="session", autouse=True)
def _clear_rate_limits():
    """Contract tests own their state and must never discover/restart a live server.

    Override the live integration suite's fixture only within this subtree.
    Individual owned HTTP fixtures reset their own limiter when necessary.
    """
    yield


@pytest.fixture(autouse=True)
def _startup_providers(monkeypatch):
    """Mirror app startup provider registration (no routes, no database).

    Contract tests mount bare routers without running module registration, so
    routes that require startup-registered services (queue staging, timelapse
    media, attempt valuation) would otherwise fail closed with 503. Each test
    gets its own registry copy; tests needing an absent provider remove it.
    """
    from core.registry import registry
    monkeypatch.setattr(registry, "_providers", dict(registry._providers))
    from modules.archives import register_providers as archives_providers
    from modules.jobs import register_providers as jobs_providers
    for register in (jobs_providers, archives_providers):
        register(registry)
