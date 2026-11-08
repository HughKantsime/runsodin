"""D4 offline integration: real schema, D2 classes, providers replaced only at HTTP."""
import asyncio
import copy
import json
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import jwt
import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from core.errors import OdinError
from modules.organizations import education_roster_store as store, education_roster_sync as sync
from modules.organizations import education_roster_providers as providers
from modules.organizations.routes_education_roster import Configuration
from tests.test_contracts.test_education_review_workflow import review_db, _principal


@pytest.fixture
def setup(review_db, tier_license, monkeypatch):
    tier_license('education')
    monkeypatch.setenv('ENCRYPTION_KEY', Fernet.generate_key().decode())
    # Any accidental real provider call fails immediately, without network access.
    async def forbidden(*args, **kwargs):
        raise AssertionError('Unexpected network call')
    monkeypatch.setattr(providers, 'request_json', forbidden)
    principal = _principal(3, 'admin')
    body = Configuration(provider='google', config={
        'client_id': 'test-client', 'client_secret': 'test-only-secret',
        'public_origin': 'https://odin.example.test', 'allowed_domains': ['example.test']})
    saved = store.save(review_db, principal, body)
    return review_db, principal, saved


def enable(setup):
    db, principal, saved = setup
    db.execute(text('UPDATE education_roster_connections SET enabled=TRUE WHERE id=:c'), {'c': saved['id']})
    db.commit()
    return store.load(db, saved['id'])


def sample():
    return {'id': 'course-1', 'title': 'Engineering', 'term': 'Fall', 'members': [
        {'subject': 'google-teacher', 'email': 'teacher.new@example.test', 'role': 'manager'},
        {'subject': 'google-student', 'email': 'student.new@example.test', 'role': 'student'}]}


def imported(setup):
    db, principal, _ = setup
    connection = enable(setup)
    preview = sync.preview(db, principal, connection, sample())
    result = sync.apply(db, principal, connection['id'], preview['preview_token'])
    return connection, result


def test_disabled_encrypted_and_redacted_config(setup):
    db, _, saved = setup
    assert saved['enabled'] is False
    assert 'client_secret' not in json.dumps(saved)
    row = db.execute(text('SELECT config_encrypted FROM education_roster_connections')).scalar_one()
    assert 'test-only-secret' not in row
    assert store.unpack(row)['client_secret'] == 'test-only-secret'
    with pytest.raises(OdinError, match='roster_disabled'):
        store.load(db, saved['id'])
    with pytest.raises(OdinError, match='not_found'):
        store.load(db, saved['id'], 2, enabled=False)


def test_dry_run_first_single_use_history_and_idempotent_resync(setup):
    db, principal, _ = setup
    connection = enable(setup)
    before = db.execute(text('SELECT COUNT(*) FROM users')).scalar()
    preview = sync.preview(db, principal, connection, sample())
    assert preview['create_users'] == 2
    assert db.execute(text('SELECT COUNT(*) FROM users')).scalar() == before
    assert db.execute(text('SELECT COUNT(*) FROM education_classes')).scalar() == 0
    result = sync.apply(db, principal, connection['id'], preview['preview_token'])
    with pytest.raises(OdinError, match='receipt_invalid'):
        sync.apply(db, principal, connection['id'], preview['preview_token'])
    db.rollback()
    second = sync.preview(db, principal, connection, sample())
    assert second['create_users'] == 0 and second['add_memberships'] == []
    assert sync.apply(db, principal, connection['id'], second['preview_token'])['class_id'] == result['class_id']
    removed = {**sample(), 'title': 'Renamed', 'members': sample()['members'][:1]}
    plan = sync.preview(db, principal, connection, removed)
    assert len(plan['revoke_memberships']) == 1
    sync.apply(db, principal, connection['id'], plan['preview_token'])
    assert db.execute(text('SELECT title FROM education_classes')).scalar() == 'Renamed'
    assert db.execute(text('SELECT COUNT(*) FROM users')).scalar() == before + 2
    assert db.execute(text("SELECT COUNT(*) FROM education_cost_center_grants WHERE cost_center_id=:c AND state='revoked'"), {'c': result['cost_center_id']}).scalar() == 1
    assert db.execute(text("SELECT COUNT(*) FROM education_audit_events WHERE action='roster.import'")).scalar() == 3
    assert 'student.new' not in str(db.execute(text('SELECT details FROM audit_logs')).all())


