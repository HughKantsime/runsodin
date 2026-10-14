"""Pending OIDC authority must not revive a revoked issued token."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_session_issuance_behavior import oidc_client, seed_code


def test_pending_oidc_code_cannot_exchange_a_blacklisted_token(oidc_client, review_db):
    from core import auth
    import jwt

    code = seed_code(review_db)
    token = review_db.execute(
        text('SELECT access_token FROM oidc_auth_codes WHERE code=:code'),
        {'code': code},
    ).scalar_one()
    payload = jwt.decode(token, auth.SECRET_KEY, algorithms=[auth.ALGORITHM])
    review_db.execute(
        text('INSERT INTO token_blacklist(jti,expires_at) VALUES(:jti,:expiry)'),
        {'jti': payload['jti'], 'expiry': (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()},
    )
    review_db.commit()

    response = oidc_client.post('/auth/oidc/exchange', json={'code': code})
    assert response.status_code == 401
    assert 'access_token' not in response.json()
    assert 'session' not in response.cookies
    with Session(review_db.get_bind()) as fresh:
        assert fresh.execute(text('SELECT COUNT(*) FROM active_sessions')).scalar_one() == 0


def test_unrelated_revocation_does_not_block_oidc_exchange(oidc_client, review_db):
    code = seed_code(review_db)
    review_db.execute(
        text('INSERT INTO token_blacklist(jti,expires_at) VALUES(:jti,:expiry)'),
        {'jti': 'owned-unrelated-revocation', 'expiry': (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()},
    )
    review_db.commit()
    assert oidc_client.post('/auth/oidc/exchange', json={'code': code}).status_code == 200


def test_oidc_revocation_storage_failure_does_not_release_authority(oidc_client, review_db, monkeypatch):
    code = seed_code(review_db)
    original = Session.execute

    def fail_lookup(self, statement, *args, **kwargs):
        if 'SELECT 1 FROM token_blacklist' in str(statement):
            raise RuntimeError('owned revocation lookup failure')
        return original(self, statement, *args, **kwargs)

    monkeypatch.setattr(Session, 'execute', fail_lookup)
    response = oidc_client.post('/auth/oidc/exchange', json={'code': code})
    assert response.status_code == 503
    assert 'access_token' not in response.json()
    assert 'session' not in response.cookies
    with Session(review_db.get_bind()) as fresh:
        assert fresh.execute(text('SELECT COUNT(*) FROM active_sessions')).scalar_one() == 0
