"""Real training dependency trees: licenses, scoped credentials, RBAC, privacy."""
from uuid import uuid4
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from fastapi.responses import JSONResponse
from sqlalchemy import text
from core.db import get_db
from core.dependencies import get_current_user
from core.errors import OdinError
from license_manager import LicenseInfo, TIERS
from tests.test_contracts.test_education_training import db, create, sign
from tests.test_contracts.test_education_review_workflow import review_db, _principal


@pytest.fixture
def client(db, monkeypatch):
    import license_manager
    from modules.organizations import education_access
    from modules.organizations.routes_education_training import router
    license = LicenseInfo(); license.valid = True; license.tier = 'education'; license.features = ['education_workflows']
    monkeypatch.setattr(license_manager, 'get_license', lambda: license)
    monkeypatch.setattr(education_access, 'get_license', lambda: license)
    principal = _principal(2)
    app = FastAPI()
    @app.exception_handler(OdinError)
    async def handler(request: Request, error: OdinError):
        return JSONResponse(status_code=error.status, content=error.to_envelope())
    app.include_router(router, prefix='/api')
    app.include_router(router, prefix='/api/v1')
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: principal
    yield TestClient(app), principal, license


@pytest.mark.parametrize('tier,valid,features,allowed', [
    ('community', True, [], False), ('pro', True, ['training_signoff'], False),
    ('education', True, ['education_workflows'], True), ('enterprise', True, ['education_workflows'], True),
    ('education', False, ['education_workflows'], False), ('enterprise', True, [], False),
])
def test_license_refusal_and_signed_education_prerequisite(client, tier, valid, features, allowed):
    http, _, license = client
    license.tier, license.valid, license.features = tier, valid, features
    response = http.get('/api/education/cost-centers/7/trainings')
    assert response.status_code == (200 if allowed else 403), response.text
    if not allowed:
        assert 'requires Education or Enterprise' in response.text
    assert license.has_feature('training_signoff') is allowed
    assert ('training_signoff' in license.effective_features()) is allowed
    assert 'training_signoff' in TIERS['education']['features']
    assert 'training_signoff' in TIERS['enterprise']['features']


@pytest.mark.parametrize('auth,scopes,read,write', [
    ('session_jwt', [], 200, 200), ('bearer_jwt', [], 200, 200),
    ('user_api_token', ['read:education'], 200, 403),
    ('user_api_token', ['write:education'], 200, 200),
    ('user_api_token', ['write:jobs'], 403, 403),
    ('legacy_api_key', [], 403, 403),
])
def test_credential_scopes_and_aliases(client, auth, scopes, read, write):
    http, principal, _ = client
    principal.update(_auth_kind=auth, _token_scopes=scopes)
    for prefix in ('/api', '/api/v1'):
        assert http.get(prefix+'/education/cost-centers/7/trainings').status_code == read
        response = http.post(prefix+'/education/cost-centers/7/trainings', json={'command_id': str(uuid4()), 'name': 'Safety'})
        assert response.status_code == write, response.text


def test_student_cannot_write_roster_or_retrieve_answer_keys(client, db):
    http, principal, _ = client
    training = create(db, quiz=[{'prompt': 'Q', 'choices': ['A','B'], 'correct': 1}])
    principal.update(_principal(1))
    public = http.get('/api/education/cost-centers/7/trainings').json()
    assert 'correct' not in public['items'][0]['quiz'][0]
    assert http.get(f"/api/education/trainings/{training['id']}/records").status_code == 404
    for action in ('signoff', 'revoke'):
        response = http.post(f"/api/education/trainings/{training['id']}/students/1/{action}", json={'command_id': str(uuid4()), 'revision': 1})
        assert response.status_code == 404
    assert http.post('/api/education/cost-centers/7/trainings', json={'command_id': str(uuid4()), 'name': 'Bypass'}).status_code == 404


def test_role_matrix_restriction_is_additional_to_center_management(client, db):
    http, _, _ = client
    import json
    db.execute(text("INSERT INTO system_config(key,value) VALUES('rbac_permissions',:value) ON CONFLICT(key) DO UPDATE SET value=excluded.value"),
               {'value': json.dumps({'action_access': {'education.training_manage': ['admin']}})})
    db.commit()
    response = http.post('/api/education/cost-centers/7/trainings', json={'command_id': str(uuid4()), 'name': 'Safety'})
    assert response.status_code == 403, response.text


def test_only_mutations_write_training_audit(client, db):
    http, principal, _ = client
    training = http.post('/api/education/cost-centers/7/trainings', json={'command_id': str(uuid4()), 'name': 'Safety'}).json()
    http.get('/api/education/cost-centers/7/trainings')
    http.get(f"/api/education/trainings/{training['id']}/records")
    response = http.post(f"/api/education/trainings/{training['id']}/students/1/signoff", json={'command_id': str(uuid4()), 'revision': 1})
    assert response.status_code == 200, response.text
    assert db.execute(text("SELECT count(*) FROM education_audit_events WHERE resource_type='training'")).scalar_one() == 2


def test_gdpr_endpoint_wires_training_export_and_erasure(client, db):
    import asyncio
    from modules.organizations.routes_sessions import export_user_data, erase_user_data
    training = create(db); sign(db, training)
    exported = asyncio.run(export_user_data(1, _principal(1), db))
    assert exported['training']['records'][0]['training_id'] == training['id']
    result = asyncio.run(erase_user_data(1, _principal(3, 'admin'), db))
    assert result['status'] == 'ok'
    assert db.execute(text('SELECT revoked_at FROM education_training_records')).scalar_one()
    assert db.execute(text('SELECT username FROM users WHERE id=1')).scalar_one() != 'student'


def test_school_wide_definition_requires_live_tenant_admin(client, db):
    http, principal, _ = client
    base = '/api/education'
    def body(**changes):
        return {'command_id': str(uuid4()), 'name': 'School safety', **changes}
    assert http.post(base+'/cost-centers/7/trainings', json=body(school_wide=True)).status_code == 403
    principal.update(_principal(3, 'admin'))
    created = http.post(base+'/cost-centers/7/trainings', json=body(school_wide=True))
    assert created.status_code == 200, created.text
    training = created.json()
    principal.update(_principal(2))
    listing = http.get(base+'/cost-centers/7/trainings').json()
    assert not listing['can_manage_school_wide']
    assert not listing['items'][0]['can_edit']
    for school in (True, False):
        response = http.put(base+f"/trainings/{training['id']}", json=body(school_wide=school, revision=1, edit_version=1))
        assert response.status_code == 403, response.text
    principal.update(_principal(3, 'admin'))
    response = http.put(base+f"/trainings/{training['id']}", json=body(school_wide=False, revision=1, edit_version=1))
    assert response.status_code == 200, response.text
    assert response.json()['revision'] == 2 and response.json()['edit_version'] == 2


def test_text_update_stale_editor_http_conflict(client, db):
    http, _, _ = client
    training = create(db)
    path = f"/api/education/trainings/{training['id']}"
    payload = {'command_id': str(uuid4()), 'name': 'Renamed', 'revision': 1, 'edit_version': 1}
    saved = http.put(path, json=payload)
    assert saved.status_code == 200, saved.text
    assert saved.json()['revision'] == 1 and saved.json()['edit_version'] == 2
    stale = http.put(path, json={**payload, 'command_id': str(uuid4()), 'description': 'Stale text'})
    assert stale.status_code == 409, stale.text
