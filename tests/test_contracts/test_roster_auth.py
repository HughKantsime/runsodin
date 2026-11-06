"""D4 HTTP boundary and browser sign-in with no provider network."""
import json
import asyncio
import time
from urllib.parse import parse_qs, urlsplit
import jwt
import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy import text
from core.db import get_db
from core.dependencies import get_current_user
from core.errors import OdinError
from modules.organizations import routes_education_roster as routes, routes_roster_auth as auth
from modules.organizations import education_roster_store as store, education_roster_providers as providers
from tests.test_contracts.test_roster_sync import setup, review_db, enable, imported, canvas_config, platform_config, claims
from tests.test_contracts.test_education_review_workflow import _principal


@pytest.fixture
def client(setup):
    db, principal, _ = setup
    auth.limiter._storage.reset()
    app = FastAPI()
    app.include_router(routes.router, prefix='/api')
    app.include_router(auth.router, prefix='/api')
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: principal
    @app.exception_handler(OdinError)
    async def errors(request, exc): return JSONResponse(exc.to_envelope(), status_code=exc.status)
    with TestClient(app, base_url='https://odin.example.test') as c:
        yield c


@pytest.mark.parametrize('tier,allowed', [(None, False), ('pro', False), ('education', True), ('enterprise', True)])
def test_license_on_settings_and_public_auth(setup, client, tier_license, tier, allowed):
    connection = enable(setup)
    tier_license(tier)
    r = client.get('/api/education/roster')
    assert r.status_code == (200 if allowed else 403)
    if not allowed:
        assert 'licensed' in r.text.lower() or 'feature' in r.text.lower()
        assert client.get(f"/api/auth/classroom/{connection['id']}/login", follow_redirects=False).status_code == 403


@pytest.mark.parametrize('role,kind', [('viewer','bearer_jwt'), ('operator','bearer_jwt'), ('admin','user_api_token')])
def test_admin_and_interactive_floor(setup, client, role, kind):
    db, principal, saved = setup
    principal.update(role=role, _auth_kind=kind, _token_scopes=['write:education'] if kind == 'user_api_token' else [])
    r = client.get('/api/education/roster')
    assert r.status_code == 403


def test_mutating_routes_require_preview_and_no_cache(setup, client):
    connection = enable(setup)
    assert client.get('/api/education/roster').headers['cache-control'] == 'private, no-store'
    r = client.post(f"/api/education/roster/{connection['id']}/import", json={'preview_token':'x'*43})
    assert r.status_code == 409 and 'receipt_invalid' in r.text


def google_double(monkeypatch):
    async def double(method, url, **kw):
        if url == providers.GOOGLE_TOKEN: return {'access_token': 'fake'}, None
        assert url == providers.GOOGLE_INFO
        return {'sub': 'google-student', 'email': 'student.new@example.test', 'email_verified': True}, None
    monkeypatch.setattr(providers, 'request_json', double)


def callback_url(client, connection):
    r = client.get(f"/api/auth/classroom/{connection['id']}/login", follow_redirects=False)
    assert r.status_code == 302
    params = parse_qs(urlsplit(r.headers['location']).query)
    assert params['code_challenge_method'] == ['S256']
    return f"/api/auth/classroom/{connection['id']}/callback?state={params['state'][0]}&code=fake"


def test_google_login_browser_binding_replay_and_live_membership(setup, client, monkeypatch):
    db, _, _ = setup
    connection, result = imported(setup)
    google_double(monkeypatch)
    url = callback_url(client, connection)
    binding = client.cookies.get(auth.COOKIE)
    client.cookies.clear()
    assert client.get(url, follow_redirects=False).status_code == 403
    client.cookies.set(auth.COOKIE, binding)
    response = client.get(url, follow_redirects=False)
    assert response.status_code == 302 and '#roster_code=' in response.headers['location']
    assert client.get(url, follow_redirects=False).status_code == 409
    assert db.execute(text("SELECT COUNT(*) FROM audit_logs WHERE action='roster.signin_refused'")).scalar() >= 2
    fragment = parse_qs(urlsplit(response.headers['location']).fragment)
    db.execute(text("UPDATE education_cost_center_grants SET state='revoked' WHERE cost_center_id=:c"), {'c': result['cost_center_id']}); db.commit()
    r = client.post(f"/api/auth/classroom/{connection['id']}/exchange", json={'code': fragment['roster_code'][0]})
    assert r.status_code == 403
    assert db.execute(text('SELECT COUNT(*) FROM oidc_auth_codes')).scalar() == 0


