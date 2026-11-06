"""D2 authorization through ASGI (no loopback listener required)."""
import json
import pytest
from sqlalchemy import text
from core.rbac_routes import ACTION_ROUTES
from core.dependencies import get_current_user
from modules.organizations.education_classes_schemas import Assessment
from tests.test_contracts.test_education_classes import setup, client
from tests.test_contracts.test_education_review_workflow import review_db, _principal

ROUTES = [(key, method, path) for key, routes in ACTION_ROUTES.items()
          if key.startswith('education.classes_') for method, path in routes]


def body_for(method, path):
    if method == 'GET':
        return None
    if path == '/api/education/classes':
        return {'cost_center_id': 7, 'title': 'Class', 'term': 'Fall'}
    if path.endswith('/{class_id}'):
        return {'title': 'Class', 'term': 'Fall', 'revision': 1}
    if path.endswith('/assignments') or path.endswith('/{assignment_id}'):
        return {'title': 'Assignment', **({'revision': 1} if method == 'PUT' else {})}
    if method == 'POST':
        return {'submission_id': 14, 'revision': 1}
    return {'revision': 1, 'learning_status': 'reviewed'}


@pytest.mark.parametrize('key,method,path', ROUTES)
def test_every_new_route_obeys_live_permission_override(client, key, method, path):
    http, _, db, cls, assignment = client
    db.execute(text("INSERT INTO system_config(key,value) VALUES('rbac_permissions',:v) "
        'ON CONFLICT(key) DO UPDATE SET value=excluded.value'),
        {'v': json.dumps({'action_access': {key: ['admin']}})})
    db.commit()
    from tests.test_rbac import _resolve_path
    url = _resolve_path(path, {'class_id': cls['id'], 'assignment_id': assignment['id'], 'submission_id': 14})
    response = http.request(method, url, json=body_for(method, path))
    assert response.status_code == 403, response.text
    assert 'Insufficient permissions' in response.text


@pytest.mark.parametrize('key,method,path', ROUTES)
def test_every_new_route_refuses_expired_entitlement(client, tier_license, key, method, path):
    http, _, _, cls, assignment = client
    tier_license(None)
    url = path.replace('{class_id}', cls['id']).replace('{assignment_id}', assignment['id']).replace('{submission_id}', '14')
    response = http.request(method, url, json=body_for(method, path))
    assert response.status_code == 403
    assert "Feature 'classes' requires Education or Enterprise" in response.text


def test_matrix_widening_does_not_make_a_student_an_instructor(client):
    http, _, _, cls, _ = client
    response = http.post(f'/api/education/classes/{cls["id"]}/assignments', json={'title': 'Unauthorized'})
    assert response.status_code == 404


def test_revoked_teacher_cannot_read_grades_or_assess(client):
    from modules.organizations.education_assignment_submissions import assess
    from tests.test_contracts.test_education_classes import attach
    http, app, db, _, a = client
    attach(db, a)
    assess(db, _principal(2), a['id'], 14, Assessment(revision=1, learning_status='reviewed', feedback='Private'))
    db.execute(text("UPDATE education_cost_center_grants SET state='revoked' WHERE user_id=2")); db.commit()
    app.dependency_overrides[get_current_user] = lambda: _principal(2)
    path = f'/api/education/classes/assignments/{a["id"]}/submissions'
    assert http.get(path).status_code == 404
    assert http.put(path + '/14', json={'revision': 2, 'learning_status': 'complete'}).status_code == 404


@pytest.mark.parametrize('ids,expected', [
    ({}, '/classes/1/assignments/1/submissions/1'),
    ({'class_id': 'class-a', 'assignment_id': 'assignment-b', 'submission_id': 42},
     '/classes/class-a/assignments/assignment-b/submissions/42'),
])
def test_licensed_matrix_substitutes_class_assignment_and_submission(ids, expected):
    from tests.test_rbac import _resolve_path
    assert _resolve_path('/classes/{class_id}/assignments/{assignment_id}/submissions/{submission_id}', ids) == expected
