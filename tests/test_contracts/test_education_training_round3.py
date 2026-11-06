"""Round-three authority and HTTP contract regressions."""
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from core.db import get_db
from core.dependencies import get_current_user
from core.errors import OdinError
from modules.organizations import education_training_policy as policy
from modules.organizations import education_training_service as s
from tests.test_contracts.test_education_training import db, definition, command
from tests.test_contracts.test_education_training_http import client
from tests.test_contracts.test_education_review_workflow import review_db, _principal


@pytest.fixture
def school_training(db):
    # Definition lives in 7. The student also belongs to 8, managed by user 4.
    db.execute(text("INSERT INTO education_cost_centers(id,org_id,name_key,code_key,display_name,code,created_by) "
                    "VALUES(8,1,'robotics','rob','Robotics','ROB',3)"))
    db.execute(text("INSERT INTO education_cost_center_grants(org_id,cost_center_id,user_id,role,state,granted_by) "
                    "VALUES(1,8,1,'student','active',3),(1,8,4,'manager','active',3)"))
    db.commit()
    return s.save_training(db, _principal(3, 'admin'), definition(school_wide=True), center_id=7)


@pytest.mark.parametrize('actor', [2, 3, 4])
def test_school_signoff_and_revoke_by_any_own_class_manager_or_admin(client, school_training, actor):
    http, principal, _ = client
    principal.update(_principal(actor, 'admin' if actor == 3 else 'viewer'))
    base = f"/api/education/trainings/{school_training['id']}"
    for action, expected in [('signoff', 'complete'), ('revoke', 'revoked')]:
        response = http.post(base+f'/students/1/{action}', json={'command_id': str(uuid4()), 'revision': 1})
        assert response.status_code == 200, response.text
        assert response.json()['status'] == expected
    if actor != 3:
        response = http.put(base, json={**definition(school_wide=True).model_dump(mode='json'), 'revision': 1, 'edit_version': 1})
        assert response.status_code in (403, 404)


@pytest.mark.parametrize('change', [
    "UPDATE education_cost_center_grants SET state='revoked' WHERE user_id=4",
    "UPDATE education_cost_center_grants SET state='revoked' WHERE user_id=1 AND cost_center_id=8",
    "UPDATE education_cost_centers SET state='archived' WHERE id=8",
    "UPDATE users SET is_active=FALSE WHERE id=4",
    "UPDATE users SET group_id=2 WHERE id=4",
])
def test_school_assessment_requires_live_own_class_authority(client, db, school_training, change):
    s.assess(db, _principal(3, 'admin'), school_training['id'], command(), kind='signoff', user_id=1)
    db.execute(text(change)); db.commit()
    http, principal, _ = client
    principal.update(_principal(4, 'admin'))  # Forged principal role cannot bypass DB authority.
    for action in ('signoff', 'revoke'):
        response = http.post(f"/api/education/trainings/{school_training['id']}/students/1/{action}",
                             json={'command_id': str(uuid4()), 'revision': 1})
        assert response.status_code in (403, 404), response.text
    assert db.execute(text('SELECT revoked_at FROM education_training_records')).scalar_one() is None


def test_school_roster_and_signoff_exclude_unrelated_students(client, db, school_training):
    db.execute(text("INSERT INTO users(id,username,password_hash,role,is_active,group_id) "
                    "VALUES(6,'engineering-student','fixture','viewer',TRUE,1),(7,'robotics-student','fixture','viewer',TRUE,1)"))
    db.execute(text("INSERT INTO education_cost_center_grants(org_id,cost_center_id,user_id,role,state,granted_by) "
                    "VALUES(1,7,6,'student','active',3),(1,8,7,'student','active',3)"))
    db.commit()
    for student in (6, 7):
        s.assess(db, _principal(3, 'admin'), school_training['id'], command(), kind='signoff', user_id=student)
    http, principal, _ = client
    principal.update(_principal(4))
    listing = http.get('/api/education/cost-centers/8/trainings').json()['items'][0]
    assert listing['can_manage'] and not listing['can_edit']
    base = f"/api/education/trainings/{school_training['id']}"
    for manager, own, unrelated in ((4, 7, 6), (2, 6, 7)):
        principal.update(_principal(manager))
        assert {r['user_id'] for r in http.get(base+'/records').json()['items']} == {1, own}
        for action in ('signoff', 'revoke'):
            assert http.post(base+f'/students/{unrelated}/{action}', json={'command_id': str(uuid4()), 'revision': 1}).status_code == 404
    principal.update(_principal(3, 'admin'))
    assert {r['user_id'] for r in http.get(base+'/records').json()['items']} == {1, 6, 7}
    principal.update(_principal(5, 'admin'))
    assert http.get(base+'/records').status_code == 404


def test_upload_reapplies_cap_after_training_lock_restores_timeout(monkeypatch):
    from modules.organizations.education_submission_graph import _lock_job_birth_authority
    session = MagicMock(spec=Session)
    session.connection.return_value.dialect.name = 'postgresql'
    calls = []
    def lock(*args):
        calls.append('training timeout restored')
    def execute(sql, params=None):
        calls.append(str(sql))
        result = MagicMock()
        result.one_or_none.return_value = None
        return result
    monkeypatch.setattr(policy, 'lock_center', lock)
    session.execute.side_effect = execute
    with pytest.raises(OdinError):
        _lock_job_birth_authority(session, 'upload', _principal(1), 7)
    assert calls[0] == 'training timeout restored'
    assert calls[1] == "SET LOCAL lock_timeout = '1s'"
    assert calls[2].startswith('SELECT id,group_id,is_active FROM users')


@pytest.mark.parametrize('warning', [False, True])
def test_printer_delete_http_is_empty_204_with_json_warning_header(db, monkeypatch, warning):
    import json
    from core.registry import registry
    from modules.organizations.services import EducationPolicyService
    from modules.printers.routes_crud import router
    db.execute(text("INSERT INTO printers(id,name,model,machine_type,is_active,shared,org_id) VALUES(11,'School machine','Unique model','P1S',TRUE,FALSE,1)"))
    db.commit()
    if warning:
        body = definition(school_wide=True, scope_kind='model', scope_value='Unique model')
        body.name = 'Safety — école'
        s.save_training(db, _principal(3, 'admin'), body, center_id=7)
    original = registry.get_provider
    monkeypatch.setattr(registry, 'get_provider', lambda name: EducationPolicyService() if name == 'EducationPolicyProvider' else original(name))
    app = FastAPI()
    app.include_router(router, prefix='/api')
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: _principal(3, 'admin')
    response = TestClient(app).delete('/api/printers/11')
    assert response.status_code == 204, response.text
    assert response.content == b''
    if warning:
        assert 'Safety — école' in json.loads(response.headers['X-ODIN-Training-Warnings'])[0]
    else:
        assert 'X-ODIN-Training-Warnings' not in response.headers
    assert db.execute(text('SELECT id FROM printers WHERE id=11')).first() is None