def test_google_connect_admin_revalidation_and_token_secrecy(setup, client, monkeypatch):
    db, _, _ = setup
    connection = enable(setup)
    async def double(method, url, **kw):
        if url == providers.GOOGLE_TOKEN:
            return {'access_token': 'fake', 'refresh_token': 'refresh-test-only', 'scope': providers.SCOPES}, None
        return {'sub':'school-admin', 'email':'admin@example.test', 'email_verified':True}, None
    monkeypatch.setattr(providers, 'request_json', double)
    r = client.post(f"/api/education/roster/{connection['id']}/connect")
    state = parse_qs(urlsplit(r.json()['authorization_url']).query)['state'][0]
    from modules.organizations.education_roster_privacy import export_roster
    assert export_roster(db, 3)['pending_actions'][0]['kind'] == 'oauth'
    r = client.get(f"/api/auth/classroom/{connection['id']}/callback?state={state}&code=fake", follow_redirects=False)
    assert r.status_code == 302
    row = store.load(db, connection['id'])
    assert row['tokens']['refresh_token'] == 'refresh-test-only'
    assert 'refresh-test-only' not in row['tokens_encrypted']
    assert 'refresh-test-only' not in client.get('/api/education/roster').text


def test_canvas_real_signed_launch_registers_context_without_provisioning(setup, client, canvas_config, platform_config, monkeypatch):
    db, principal, _ = setup
    saved = store.save(db, principal, routes.Configuration(provider='canvas', enabled=True, config=canvas_config))
    base = f"/api/auth/classroom/{saved['id']}"
    target = 'https://odin.example.test' + base + '/launch'
    r = client.get(base+'/login', params={'iss': canvas_config['issuer'], 'login_hint':'hint', 'target_link_uri':target}, follow_redirects=False)
    params = parse_qs(urlsplit(r.headers['location']).query)
    assert 'SameSite=none' in r.headers['set-cookie'] and 'Secure' in r.headers['set-cookie']
    payload = claims(canvas_config)
    payload['nonce'] = params['nonce'][0]
    payload[providers.LTI + 'target_link_uri'] = target
    encoded = jwt.encode(payload, platform_config['private_key'], algorithm='RS256', headers={'kid':platform_config['key_id']})
    async def double(*a, **kw): return {'keys':[providers.public_jwk(platform_config)]}, None
    monkeypatch.setattr(providers, 'request_json', double)
    before = db.execute(text('SELECT COUNT(*) FROM users')).scalar()
    r = client.post(base+'/launch', data={'state':params['state'][0], 'id_token':encoded, 'lti_storage_target':'_parent', 'unknown':''}, follow_redirects=False)
    assert r.status_code == 302 and 'roster_import_required' in r.headers['location']
    assert db.execute(text('SELECT COUNT(*) FROM users')).scalar() == before
    assert store.load(db, saved['id'])['tokens']['contexts']['course']['title'] == 'Canvas class'
    assert client.post(base+'/launch', data={'state':params['state'][0], 'id_token':encoded, 'lti_storage_target':'_parent', 'unknown':''}, follow_redirects=False).status_code == 409


def test_api_tokens_cannot_change_roster_credentials():
    from core import credential_write_guard as guard
    from fastapi import HTTPException
    token = guard._NONINTERACTIVE.set(True)
    try:
        for sql in ['UPDATE education_roster_connections SET enabled=TRUE',
                    'INSERT INTO education_roster_identities(connection_id) VALUES(:c)',
                    'UPDATE education_roster_receipts SET payload_encrypted=:p']:
            with pytest.raises(HTTPException): guard.check_credential_write(sql, {})
    finally: guard._NONINTERACTIVE.reset(token)