@pytest.mark.parametrize('change', ['revision', 'roster', 'expired', 'actor', 'disabled'])
def test_preview_invalidated(setup, change):
    db, principal, _ = setup
    connection = enable(setup)
    plan = sync.preview(db, principal, connection, sample())
    if change == 'revision': db.execute(text('UPDATE education_roster_connections SET revision=revision+1'))
    if change == 'roster': db.execute(text("UPDATE users SET email='changed@example.test' WHERE id=4"))
    if change == 'expired': db.execute(text('UPDATE education_roster_receipts SET expires_at=1'))
    if change == 'disabled': db.execute(text('UPDATE education_roster_connections SET enabled=FALSE'))
    db.commit()
    actor = _principal(5, 'admin', 2) if change == 'actor' else principal
    with pytest.raises(OdinError):
        sync.apply(db, actor, connection['id'], plan['preview_token'])
    db.rollback()
    assert db.execute(text('SELECT COUNT(*) FROM education_classes')).scalar() == 0


@pytest.mark.parametrize('case', ['foreign', 'inactive', 'duplicate_subject', 'duplicate_email'])
def test_identity_takeover_refused_without_partial_import(setup, case):
    db, principal, _ = setup
    connection = enable(setup)
    data = sample()
    if case == 'foreign': data['members'][0]['email'] = 'foreign@example.test'
    if case == 'inactive':
        db.execute(text("UPDATE users SET password_hash='',is_active=FALSE WHERE id=4")); data['members'][0]['email'] = 'outsider@example.test'
    if case == 'duplicate_subject': data['members'][1]['subject'] = data['members'][0]['subject']
    if case == 'duplicate_email': data['members'][1]['email'] = data['members'][0]['email']
    db.commit()
    with pytest.raises(OdinError):
        normalized = sync.normalize(data, connection['config'])
        sync.preview(db, principal, connection, normalized)
    db.rollback()
    assert db.execute(text('SELECT COUNT(*) FROM education_classes')).scalar() == 0


def test_rollback_partial_user_creation(setup, monkeypatch):
    db, principal, _ = setup
    connection = enable(setup)
    plan = sync.preview(db, principal, connection, sample())
    def broken(*a, **kw): raise RuntimeError('injected failure after user/grant writes')
    monkeypatch.setattr(sync, 'school_audit', broken)
    with pytest.raises(RuntimeError): sync.apply(db, principal, connection['id'], plan['preview_token'])
    db.rollback()
    assert db.execute(text('SELECT COUNT(*) FROM education_classes')).scalar() == 0
    assert db.execute(text('SELECT COUNT(*) FROM education_roster_identities')).scalar() == 0
    assert db.execute(text('SELECT COUNT(*) FROM education_roster_receipts')).scalar() == 1


def test_roster_import_capacity_refusal_rolls_back_and_can_retry(setup, tier_license):
    from fastapi import HTTPException
    db, principal, _ = setup
    connection = enable(setup)
    plan = sync.preview(db, principal, connection, sample())
    before = db.execute(text('SELECT COUNT(*) FROM users')).scalar()
    tier_license('education', max_users=before + 1)
    with pytest.raises(HTTPException) as refused:
        sync.apply(db, principal, connection['id'], plan['preview_token'])
    assert refused.value.status_code == 403
    db.rollback()
    assert db.execute(text('SELECT COUNT(*) FROM users')).scalar() == before
    assert db.execute(text('SELECT COUNT(*) FROM education_classes')).scalar() == 0
    tier_license('education', max_users=before + 2)
    result = sync.apply(db, principal, connection['id'], plan['preview_token'])
    assert result['class_id']
    assert db.execute(text('SELECT COUNT(*) FROM users')).scalar() == before + 2


