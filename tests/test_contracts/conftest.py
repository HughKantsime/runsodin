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


def pytest_collection_modifyitems(config, items):
    """Qualified-source checks marked preserved029_bundle run only against the bundle."""
    from tests.test_contracts._preserved029_target import running_against_bundle
    if running_against_bundle():
        return
    deselected = [item for item in items if item.get_closest_marker("preserved029_bundle")]
    if deselected:
        config.hook.pytest_deselected(items=deselected)
        items[:] = [item for item in items if not item.get_closest_marker("preserved029_bundle")]


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


@pytest.fixture
def tier_license(tmp_path, monkeypatch):
    """Install a REAL signed license through the production verifier.

    A throwaway Ed25519 issuer key is generated per test and substituted for
    the embedded vendor public key (the only seam: the vendor private key is
    not available); the license file is written to a per-test LICENSE_DIR and
    read back by license_manager exactly as in production. Returns
    ``install(tier, **payload_overrides) -> LicenseInfo``; ``install(None)``
    removes the file (Community).
    """
    import base64
    import json as _json
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    import license_manager as lm

    issuer = Ed25519PrivateKey.generate()
    monkeypatch.setattr(lm, "ODIN_PUBLIC_KEY", issuer.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode())
    license_dir = tmp_path / "signed-license"
    license_dir.mkdir()
    monkeypatch.setattr(lm, "LICENSE_DIR", str(license_dir))
    for name in ("ODIN_LICENSE_FILE", "ODIN_REQUIRE_LICENSE_BINDING", "ODIN_LICENSE_READ_ONLY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(lm, "_cached_license", None)
    monkeypatch.setattr(lm, "_cached_mtime", 0)

    def install(tier="pro", **overrides):
        path = license_dir / lm.LICENSE_FILENAME
        if tier is None:
            path.unlink(missing_ok=True)
        else:
            payload = {"tier": tier, "licensee": f"Contract test {tier}", "expires_at": "2099-01-01", **overrides}
            raw = _json.dumps(payload).encode()
            lm.save_license_file(base64.urlsafe_b64encode(raw).decode() + "." +
                                 base64.urlsafe_b64encode(issuer.sign(raw)).decode())
        lm._cached_license = None
        lm._cached_mtime = 0
        info = lm.get_license()
        assert tier is None or (info.valid and info.tier == tier), info.error
        return info

    return install


@pytest.fixture
def pro_license(tier_license):
    """A signed Pro license (orders, products, analytics, maintenance, branding)."""
    return tier_license("pro")


@pytest.fixture
def enterprise_license(tier_license):
    """A signed Enterprise license (adds audit export and MQTT event republish)."""
    return tier_license("enterprise")


@pytest.fixture
def education_license(tier_license):
    """A signed Education license (multi-user, job approval, groups, quotas)."""
    return tier_license("education")

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
    from sqlalchemy.pool import Pool
    from tests.test_contracts.job_census import sql_status_writes
    violations, active = [], [True]

    def trace(statement):
        if not active[0]:
            return  # a later test's connection reuse: this test's census is over
        lowered = statement.lower()
        if "jobs" in lowered and "status" in lowered and sql_status_writes(statement):
            origin = _unauthorised_writer()
            if origin is not None:
                violations.append((origin, " ".join(statement.split())[:160]))

    def attach(dbapi_connection, *_):
        # Every DBAPI connection a test uses: new ones (connect) and pooled ones
        # opened before the test, e.g. by module or session engines (checkout).
        if hasattr(dbapi_connection, "set_trace_callback"):
            dbapi_connection.set_trace_callback(trace)

    def on_status(target, value, oldvalue, initiator):
        if orm_inspect(target).persistent:
            origin = _unauthorised_writer()
            if origin is not None:
                violations.append((origin, f"Job.status = {value!r}"))

    from modules.jobs.models import Job
    event.listen(Engine, "connect", attach)
    event.listen(Pool, "checkout", attach)
    event.listen(Job.status, "set", on_status)
    try:
        yield violations
    finally:
        event.remove(Engine, "connect", attach)
        event.remove(Pool, "checkout", attach)
        event.remove(Job.status, "set", on_status)
        # Disable, never detach: set_trace_callback(None) from this thread on a connection a
        # background thread is executing on (e.g. the Spoolman outbox delivery an archive event
        # starts) deadlocks: that thread holds SQLite's connection mutex and waits for the GIL
        # to run the callback, while this thread holds the GIL inside sqlite3_trace_v2.
        active[0] = False
    assert violations == [], f"jobs.status written outside the state machine: {violations}"
