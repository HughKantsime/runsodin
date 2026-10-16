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
    media) would otherwise fail closed with 503. Each test
    gets its own registry copy; tests needing an absent provider remove it.
    """
    from core.registry import registry
    monkeypatch.setattr(registry, "_providers", dict(registry._providers))
    from modules.archives.timelapse_media_provider import TimelapseMediaService
    from modules.jobs.queue_creation_provider import QueueCreationService
    # Only stateless request-path services; valuation/accounting providers stay
    # opt-in because their absence is a tested, distinct code path.
    for name, factory in (("QueueCreationProvider", QueueCreationService),
                          ("TimelapseMediaProvider", TimelapseMediaService)):
        if not registry.has_provider(name):
            registry.register_provider(name, factory())



@pytest.fixture(autouse=True)
def _join_dispatch_background_threads(monkeypatch):
    """Join smart-plug background threads started under configure_dispatch before
    monkeypatch restores module globals, so no thread outlives its test."""
    yield
    import sys
    support = sys.modules.get("tests.test_contracts.dispatch_test_support")
    if support is not None:
        support.join_background_threads()
