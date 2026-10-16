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


_STATE_MACHINE = str(_BACKEND / "modules" / "jobs" / "state_machine.py")
# Runtime writers allowed besides the state machine (mirrors the static census allowlist).
_STATUS_WRITERS = {
    _STATE_MACHINE,
    # bootstrap repair of legacy enum casing (status=LOWER(status)), not a transition
    str(_BACKEND / "core" / "schema" / "migrations" / "001_legacy_columns.py"),
}
_LIBRARY_PARTS = (os.sep + "site-packages" + os.sep, os.sep + "sqlalchemy" + os.sep, os.sep + "lib" + os.sep + "python")


def _unauthorised_writer():
    """The backend code that issued the current write without an allowed writer on
    its stack (None when an allowed writer -- or test code itself -- issued it)."""
    frame = sys._getframe(2)
    backend = []
    while frame is not None:
        filename = frame.f_code.co_filename
        if filename != __file__ and not any(part in filename for part in _LIBRARY_PARTS):
            if not filename.startswith(str(_BACKEND)):
                break  # reached test code
            if filename in _STATUS_WRITERS:
                return None
            backend.append(f"{filename}:{frame.f_lineno}")
        frame = frame.f_back
    return backend[-1] if backend else None  # the outermost product frame names the caller


@pytest.fixture(autouse=True)
def _job_status_runtime_census(request):
    """Every contract test: product code writes jobs.status only through the state machine.

    SQLite connections opened during the test trace every statement (raw DBAPI
    monitor paths included), and Job.status assignments on persistent rows are
    observed; a write whose issuing frame is backend code outside
    modules/jobs/state_machine.py fails the test. Test code's own fixture writes
    are not product writes and are ignored.
    """
    from sqlalchemy import event, inspect as orm_inspect
    from sqlalchemy.engine import Engine
    from tests.test_contracts.job_census import sql_status_writes
    violations = []

    def trace(statement):
        lowered = statement.lower()
        if "jobs" in lowered and "status" in lowered and sql_status_writes(statement):
            origin = _unauthorised_writer()
            if origin is not None:
                violations.append((origin, " ".join(statement.split())[:160]))

    def on_connect(dbapi_connection, _record):
        if hasattr(dbapi_connection, "set_trace_callback"):
            dbapi_connection.set_trace_callback(trace)

    def on_status(target, value, oldvalue, initiator):
        if orm_inspect(target).persistent:
            origin = _unauthorised_writer()
            if origin is not None:
                violations.append((origin, f"Job.status = {value!r}"))

    from modules.jobs.models import Job
    event.listen(Engine, "connect", on_connect)
    event.listen(Job.status, "set", on_status)
    try:
        yield violations
    finally:
        event.remove(Engine, "connect", on_connect)
        event.remove(Job.status, "set", on_status)
    assert violations == [], f"jobs.status written outside the state machine: {violations}"