def test_export_erasure_removes_identity_cache_and_keeps_connection(setup):
    from modules.organizations.education_roster_privacy import export_roster, erase_roster
    db, principal, _ = setup
    connection, result = imported(setup)
    uid = db.execute(text("SELECT user_id FROM education_roster_identities WHERE subject='google-student'")).scalar()
    exported = export_roster(db, uid)
    assert exported['identities'][0]['subject'] == 'google-student'
    assert 'google-teacher' not in json.dumps(exported)
    sync.preview(db, principal, connection, sample())
    erase_roster(db, uid); db.commit()
    assert db.execute(text('SELECT COUNT(*) FROM education_roster_receipts')).scalar() == 0
    assert store.load(db, connection['id'])['enabled']
    plan = sync.preview(db, principal, store.load(db, connection['id']), sample())
    assert len(plan['skipped']) == 1
    assert plan['skipped'][0]['reason'] == 'roster_erased_subject'
    assert len(plan['skipped'][0]['clear_handle']) == 43
    assert 'google-student' not in json.dumps(plan)
    sync.apply(db, principal, connection['id'], plan['preview_token'])
    assert db.execute(text("SELECT COUNT(*) FROM education_roster_identities WHERE subject='google-student'")).scalar() == 0
    assert not export_roster(db, uid)['identities']
    assert db.execute(text('SELECT COUNT(*) FROM education_classes')).scalar() == 1
    assert db.execute(text('SELECT COUNT(*) FROM education_submissions')).scalar() > 0


def test_google_api_double_pagination_and_members(setup, monkeypatch):
    connection = enable(setup)
    calls = []
    async def double(method, url, **kw):
        calls.append((method, url))
        if url == providers.GOOGLE_TOKEN: return {'access_token': 'fake'}, None
        if '/teachers?' in url: return {'teachers': [{'profile': {'id': 't', 'emailAddress': 'teacher@example.test'}}]}, None
        if '/students?' in url: return {'students': [{'profile': {'id': 's', 'emailAddress': 'student@example.test'}}]}, None
        if 'pageToken=next' not in url: return {'courses': [], 'nextPageToken': 'next'}, None
        return {'courses': [{'id': 'c', 'name': 'Class', 'courseState': 'ACTIVE'}]}, None
    monkeypatch.setattr(providers, 'request_json', double)
    result = asyncio.run(providers.google_roster(connection['config'], {'refresh_token': 'fake'}, 'c'))
    assert {m['role'] for m in result['members']} == {'manager', 'student'}
    assert len(calls) == 5


@pytest.fixture
def canvas_config():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    return {'provider': 'canvas', 'client_id': 'client', 'deployment_id': 'deployment',
        'issuer': 'https://canvas.example.test', 'public_origin': 'https://odin.example.test',
        'authorization_url': 'https://canvas.example.test/authorize', 'token_url': 'https://canvas.example.test/token',
        'jwks_url': 'https://canvas.example.test/jwks', 'api_origin': 'https://canvas.example.test',
        'key_id': 'test-key', 'private_key': pem, 'allowed_domains': ['example.test']}


@pytest.fixture
def platform_config():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return {'private_key': key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()).decode(), 'key_id': 'platform-key'}


def claims(config):
    now = int(time.time())
    return {'iss': config['issuer'], 'aud': config['client_id'], 'sub': 'student', 'exp': now + 300, 'iat': now,
        'nonce': 'nonce', providers.LTI + 'deployment_id': 'deployment', providers.LTI + 'version': '1.3.0',
        providers.LTI + 'message_type': 'LtiResourceLinkRequest', providers.LTI + 'target_link_uri': 'https://odin.example.test/launch',
        providers.LTI + 'context': {'id': 'course', 'title': 'Canvas class'},
        providers.NRPS: {'service_versions': ['2.0'], 'context_memberships_url': 'https://canvas.example.test/nrps'}}


