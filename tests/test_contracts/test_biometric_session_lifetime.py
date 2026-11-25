"""Owned biometric HTTP rotation and issued-session authority contract."""
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session
import pytest

from tests.test_contracts.test_education_review_workflow import review_db


@pytest.mark.parametrize('fault', ['none', 'zero_microseconds', 'staging', 'commit', 'expired', 'inactive', 'wrong_device', 'race'])
def test_biometric_refresh_rotates_once_and_records_issued_session(review_db, monkeypatch, fault):
    from core import auth
    from core.db import get_db
    from modules.push.models import BiometricToken
    from modules.push.routes import biometric

    monkeypatch.setattr(auth, 'SECRET_KEY', 'owned-biometric-session-test-key-only')
    engine = review_db.get_bind()
    engine.hide_parameters = True
    BiometricToken.__table__.create(engine, checkfirst=True)
    raw = secrets.token_hex(32)
    device = 'owned-synthetic-device'
    expiry = datetime.now(timezone.utc) + timedelta(days=-1 if fault == 'expired' else 1)
    if fault == 'zero_microseconds':
        expiry = expiry.replace(microsecond=0)
    review_db.add(BiometricToken(
        user_id=1, device_id=device, token_hash=biometric._hash_token(raw),
        expires_at=expiry,
    ))
    if fault == 'inactive':
        review_db.execute(text('UPDATE users SET is_active=0 WHERE id=1'))
    review_db.commit()
    if fault == 'staging':
        import core.session_tracking as tracking
        original_stage = tracking.stage_session
        def refuse_after_staging(*args, **kwargs):
            assert original_stage(*args, **kwargs)
            return False
        monkeypatch.setattr(tracking, 'stage_session', refuse_after_staging)
    elif fault == 'commit':
        def fail_commit(self):
            raise RuntimeError('owned outer commit failure')
        monkeypatch.setattr(Session, 'commit', fail_commit)
    app = FastAPI()
    app.include_router(biometric.router)

    def database():
        with Session(engine) as db:
            yield db

    app.dependency_overrides[get_db] = database
    with TestClient(app) as client:
        if fault == 'race':
            from concurrent.futures import ThreadPoolExecutor
            from threading import Barrier, local
            barrier = Barrier(2)
            original_execute = Session.execute
            worker = local()
            def synchronized_parent(self, statement, *args, **kwargs):
                if str(statement).startswith('UPDATE users SET id=id') and not getattr(worker, 'arrived', False):
                    worker.arrived = True
                    barrier.wait(timeout=10)
                return original_execute(self, statement, *args, **kwargs)
            monkeypatch.setattr(Session, 'execute', synchronized_parent)
            def refresh_independently(_):
                with TestClient(app) as independent:
                    return independent.post('/auth/biometric-refresh', json={'refresh_token':raw,'device_id':device})
            with ThreadPoolExecutor(max_workers=2) as pool:
                responses = list(pool.map(refresh_independently, range(2)))
            monkeypatch.setattr(Session, 'execute', original_execute)
            statuses = sorted(r.status_code for r in responses)
            assert statuses in ([200, 200], [200, 503])
            completed = [client.post('/auth/biometric-refresh', json={'refresh_token': raw, 'device_id': device})
                         if response.status_code == 503 else response for response in responses]
            assert all(response.status_code == 200 for response in completed)
            assert completed[0].json()['refresh_token'] == completed[1].json()['refresh_token']
            with Session(engine) as fresh:
                assert fresh.execute(text('SELECT COUNT(*) FROM active_sessions WHERE user_id=1')).scalar_one() == 2
                assert fresh.query(BiometricToken).count() == 1
            return
        if fault in ('expired', 'inactive', 'wrong_device'):
            response = client.post('/auth/biometric-refresh', json={'refresh_token':raw,'device_id': 'wrong-owned-device' if fault == 'wrong_device' else device})
            assert response.status_code == 401
            assert 'access_token' not in response.json()
            with Session(engine) as fresh:
                assert fresh.execute(text('SELECT COUNT(*) FROM active_sessions WHERE user_id=1')).scalar_one() == 0
                assert fresh.query(BiometricToken).one().token_hash == biometric._hash_token(raw)
            return
        response = client.post('/auth/biometric-refresh', json={'refresh_token': raw, 'device_id': device})
        if fault in ('staging', 'commit'):
            assert response.status_code == 503
            assert 'access_token' not in response.json()
            with Session(engine) as fresh:
                assert fresh.execute(text('SELECT COUNT(*) FROM active_sessions WHERE user_id=1')).scalar_one() == 0
                row = fresh.query(BiometricToken).one()
                assert row.token_hash == biometric._hash_token(raw)
                assert row.is_revoked is False
            return
        assert response.status_code == 200
        assert 'access_token' in response.json()
        replacement = response.json()['refresh_token']
        assert replacement and replacement != raw
        assert response.json()['expires_at']
        retry = client.post('/auth/biometric-refresh', json={'refresh_token': raw, 'device_id': device})
        assert retry.status_code == 200 and retry.json()['refresh_token'] == replacement
        second = client.post('/auth/biometric-refresh', json={'refresh_token': replacement, 'device_id': device})
        assert second.status_code == 200
        assert second.json()['refresh_token'] != replacement
    with Session(engine) as fresh:
        assert fresh.execute(text('SELECT COUNT(*) FROM active_sessions WHERE user_id=1')).scalar_one() == 3
