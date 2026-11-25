"""Signing-key maintenance on an upgraded database, including competing API workers."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import faulthandler
import hmac
import logging
import secrets
from threading import Barrier

import pytest
from sqlalchemy import event, text
from sqlalchemy.orm import Session

from core import auth
from core.app_maintenance import _check_signing_secret, _revoke_legacy_biometric_tokens
from core.models import AuditLog, SystemConfig
from tests.test_contracts.test_biometric_refresh_retry import client, refresh, refused, seed, snapshot
from tests.test_contracts.test_education_classes_migration import native_engine, pg_engine
from tests.test_contracts.test_mfa_code_lockout import account_app, both_databases

pytestmark = both_databases
MARKER = "signing_secret_fingerprint"
ACTION = "auth.signing_secret_changed"


def rotate(monkeypatch):
    key = secrets.token_hex(32)
    monkeypatch.setattr(auth, "SECRET_KEY", key)
    return hmac.digest(key.encode(), b"odin-signing-secret-fingerprint-v1", "sha256").hex()


def stored(engine):
    with Session(engine) as db:
        return db.get(SystemConfig, MARKER).value


def changes(engine):
    with Session(engine) as db:
        return [r.details for r in db.query(AuditLog).filter_by(action=ACTION).all()]


def test_restart_revokes_once_and_keeps_fingerprints_private(client, native_engine, monkeypatch, caplog):
    raw = seed(native_engine)
    seed(native_engine, device="another-phone")
    before = snapshot(native_engine)
    old = rotate(monkeypatch)
    # The normal test client hides SQL parameters. Exercise a debug installation too.
    native_engine.hide_parameters = False

    def restart():
        with caplog.at_level(logging.DEBUG, logger="sqlalchemy.engine"):
            _check_signing_secret(native_engine)

    restart()
    assert stored(native_engine) == old and snapshot(native_engine) == before
    restart()
    assert snapshot(native_engine) == before and not changes(native_engine)
    current = rotate(monkeypatch)
    restart()
    assert stored(native_engine) == current and not snapshot(native_engine)
    refused(refresh(client, raw))
    seed(native_engine, device="new-phone")
    after = snapshot(native_engine)
    restart()
    assert snapshot(native_engine) == after
    assert changes(native_engine) == [{"saved_sign_ins_removed": 2}]
    with native_engine.connect() as db:
        audit = str(db.execute(text("SELECT * FROM audit_logs")).all())
    assert all(value not in caplog.text and value not in audit for value in (old, current))


def test_concurrent_restarts_record_one_change(account_app, native_engine, monkeypatch):
    _check_signing_secret(native_engine)
    seed(native_engine)
    current = rotate(monkeypatch)
    workers = Barrier(2)

    def ready(conn, cursor, statement, params, context, many):
        if statement.startswith("INSERT INTO system_config"):
            workers.wait(timeout=10)

    event.listen(native_engine, "before_cursor_execute", ready)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(_check_signing_secret, [native_engine, native_engine]))
    finally:
        event.remove(native_engine, "before_cursor_execute", ready)
    assert stored(native_engine) == current and not snapshot(native_engine)
    assert changes(native_engine) == [{"saved_sign_ins_removed": 1}]


def test_failed_audit_rolls_back_and_lifespan_continues(account_app, native_engine, monkeypatch, caplog):
    from core import app as application, db, job_timeline
    from core.registry import registry

    _revoke_legacy_biometric_tokens(native_engine)
    old = rotate(monkeypatch)
    _check_signing_secret(native_engine)
    seed(native_engine)
    current = rotate(monkeypatch)

    def fail_insert(conn, cursor, statement, params, context, many):
        if statement.startswith("INSERT INTO audit_logs"):
            raise RuntimeError(current)

    class ReachedNextStartupStep(Exception):
        pass

    def reached():
        raise ReachedNextStartupStep

    monkeypatch.setattr(db, "engine", native_engine)
    monkeypatch.setattr(application, "_discover_modules", lambda: [])
    monkeypatch.setattr(job_timeline, "start", lambda *args: None)
    monkeypatch.setattr(faulthandler, "register", lambda *args, **kwargs: None)
    monkeypatch.setattr(registry, "validate_dependencies", reached)
    app = application.create_app()

    async def start():
        async with app.router.lifespan_context(app):
            pass

    event.listen(native_engine, "before_cursor_execute", fail_insert)
    try:
        with pytest.raises(ReachedNextStartupStep):
            asyncio.run(start())
    finally:
        event.remove(native_engine, "before_cursor_execute", fail_insert)
    assert caplog.messages.count("Could not check saved phone sign-ins. ODIN will try again at the next start.") == 1
    assert current not in caplog.text and old not in caplog.text
    assert stored(native_engine) == old and len(snapshot(native_engine)) == 1 and not changes(native_engine)
    _check_signing_secret(native_engine)
    assert stored(native_engine) == current and not snapshot(native_engine)
    assert changes(native_engine) == [{"saved_sign_ins_removed": 1}]