@pytest.mark.parametrize('invalid', [None, 'iss', 'aud', 'nonce', 'deployment', 'target', 'expired', 'signature', 'origin'])
def test_canvas_verified_launch_double(canvas_config, platform_config, monkeypatch, invalid):
    config = canvas_config
    payload = claims(config)
    if invalid in ('iss', 'aud', 'nonce'): payload[invalid] = 'wrong'
    if invalid == 'deployment': payload[providers.LTI + 'deployment_id'] = 'other'
    if invalid == 'target': payload[providers.LTI + 'target_link_uri'] = 'https://evil.test/launch'
    if invalid == 'expired': payload['exp'] = 1
    if invalid == 'origin': payload[providers.NRPS]['context_memberships_url'] = 'https://evil.test/steal'
    signing = platform_config['private_key']
    if invalid == 'signature':
        signing = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = jwt.encode(payload, signing, algorithm='RS256', headers={'kid': platform_config['key_id']})
    async def double(*a, **kw): return {'keys': [providers.public_jwk(platform_config)]}, None
    monkeypatch.setattr(providers, 'request_json', double)
    task = providers.canvas_launch(config, {'nonce': 'nonce', 'callback': 'https://odin.example.test/launch'}, token)
    if invalid:
        with pytest.raises(OdinError): asyncio.run(task)
    else:
        verified, context = asyncio.run(task)
        assert verified['sub'] == 'student' and context['id'] == 'course'


def test_canvas_nrps_assertion_pagination_roles_and_origin(canvas_config, monkeypatch):
    config = canvas_config
    calls = []
    async def double(method, url, **kw):
        calls.append(url)
        if method == 'POST':
            verified = jwt.decode(kw['data']['client_assertion'], jwt.PyJWK.from_dict(providers.public_jwk(config)).key,
                                  algorithms=['RS256'], audience=config['token_url'])
            assert verified['iss'] == verified['sub'] == 'client'
            assert kw['data']['scope'] == providers.NRPS_SCOPE
            return {'access_token': 'fake'}, None
        role = 'Instructor' if url.endswith('/nrps') else 'Learner'
        return {'context': {'id': 'course'}, 'members': [{'status': 'Active', 'user_id': role,
            'email': role.lower() + '@example.test', 'roles': ['http://purl.imsglobal.org/vocab/lis/v2/membership#' + role]},
            {'status': 'Inactive', 'user_id': 'inactive'}]}, 'https://canvas.example.test/page2' if url.endswith('/nrps') else None
    monkeypatch.setattr(providers, 'request_json', double)
    context = {'id': 'course', 'title': 'Class', 'memberships_url': 'https://canvas.example.test/nrps'}
    result = asyncio.run(providers.canvas_roster(config, context))
    assert {m['role'] for m in result['members']} == {'manager', 'student'}
    assert len(calls) == 3
    with pytest.raises(OdinError): asyncio.run(providers.canvas_roster(config, {**context, 'memberships_url': 'https://evil.test'}))


def test_provider_transport_rejects_loopback_without_network():
    with pytest.raises(OdinError, match='provider_unavailable'):
        asyncio.run(providers.request_json('GET', 'https://127.0.0.1/secrets'))


def test_config_cannot_rebind_imported_subjects_to_another_namespace(setup):
    db, principal, _ = setup
    connection, _ = imported(setup)
    with pytest.raises(OdinError, match='namespace_immutable'):
        store.save(db, principal, Configuration(provider='google', enabled=True, revision=1,
            config={'client_id':'different-client'}))
    db.rollback()
    saved = store.save(db, principal, Configuration(provider='google', enabled=False, revision=1,
        config={'client_secret':'rotated-test-secret'}))
    assert saved['revision'] == 2 and saved['enabled'] is False


def test_existing_external_identity_is_not_merged_by_email(setup):
    db, principal, _ = setup
    connection, _ = imported(setup)
    data = sample()
    data['members'][0]['subject'] = 'different-subject'
    with pytest.raises(OdinError, match='identity_conflict'):
        sync.preview(db, principal, connection, data)
    db.rollback()
