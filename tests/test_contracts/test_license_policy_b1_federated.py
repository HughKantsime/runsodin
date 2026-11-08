"""A2/A3 provider transports replaced; real identity, policy and issuance routes."""
from urllib.parse import parse_qs, urlsplit
import jwt
import pytest
from sqlalchemy import text
from tests.test_contracts.test_roster_auth import client, google_double, callback_url
from tests.test_contracts.test_roster_sync import setup, review_db, imported, canvas_config, platform_config, claims
from core import license_lock as policy


@pytest.mark.parametrize('provider', ['google', 'canvas'])
@pytest.mark.parametrize('boundary', ['callback', 'exchange'])
def test_roster_provider_seat_change(setup, client, tier_license, monkeypatch, canvas_config, platform_config, provider, boundary):
    from core.auth import hash_password
    from modules.organizations import education_roster_store as store, education_roster_sync as sync
    from modules.organizations import education_roster_providers as providers
    from modules.organizations import routes_education_roster as routes
    db, principal, _ = setup
    if provider == 'google':
        connection, _ = imported(setup)
        google_double(monkeypatch)
    else:
        saved = store.save(db, principal, routes.Configuration(provider='canvas', enabled=True, config=canvas_config))
        connection = store.load(db, saved['id'])
        roster = {'id': 'course', 'title': 'Canvas class', 'term': '', 'members': [
            {'subject': 'student', 'email': 'canvas-student@example.test', 'role': 'student'}]}
        plan = sync.preview(db, principal, connection, roster)
        sync.apply(db, principal, connection['id'], plan['preview_token'])
    db.execute(text('UPDATE users SET group_id=NULL,password_hash=:h WHERE id=5'), {'h': hash_password('SyntheticPassword123!')}); db.commit()
    tier_license('education', max_users=None)
    monkeypatch.setenv('ODIN_LICENSE_FLEET_LOCK_ENABLED', 'true')
    policy.reconcile(db); db.commit()
    base = f"/api/auth/classroom/{connection['id']}"
    if boundary == 'callback': tier_license('education', max_users=1)
    if provider == 'google':
        response = client.get(callback_url(client, connection), follow_redirects=False)
    else:
        target = 'https://odin.example.test' + base + '/launch'
        response = client.post(base + '/login', data={'iss': canvas_config['issuer'], 'login_hint': 'hint', 'target_link_uri': target}, follow_redirects=False)
        params = parse_qs(urlsplit(response.headers['location']).query)
        payload = claims(canvas_config)
        payload['nonce'] = params['nonce'][0]; payload[providers.LTI + 'target_link_uri'] = target
        encoded = jwt.encode(payload, platform_config['private_key'], algorithm='RS256', headers={'kid': platform_config['key_id']})
        async def owned_keys(*args, **kwargs): return {'keys': [providers.public_jwk(platform_config)]}, None
        monkeypatch.setattr(providers, 'request_json', owned_keys)
        response = client.post(base + '/launch', data={'state': params['state'][0], 'id_token': encoded}, follow_redirects=False)
    if boundary == 'exchange':
        assert response.status_code == 302, response.text
        code = parse_qs(urlsplit(response.headers['location']).fragment)['roster_code'][0]
        tier_license('education', max_users=1)
        response = client.post(base + '/exchange', json={'code': code})
        assert client.post(base + '/exchange', json={'code': code}).status_code == 409
    assert response.status_code == 403, response.text
    assert response.json()['detail']['code'] == 'license_user_locked'
    assert 'set-cookie' not in response.headers
    assert db.execute(text('SELECT COUNT(*) FROM active_sessions')).scalar() == 0


from tests.test_contracts.test_education_oidc_identity import oidc_db, _seed_oidc_config, _CallbackHandler


def test_oidc_callback_existing_unselected_identity(oidc_db, tier_license, monkeypatch):
    from core.db import get_db
    from core.auth import hash_password
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from modules.organizations import routes_oidc, oidc_handler
    _seed_oidc_config(oidc_db)
    oidc_db.execute(text("INSERT INTO users(id,username,email,password_hash,role,is_active,group_id,oidc_issuer,oidc_subject) VALUES(1,'local','local@test',:h,'admin',TRUE,NULL,NULL,NULL),(2,'student','student@example.test','sso-only','viewer',TRUE,1,'https://issuer.example','subject-1')"), {'h': hash_password('SyntheticPassword123!')})
    oidc_db.commit()
    tier_license('enterprise', max_users=1)
    monkeypatch.setenv('ODIN_LICENSE_FLEET_LOCK_ENABLED', 'true')
    monkeypatch.setattr(oidc_handler, 'create_handler_from_config', lambda *args: _CallbackHandler())
    app = FastAPI(); app.include_router(routes_oidc.router)
    app.dependency_overrides[get_db] = lambda: oidc_db
    client = TestClient(app)
    client.cookies.set('odin_oidc_bind', routes_oidc.oidc_state_binding('fixture-state'))
    result = client.get('/auth/oidc/callback?code=fixture-code&state=fixture-state', follow_redirects=False)
    assert result.status_code == 403, result.text
    assert result.json()['detail']['code'] == 'license_user_locked'
    assert oidc_db.execute(text('SELECT COUNT(*) FROM oidc_auth_codes')).scalar() == 0


from tests.test_contracts.test_classroom_poc import classroom_db, _configure, _roster


def test_classroom_import_rechecks_actor_after_provider_round_trip(classroom_db, tier_license, monkeypatch):
    import asyncio
    from uuid import uuid4
    from core.auth import hash_password
    from modules.organizations import routes_classroom
    _configure(classroom_db)
    classroom_db.execute(text("INSERT INTO users(id,username,password_hash,role,is_active) VALUES(2,'local-recovery',:h,'admin',TRUE)"), {'h': hash_password('SyntheticPassword123!')}); classroom_db.commit()
    tier_license('education', max_users=None)
    monkeypatch.setenv('ODIN_LICENSE_FLEET_LOCK_ENABLED', 'true')
    policy.reconcile(classroom_db); classroom_db.commit()
    async def roster(*args):
        tier_license('education', max_users=1)
        return _roster()
    monkeypatch.setattr(routes_classroom, 'get_roster', roster)
    with pytest.raises(policy.LicenseLockError):
        asyncio.run(routes_classroom.import_classroom_course('course-123',
            routes_classroom.ClassroomImportInput(command_id=uuid4()),
            {'id': 1, 'role': 'admin', 'group_id': 1, 'actor_kind': 'user', 'is_superadmin': False}, classroom_db))
    classroom_db.rollback()
    assert classroom_db.execute(text('SELECT COUNT(*) FROM users')).scalar() == 2