def test_google_exchange_uses_real_session_and_one_time_receipt(setup, client, monkeypatch):
    db, _, _ = setup
    connection, _ = imported(setup)
    google_double(monkeypatch)
    r = client.get(callback_url(client, connection), follow_redirects=False)
    code = parse_qs(urlsplit(r.headers['location']).fragment)['roster_code'][0]
    r = client.post(f"/api/auth/classroom/{connection['id']}/exchange", json={'code': code})
    assert r.status_code == 200, r.text
    assert r.json()['token_type'] == 'bearer'
    assert 'session=' in r.headers['set-cookie']
    assert db.execute(text('SELECT COUNT(*) FROM active_sessions')).scalar() == 1
    assert client.post(f"/api/auth/classroom/{connection['id']}/exchange", json={'code': code}).status_code == 409


def test_disabled_connection_blocks_existing_login_receipt(setup, client, monkeypatch):
    db, _, _ = setup
    connection, _ = imported(setup)
    google_double(monkeypatch)
    r = client.get(callback_url(client, connection), follow_redirects=False)
    code = parse_qs(urlsplit(r.headers['location']).fragment)['roster_code'][0]
    db.execute(text('UPDATE education_roster_connections SET enabled=FALSE')); db.commit()
    r = client.post(f"/api/auth/classroom/{connection['id']}/exchange", json={'code': code})
    assert r.status_code == 409 and 'roster_disabled' in r.text
    assert db.execute(text('SELECT COUNT(*) FROM active_sessions')).scalar() == 0


@pytest.mark.parametrize('policy', ['required', 'enrolled'])
def test_roster_login_preserves_mfa_policy(setup, client, monkeypatch, policy):
    db, _, _ = setup
    connection, _ = imported(setup)
    google_double(monkeypatch)
    r = client.get(callback_url(client, connection), follow_redirects=False)
    code = parse_qs(urlsplit(r.headers['location']).fragment)['roster_code'][0]
    if policy == 'required':
        db.execute(text("INSERT INTO system_config(key,value) VALUES('require_mfa','true') ON CONFLICT(key) DO UPDATE SET value='true'"))
    else:
        from core.crypto import encrypt
        db.execute(text("UPDATE users SET mfa_enabled=TRUE,mfa_secret=:s WHERE id IN "
            "(SELECT user_id FROM education_roster_identities WHERE subject='google-student')"), {'s':encrypt('JBSWY3DPEHPK3PXP')})
    db.commit()
    r = client.post(f"/api/auth/classroom/{connection['id']}/exchange", json={'code':code})
    assert r.status_code == 200, r.text
    assert r.json()['mfa_setup_required' if policy == 'required' else 'mfa_required'] is True
    assert db.execute(text('SELECT COUNT(*) FROM active_sessions')).scalar() == 0


def test_explicit_education_license_must_include_workflow_opt_in(setup, client, tier_license):
    connection = enable(setup)
    tier_license('education', features=['roster_sync', 'classes'])
    assert client.get('/api/education/roster').status_code == 403
    assert client.get(f"/api/auth/classroom/{connection['id']}/login", follow_redirects=False).status_code == 403


