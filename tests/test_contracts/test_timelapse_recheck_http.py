"""Session-authenticated, bounded re-check through the actual route and service."""
import json
import secrets

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from core.auth import create_access_token, hash_password
from core.db import get_db
from core.middleware.audit_actor import AuditActorMiddleware
from tests.test_contracts.test_timelapse_restart_ctime import ready, journal
from tests.test_contracts.test_timelapse_encoder_publication import encoder, frame_writer, review_db, owned_runtime

URL = '/api/timelapses/recheck-facts'


@pytest.fixture
def http_recheck(ready, monkeypatch):
    from modules.printers import camera_routes
    engine, root, tid, evidence, path = ready
    monkeypatch.setattr(camera_routes, 'TIMELAPSE_ROOT', str(root))
    app = FastAPI()
    app.add_middleware(AuditActorMiddleware)
    app.include_router(camera_routes.router, prefix='/api')
    def database():
        with Session(engine) as db:
            yield db
    app.dependency_overrides[get_db] = database
    with Session(engine) as db:
        username = db.execute(text('SELECT username FROM users WHERE id=3')).scalar_one()
    with TestClient(app) as client:
        client.cookies.set('session', create_access_token({'sub': username}))
        yield client, engine, tid, path, username


@pytest.mark.parametrize('selection', ['ids', 'all_ready'])
def test_session_rechecks_real_video_and_audits_signed_in_admin(http_recheck, selection):
    client, engine, tid, path, username = http_recheck
    response = client.post(URL, json={'timelapse_ids': [tid]} if selection == 'ids' else {'all_ready': True})
    assert response.status_code == 200, response.text
    assert response.json() == {'results': [{'timelapse_id': tid, 'status': 're-admitted', 'changed': True}], 'next_after_id': None}
    assert response.headers['cache-control'] == 'private, no-store'
    audit = journal(engine)[2][0]._mapping
    assert audit['actor_user_id'] == 3
    assert audit['actor_username'] == username
    details = json.loads(audit['details']) if isinstance(audit['details'], str) else audit['details']
    assert details['new_ctime_ns'] == path.stat().st_ctime_ns
    assert client.get(f'/api/timelapses/{tid}/download').status_code == 200
    assert client.post(URL, json={'timelapse_ids': [tid, tid]}).json()['results'] == [{'timelapse_id': tid, 'status': 're-admitted', 'changed': False}]
    assert len(journal(engine)[2]) == 1


@pytest.mark.parametrize('role,group', [('viewer', None), ('operator', None), ('admin', 1)])
def test_non_platform_admin_is_403_without_writes(http_recheck, role, group):
    client, engine, tid, _, _ = http_recheck
    with engine.begin() as db:
        db.execute(text('UPDATE users SET role=:role, group_id=:group WHERE id=3'), {'role': role, 'group': group})
    before = journal(engine)
    assert client.post(URL, json={'all_ready': True}).status_code == 403
    assert journal(engine) == before


@pytest.mark.parametrize('scopes', [[], ['read'], ['write'], ['admin'], ['read:printers', 'write:printers']])
def test_scoped_tokens_refused_even_for_superadmin(http_recheck, scopes):
    client, engine, tid, _, _ = http_recheck
    client.cookies.clear()
    key = 'odin_' + secrets.token_hex(24)
    with engine.begin() as db:
        db.execute(text("INSERT INTO api_tokens(name,user_id,token_prefix,token_hash,scopes) VALUES('owned',3,:prefix,:hash,:scopes)"), {'prefix': key[:10], 'hash': hash_password(key), 'scopes': json.dumps(scopes)})
    before = journal(engine)
    assert client.post(URL, json={'timelapse_ids': [tid]}, headers={'X-API-Key': key}).status_code == (403 if scopes else 401)
    assert journal(engine) == before


def test_global_key_and_anonymous_refused(http_recheck, monkeypatch):
    client, engine, tid, _, _ = http_recheck
    client.cookies.clear()
    before = journal(engine)
    assert client.post(URL, json={'all_ready': True}).status_code == 401
    monkeypatch.setenv('API_KEY', 'owned-test-global-key')
    assert client.post(URL, json={'all_ready': True}, headers={'X-API-Key': 'owned-test-global-key'}).status_code == 403
    assert journal(engine) == before


@pytest.mark.parametrize('body', [{}, {'all_ready': False}, {'all_ready': 'true'}, {'all_ready': 1}, {'timelapse_ids': []}, {'timelapse_ids': [0]}, {'timelapse_ids': [True]}, {'timelapse_ids': ['1']}, {'timelapse_ids': [2**63]}, {'timelapse_ids': list(range(1, 102))}, {'all_ready': True, 'timelapse_ids': [1]}, {'all_ready': True, 'limit': 200}])
def test_invalid_or_unbounded_selection_rejected_without_writes(http_recheck, body):
    client, engine, _, _, _ = http_recheck
    before = journal(engine)
    assert client.post(URL, json=body).status_code == 422
    assert journal(engine) == before


def test_all_ready_keyset_page_is_at_most_100_and_skips_other_states(http_recheck, monkeypatch):
    from modules.archives import timelapse_facts_recheck as service
    client, engine, tid, _, _ = http_recheck
    with engine.begin() as db:
        for identifier in range(1000, 1102):
            db.execute(text("INSERT INTO timelapses(id,printer_id,status,filename) SELECT :id,printer_id,'ready','owned-page-fixture.mp4' FROM timelapses WHERE id=:source"), {'id': identifier, 'source': tid})
        db.execute(text("UPDATE timelapses SET status='failed' WHERE id=1101"))
    seen = []
    def recheck(engine, root, identifier, evidence, **kwargs):
        seen.append(identifier)
        return {'status': 'unchanged'}
    monkeypatch.setattr(service, 'recheck_timelapse', recheck)
    first = client.post(URL, json={'all_ready': True}).json()
    assert len(first['results']) == len(seen) == 100
    assert first['next_after_id'] == 1098
    second = client.post(URL + '?after_id=1098', json={'all_ready': True}).json()
    assert [r['timelapse_id'] for r in second['results']] == [1099, 1100]
    assert second['next_after_id'] is None
    assert len(seen) == len(set(seen)) == 102
    seen.clear()
    assert len(client.post(URL, json={'timelapse_ids': list(range(1000, 1100))}).json()['results']) == len(seen) == 100
    assert client.post(URL + '?after_id=1', json={'timelapse_ids': [tid]}).status_code == 422
    assert client.post(URL + '?after_id=-1', json={'all_ready': True}).status_code == 422


def test_refused_results_include_reason_and_continue(http_recheck):
    client, engine, tid, path, _ = http_recheck
    import os
    original = path.stat()
    path.write_bytes(b'x' * original.st_size)
    os.utime(path, ns=(original.st_atime_ns, original.st_mtime_ns))
    before = journal(engine)
    response = client.post(URL, json={'timelapse_ids': [tid, 99999]})
    assert response.status_code == 200
    results = response.json()['results']
    assert results[0] == {'timelapse_id': tid, 'status': 'refused', 'reason': 'digest_mismatch'}
    assert results[1]['status'] == 'refused' and results[1]['reason']
    assert journal(engine) == before
