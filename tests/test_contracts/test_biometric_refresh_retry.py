"""Lost mobile sign-in responses, transaction boundaries and policy on both databases."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import logging
import secrets
from threading import Barrier, local

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from core import auth, license_lock, session_tracking
from core.models import AuditLog
from modules.push.models import BiometricToken
from modules.push.routes import biometric
from tests.test_contracts.test_education_classes_migration import native_engine, pg_engine
from tests.test_contracts.test_mfa_code_lockout import account_app, both_databases
from tests.test_contracts.test_password_reset_behavior import new_password, seed_link

pytestmark = both_databases
DEVICE = "test-001-phone"
UNKNOWN = "Invalid or revoked biometric token"
EXPIRED = "This device's saved sign-in has expired. Sign in with your password."
USED = "auth.biometric_retry_used"
CAP = "auth.biometric_retry_cap_refused"
LATE = "auth.biometric_late_retry_refused"
REPLAY = "auth.biometric_replay_refused"


@pytest.fixture
def client(account_app, native_engine):
    native_engine.hide_parameters = True
    account_app.include_router(biometric.router)
    with TestClient(account_app) as caller:
        yield caller


def seed(engine, user=1, device=DEVICE):
    # This is the stored form of a token issued before deterministic rotation.
    raw = secrets.token_hex(32)
    with Session(engine) as db:
        db.add(BiometricToken(user_id=user, device_id=device, token_hash=biometric._hash_token(raw),
                              expires_at=datetime.now(timezone.utc) + timedelta(days=1)))
        db.commit()
    return raw


def refresh(client, raw, device=DEVICE):
    return client.post("/auth/biometric-refresh", json={"device_id": device, "refresh_token": raw})


def renew(client, raw, device=DEVICE):
    response = refresh(client, raw, device)
    assert response.status_code == 200
    return response.json()


def refused(response, detail=UNKNOWN):
    assert response.status_code == 401
    assert response.json() == {"detail": detail}


def snapshot(engine):
    with engine.connect() as db:
        return db.execute(text("SELECT * FROM biometric_tokens ORDER BY id")).mappings().all()


def session_count(engine):
    with engine.connect() as db:
        return db.execute(text("SELECT count(*) FROM active_sessions")).scalar_one()


def audits(engine):
    with Session(engine) as db:
        return db.query(AuditLog).filter(AuditLog.action.like("auth.biometric%")).order_by(AuditLog.id).all()


def test_lost_answer_and_legacy_row(client, native_engine, caplog):
    caplog.set_level(logging.INFO)
    raw = seed(native_engine)
    original, = snapshot(native_engine)
    assert original["last_used_at"] is None
    first = renew(client, raw)
    rotated = snapshot(native_engine)
    assert rotated[0]["last_used_at"] is not None
    # Independent RFC 5869 extract and single-block expand pin the wire derivation.
    label = b"odin-biometric-rotate-v1"
    prk = hmac.digest(bytes(32), auth.SECRET_KEY.encode(), "sha256")
    key = hmac.digest(prk, label + bytes([1]), "sha256")
    expected = hmac.new(key, label + original["id"].to_bytes(8, "big") + raw.encode(), hashlib.sha256).hexdigest()
    assert first["refresh_token"] == expected
    assert not audits(native_engine)
    access = {first["access_token"]}
    for count in range(1, 4):
        retry = renew(client, raw)
        assert retry["refresh_token"] == first["refresh_token"]
        assert retry["expires_at"] == first["expires_at"]
        access.add(retry["access_token"])
        assert len(access) == session_count(native_engine) == count + 1
        assert snapshot(native_engine) == rotated
    second = renew(client, expected)
    refused(refresh(client, raw), EXPIRED)
    rows = audits(native_engine)
    assert [row.action for row in rows] == [USED] * 3 + [REPLAY]
    for row in rows:
        assert row.entity_type == "user" and row.entity_id == 1
        assert row.details["device_id"] == DEVICE[:8]
        assert type(row.details["seconds_since_rotation"]) is int
    captured = caplog.text + str([row.details for row in rows])
    sensitive = {raw, expected, second["refresh_token"], second["access_token"], *access}
    assert all(value not in captured and biometric._hash_token(value) not in captured for value in sensitive)


@pytest.mark.parametrize("rotation_age", [601, -30], ids=["late", "clock-behind"])
def test_late_retry_and_old_generations(client, native_engine, rotation_age):
    raw = seed(native_engine)
    successor = renew(client, raw)["refresh_token"]
    with Session(native_engine) as db:
        db.query(BiometricToken).one().last_used_at = datetime.now(timezone.utc) - timedelta(seconds=rotation_age)
        db.commit()
    before = snapshot(native_engine)
    for _ in range(3):
        refused(refresh(client, raw), EXPIRED)
    assert snapshot(native_engine) == before and session_count(native_engine) == 1
    row, = audits(native_engine)
    assert row.action == LATE and row.details["seconds_since_rotation"] >= max(0, rotation_age)
    current = renew(client, successor)["refresh_token"]
    before = snapshot(native_engine)
    for _ in range(3):
        refused(refresh(client, raw), EXPIRED)
    assert snapshot(native_engine) == before and session_count(native_engine) == 2
    renew(client, current)
    before = snapshot(native_engine)
    refused(refresh(client, raw))
    assert snapshot(native_engine) == before and session_count(native_engine) == 3
    assert [row.action for row in audits(native_engine)] == [LATE, REPLAY]


def test_cap_is_per_rotation_and_device(client, native_engine, monkeypatch):
    device2 = DEVICE[:8] + "-tablet"
    a = seed(native_engine)
    b = seed(native_engine, device=device2)
    successor = renew(client, a)["refresh_token"]
    renew(client, b, device2)
    for _ in range(5):
        renew(client, a)
    before = snapshot(native_engine)
    for _ in range(2):
        refused(refresh(client, a), EXPIRED)
    assert session_count(native_engine) == 7 and snapshot(native_engine) == before
    rows = audits(native_engine)
    assert [r.action for r in rows] == [USED] * 5 + [CAP]
    assert rows[-1].details["device_row_id"] == before[0]["id"]
    assert rows[-1].details["device_id"] == DEVICE[:8]
    assert 0 <= rows[-1].details["seconds_since_rotation"] <= 600

    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(minutes=11)

    with monkeypatch.context() as patch:
        patch.setattr(biometric, "datetime", Later)
        for _ in range(2):
            refused(refresh(client, a), EXPIRED)
    assert [r.action for r in audits(native_engine)] == [USED] * 5 + [CAP, LATE]
    assert session_count(native_engine) == 7 and snapshot(native_engine) == before
    renew(client, b, device2)
    renew(client, successor)
    renew(client, successor)


def test_invalid_format_skips_hmac(client, native_engine, monkeypatch):
    raw = seed(native_engine)
    renew(client, raw)
    before = snapshot(native_engine)
    monkeypatch.setattr(biometric, "_rotation_key", lambda: pytest.fail("derived key for malformed token"))
    for malformed in ("", raw[:-1], raw + "0", "A" * 64, "x" * 64, "é" * 64, raw + "\n"):
        refused(refresh(client, malformed))
    assert not audits(native_engine) and session_count(native_engine) == 1
    assert snapshot(native_engine) == before


def test_device_and_owner_binding(client, native_engine):
    with native_engine.begin() as db:
        db.execute(text("INSERT INTO users(id,username,password_hash,role,is_active) "
                        "VALUES(2,'other','fixture','viewer',TRUE)"))
    a = seed(native_engine, user=2)
    b = seed(native_engine)
    renew(client, a)
    before = snapshot(native_engine)
    refused(refresh(client, a, "different-device"))
    assert not audits(native_engine) and snapshot(native_engine) == before
    renew(client, a)
    with native_engine.connect() as db:
        assert db.execute(text("SELECT user_id FROM active_sessions")).scalars().all() == [2, 2]
    renew(client, b)
    row, = audits(native_engine)
    assert row.entity_id == 2


def test_signing_key_change_without_restart_closes_only_old_retry(client, native_engine, monkeypatch):
    a = seed(native_engine)
    b = renew(client, a)["refresh_token"]
    before = snapshot(native_engine)
    monkeypatch.setattr(auth, "SECRET_KEY", secrets.token_hex(32))
    refused(refresh(client, a))
    assert snapshot(native_engine) == before and not audits(native_engine)
    assert session_count(native_engine) == 1
    renew(client, b)


@pytest.mark.parametrize("fault", ["inactive", "expired", "license", "license_lock", "staging", "commit"])
def test_retry_issuance_failure_is_atomic(client, native_engine, monkeypatch, fault):
    a = seed(native_engine)
    b = renew(client, a)["refresh_token"]
    if fault == "inactive":
        with native_engine.begin() as db:
            db.execute(text("UPDATE users SET is_active=FALSE WHERE id=1"))
    elif fault == "expired":
        with Session(native_engine) as db:
            db.query(BiometricToken).one().expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            db.commit()
    elif fault == "license":
        def deny(db, user_id, *, issuance=False):
            assert issuance and user_id == 1
            raise HTTPException(403, "Sign-in is unavailable for this account.")
        monkeypatch.setattr(license_lock, "authorize_user", deny)
    elif fault == "license_lock":
        def lock_unavailable(db):
            raise RuntimeError("simulated lock failure")
        monkeypatch.setattr(license_lock, "enabled", lambda: True)
        monkeypatch.setattr(license_lock, "_lock", lock_unavailable)
    elif fault == "staging":
        monkeypatch.setattr(session_tracking, "stage_session", lambda *args: False)
    else:
        def fail_commit(db):
            db.flush()
            raise RuntimeError("simulated commit failure")
        monkeypatch.setattr(Session, "commit", fail_commit)
    before = snapshot(native_engine)
    retry = refresh(client, a)
    current = refresh(client, b)
    expected = {"inactive": 401, "expired": 401, "license": 403, "license_lock": 503, "staging": 503, "commit": 503}
    assert retry.status_code == current.status_code == expected[fault]
    assert retry.json() == current.json()
    assert snapshot(native_engine) == before and not audits(native_engine)
    assert session_count(native_engine) == 1


@pytest.mark.parametrize("change", ["sign-out", "re-enrol", "rotate"])
def test_reclassifies_after_discovery(account_app, client, native_engine, monkeypatch, change):
    a = seed(native_engine)
    first = renew(client, a)
    headers = {"Authorization": "Bearer " + first["access_token"]}
    lock = biometric._lock_user

    def change_before_lock(db, uid):
        assert not db.in_transaction()
        monkeypatch.setattr(biometric, "_lock_user", lock)
        with TestClient(account_app) as independent:
            if change == "rotate":
                renew(independent, first["refresh_token"])
            else:
                method = "DELETE" if change == "sign-out" else "POST"
                response = independent.request(method, "/auth/biometric-token", params={"device_id": DEVICE}, headers=headers)
                assert response.status_code == (204 if change == "sign-out" else 200)
        return lock(db, uid)

    monkeypatch.setattr(biometric, "_lock_user", change_before_lock)
    refused(refresh(client, a), EXPIRED if change == "rotate" else UNKNOWN)
    if change == "rotate":
        assert session_count(native_engine) == 2
        assert [row.action for row in audits(native_engine)] == [REPLAY]
    else:
        refused(refresh(client, first["refresh_token"]))
        assert session_count(native_engine) == 1 and not audits(native_engine)


@pytest.mark.parametrize("operation", ["change", "reset"])
def test_password_operations_delete_all_device_rows(client, native_engine, operation):
    password = new_password()
    with native_engine.begin() as db:
        db.execute(text("UPDATE users SET password_hash=:h WHERE id=1"), {"h": auth.hash_password(password)})
    old = seed(native_engine)
    other = seed(native_engine, device="second")
    first = renew(client, old)
    if operation == "change":
        response = client.post("/auth/me/password", headers={"Authorization": "Bearer " + first["access_token"]},
                               json={"current_password": password, "new_password": new_password()})
    else:
        with Session(native_engine) as db:
            link = seed_link(db)
        assert len(snapshot(native_engine)) == 2
        response = client.post("/auth/reset-password", json={"token": link, "new_password": new_password()})
    assert response.status_code == 200
    assert not snapshot(native_engine)
    refused(refresh(client, old))
    refused(refresh(client, first["refresh_token"]))
    refused(refresh(client, other, "second"))
    assert session_count(native_engine) == (1 if operation == "change" else 0)


def test_simultaneous_current_uses_converge(account_app, client, native_engine, monkeypatch):
    a = seed(native_engine)
    barrier = Barrier(2)
    arrived = local()
    lock = biometric._lock_user

    def together(db, user_id):
        if not getattr(arrived, "once", False):
            arrived.once = True
            barrier.wait(timeout=10)
        return lock(db, user_id)

    def run(_):
        with TestClient(account_app) as independent:
            return refresh(independent, a)

    with monkeypatch.context() as patch:
        patch.setattr(biometric, "_lock_user", together)
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(run, range(2)))
    assert sorted(r.status_code for r in responses) in ([200, 200], [200, 503])
    answers = [renew(client, a) if r.status_code == 503 else r.json() for r in responses]
    assert answers[0]["refresh_token"] == answers[1]["refresh_token"]
    assert answers[0]["expires_at"] == answers[1]["expires_at"]
    assert session_count(native_engine) == 2 and [r.action for r in audits(native_engine)] == [USED]