def test_canvas_post_initiation_and_successful_imported_signin(setup, client, canvas_config, platform_config, monkeypatch):
    from modules.organizations import education_roster_sync as sync
    db, principal, _ = setup
    saved = store.save(db, principal, routes.Configuration(provider='canvas', enabled=True, config=canvas_config))
    connection = store.load(db, saved['id'])
    roster = {'id':'course', 'title':'Canvas class', 'term':'', 'members':[
        {'subject':'student', 'email':'canvas-student@example.test', 'role':'student'}]}
    plan = sync.preview(db, principal, connection, roster)
    sync.apply(db, principal, saved['id'], plan['preview_token'])
    base = f"/api/auth/classroom/{saved['id']}"
    target = 'https://odin.example.test' + base + '/launch'
    before = db.execute(text('SELECT COUNT(*) FROM audit_logs')).scalar()
    response = client.post(base + '/login', data={'iss':canvas_config['issuer'], 'login_hint':'hint',
        'target_link_uri':target, 'extra':'ignored'}, follow_redirects=False)
    assert response.status_code == 302, response.text
    assert db.execute(text('SELECT COUNT(*) FROM audit_logs')).scalar() == before
    params = parse_qs(urlsplit(response.headers['location']).query)
    payload = claims(canvas_config)
    payload['nonce'] = params['nonce'][0]; payload[providers.LTI + 'target_link_uri'] = target
    encoded = jwt.encode(payload, platform_config['private_key'], algorithm='RS256', headers={'kid':platform_config['key_id']})
    async def double(*a, **kw): return {'keys':[providers.public_jwk(platform_config)]}, None
    monkeypatch.setattr(providers, 'request_json', double)
    response = client.post(base + '/launch', data={'state':params['state'][0], 'id_token':encoded,
        'unknown':['one','two']}, follow_redirects=False)
    assert response.status_code == 302 and '#roster_code=' in response.headers['location']
    code = parse_qs(urlsplit(response.headers['location']).fragment)['roster_code'][0]
    response = client.post(base + '/exchange', json={'code':code})
    assert response.status_code == 200 and response.json()['token_type'] == 'bearer'
    assert db.execute(text('SELECT COUNT(*) FROM active_sessions')).scalar() == 1
    detail = db.execute(text("SELECT details FROM audit_logs WHERE action='auth.login'")).scalar()
    assert json.loads(detail)['method'] == 'classroom_canvas'
    verified = json.loads(db.execute(text("SELECT details FROM audit_logs WHERE action='roster.identity_verified'")).scalar())
    assert verified['user_id'] == db.execute(text('SELECT user_id FROM education_roster_identities')).scalar()


def test_anonymous_google_login_get_without_audit_row(setup, client):
    db, _, _ = setup
    connection = enable(setup)
    before = db.execute(text('SELECT COUNT(*) FROM audit_logs')).scalar()
    callback_url(client, connection)
    assert db.execute(text('SELECT COUNT(*) FROM audit_logs')).scalar() == before


def test_confirmed_google_adoption_through_import_route_can_sign_in(setup, client, monkeypatch):
    from modules.organizations import education_roster_sync as sync
    from tests.test_contracts.test_roster_sync import sample
    db, principal, _ = setup
    connection = enable(setup)
    db.execute(text("UPDATE users SET password_hash='',email='student.new@example.test' WHERE id=4")); db.commit()
    plan = sync.preview(db, principal, connection, sample())
    url = f"/api/education/roster/{connection['id']}/import"
    response = client.post(url, json={'preview_token':plan['preview_token']})
    assert response.status_code == 422 and 'roster_account_confirmation_required' in response.text
    assert db.execute(text('SELECT COUNT(*) FROM education_roster_identities WHERE user_id=4')).scalar() == 0
    response = client.post(url, json={'preview_token':plan['preview_token'], 'account_confirmations':[
        {'subject':'google-student', 'user_id':4, 'mode':'identity'}]})
    assert response.status_code == 200, response.text
    google_double(monkeypatch)
    response = client.get(callback_url(client, connection), follow_redirects=False)
    code = parse_qs(urlsplit(response.headers['location']).fragment)['roster_code'][0]
    response = client.post(f"/api/auth/classroom/{connection['id']}/exchange", json={'code':code})
    assert response.status_code == 200, response.text
    assert db.execute(text('SELECT user_id FROM active_sessions')).scalar() == 4
    assert json.loads(db.execute(text("SELECT details FROM audit_logs WHERE action='auth.login'")).scalar())['method'] == 'classroom_google'
