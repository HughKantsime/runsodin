"""D2 integration against the real migration, roster, license and review service."""
import asyncio
import importlib
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from core.errors import OdinError
from modules.organizations import education_classes_service as svc
from modules.organizations.education_assignment_submissions import link_submission, assess
from modules.organizations.education_classes_portfolio import portfolio
from modules.organizations.education_classes_schemas import ClassCreate, AssignmentCreate, SubmissionLink, Assessment
from tests.test_contracts.test_education_review_workflow import review_db, _principal


@pytest.fixture
def setup(review_db, tier_license):
    tier_license('education')
    review_db.execute(text("UPDATE print_files SET print_time_seconds=600,total_weight_grams=20,filaments_json='[{\"type\":\"PLA\"}]' WHERE id=11"))
    review_db.commit()
    cls = svc.create_class(review_db, _principal(2), ClassCreate(cost_center_id=7, title='Engineering', term='Fall 2026'))
    a = svc.save_assignment(review_db, _principal(2), cls['id'], AssignmentCreate(title='Gear', max_print_seconds=1200,
        max_material_grams=30, allowed_materials=['pla'], submission_quota=1))
    return review_db, cls, a


def attach(db, a):
    return link_submission(db, _principal(1), a['id'], SubmissionLink(submission_id=14, revision=1))


def test_migration_inventory_idempotence_and_exact_shape(setup):
    db, _, _ = setup
    m = importlib.import_module('core.schema.migrations.049_education_classes')
    m.apply(db.connection())
    assert all(set(v.split()) == {c['name'] for c in inspect(db.connection()).get_columns(t)} for t, v in m.COLUMNS.items())
    db.rollback()


def test_roster_visibility_and_roles(setup):
    db, cls, a = setup
    assert svc.list_classes(db, _principal(1))['items'][0]['can_submit']
    assert svc.list_classes(db, _principal(2))['items'][0]['can_manage']
    assert svc.list_classes(db, _principal(4))['items'] == []
    assert svc.list_classes(db, _principal(5, 'admin', 2))['items'] == []
    assert {r['role'] for r in svc.roster(db, _principal(2), cls['id'])['items']} == {'manager', 'student'}
    with pytest.raises(OdinError):
        svc.save_assignment(db, _principal(1), cls['id'], AssignmentCreate(title='No'))
    db.rollback()
    db.execute(text("UPDATE education_cost_center_grants SET state='revoked' WHERE user_id=2")); db.commit()
    with pytest.raises(OdinError):
        svc.assignments(db, _principal(2), cls['id'])


def test_link_idempotent_grade_private_and_revision_conflict(setup):
    db, cls, a = setup
    assert attach(db, a)['auto_approval'] == 'manual_review'
    assert attach(db, a)['linked']  # replay never consumes quota twice
    body = Assessment(revision=1, feedback='Good fit', grade='A', learning_status='complete')
    assess(db, _principal(2), a['id'], 14, body)
    item = portfolio(db, _principal(1))['items'][0]
    assert (item['feedback'], item['grade'], item['learning_status'], item['status']) == ('Good fit', 'A', 'complete', 'submitted')
    assert portfolio(db, _principal(4))['items'] == []
    assert portfolio(db, _principal(2), assignment_id=a['id'])['items'][0]['grade'] == 'A'
    with pytest.raises(OdinError):
        assess(db, _principal(2), a['id'], 14, body)
    db.rollback()
    with pytest.raises(OdinError):
        assess(db, _principal(1), a['id'], 14, Assessment(revision=2, learning_status='complete'))
    db.rollback()
    events = db.execute(text("SELECT action,details_json,result_json FROM education_audit_events WHERE action LIKE 'assignment.%'")).all()
    assert any(r.action == 'assignment.assess' for r in events)
    assert 'Good fit' not in str(events)


@pytest.mark.parametrize('column,value,message', [
    ('print_time_seconds', 1201, 'print time'), ('print_time_seconds', None, 'print time'),
    ('total_weight_grams', 31, 'material weight'), ('total_weight_grams', 0, 'material weight'),
    ('filaments_json', '[{"type":"PETG"}]', 'allowed material'),
    ('filaments_json', '[]', 'allowed material'), ('filaments_json', 'broken', 'allowed material'),
])
def test_limit_refusal_has_no_partial_link(setup, column, value, message):
    db, _, a = setup
    db.execute(text('UPDATE print_files SET ' + column + '=:v WHERE id=11'), {'v': value}); db.commit()
    with pytest.raises(OdinError, match=message):
        attach(db, a)
    db.rollback()
    assert db.execute(text('SELECT count(*) FROM education_assignment_submissions')).scalar_one() == 0
    assert db.execute(text('SELECT lifecycle_revision FROM education_submissions WHERE id=14')).scalar_one() == 1


@pytest.mark.parametrize('case', ['late', 'closed', 'archived', 'wrong_owner', 'wrong_center', 'reviewed', 'revoked', 'inactive'])
def test_admission_fails_closed(setup, case):
    db, cls, a = setup
    statements = {
        'late': ("UPDATE education_assignments SET due_at=:v", (datetime.now(timezone.utc)-timedelta(days=1)).isoformat()),
        'closed': ("UPDATE education_assignments SET state='closed'", None),
        'archived': ("UPDATE education_classes SET state='archived'", None),
        'wrong_owner': ('UPDATE education_submissions SET submitted_by=4', None),
        'wrong_center': ('UPDATE education_classes SET cost_center_id=8', None),
        'reviewed': ("UPDATE jobs SET status='pending' WHERE id=13", None),
        'revoked': ("UPDATE education_cost_center_grants SET state='revoked' WHERE user_id=1", None),
        'inactive': ('UPDATE users SET is_active=FALSE WHERE id=1', None),
    }
    if case == 'wrong_owner':
        db.execute(text('INSERT INTO education_memberships(user_id,org_id) VALUES(4,1) ON CONFLICT DO NOTHING'))
    if case == 'wrong_center':
        db.execute(text("INSERT INTO education_cost_centers(id,org_id,name_key,code_key,display_name,code,created_by) VALUES(8,1,'other','other','Other','other',3)"))
    stmt, value = statements[case]
    db.execute(text(stmt), {'v': value}); db.commit()
    with pytest.raises(OdinError):
        attach(db, a)
    db.rollback()
    assert db.execute(text('SELECT count(*) FROM education_assignment_submissions')).scalar_one() == 0


def test_auto_approval_runs_existing_compatibility_and_audit(setup):
    db, _, a = setup
    db.execute(text("UPDATE education_assignments SET auto_print_seconds=600,auto_material_grams=20,auto_printer_ids='[9]'")); db.commit()
    result = attach(db, a)
    assert result['auto_approval'] == 'approved'
    assert db.execute(text('SELECT status,approved_printer_id FROM education_submissions WHERE id=14')).one() == ('pending', 9)
    assert db.execute(text("SELECT count(*) FROM education_audit_events WHERE action='submission.approve'")).scalar_one() == 1


@pytest.mark.parametrize('change', ["auto_printer_ids='[10]'", 'auto_print_seconds=599', 'auto_material_grams=19', 'authorized_by=4'])
def test_unsafe_auto_approval_stays_manual(setup, change):
    db, _, a = setup
    db.execute(text("UPDATE education_assignments SET auto_print_seconds=600,auto_material_grams=20,auto_printer_ids='[9]'"))
    db.execute(text('INSERT INTO education_memberships(user_id,org_id) VALUES(4,1) ON CONFLICT DO NOTHING'))
    db.execute(text('UPDATE education_assignments SET ' + change)); db.commit()
    assert attach(db, a)['auto_approval'] == 'manual_review'
    assert db.execute(text('SELECT status FROM education_submissions WHERE id=14')).scalar_one() == 'submitted'


def test_review_rechecks_assignment_limits(setup):
    from modules.organizations.education_review_service import approve_submission
    from modules.organizations.education_schemas import SubmissionApproval
    import uuid
    db, _, a = setup
    attach(db, a)
    db.execute(text('UPDATE education_assignments SET max_print_seconds=599')); db.commit()
    with pytest.raises(OdinError, match='print time'):
        approve_submission(db, submission_id=14, principal=_principal(2),
            body=SubmissionApproval(revision=2, printer_id=9, command_id=uuid.uuid4()))
    assert db.execute(text('SELECT status FROM jobs WHERE id=13')).scalar_one() == 'submitted'


def test_gdpr_export_and_erase_preserve_history_without_feedback(setup):
    from modules.organizations.education_classes_privacy import export_classes, erase_classes
    db, _, a = setup
    attach(db, a)
    assess(db, _principal(2), a['id'], 14, Assessment(revision=1, feedback='student did well', grade='A', learning_status='complete'))
    exported = export_classes(db, 1)
    assert len(exported['assessments']) == 1
    assert len(exported['classes']) == 1
    assert exported['actions'][0]['action'] == 'assignment.link'
    erase_classes(db, 1, {'student'})
    db.commit()
    row = db.execute(text('SELECT feedback,grade,reviewed_by,learning_status FROM education_assignment_submissions')).one()
    assert row == ('', '', None, 'complete')
    assert db.execute(text('SELECT count(*) FROM education_audit_events')).scalar_one() > 0
    assert db.execute(text('SELECT count(*) FROM education_submissions')).scalar_one() == 1


@pytest.fixture
def client(setup):
    from core.db import get_db
    from core.dependencies import get_current_user
    from core.app_errors import register_error_handlers
    from modules.organizations.routes_education_classes import router
    db, cls, a = setup
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(router, prefix='/api')
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: _principal(1)
    with TestClient(app) as http:
        yield http, app, db, cls, a


@pytest.mark.parametrize('tier,expected', [('community',403),('pro',403),('education',200),('enterprise',200)])
def test_http_license_gate(client, tier_license, tier, expected):
    http, _, _, _, _ = client
    tier_license(None if tier == 'community' else tier)
    response = http.get('/api/education/classes')
    assert response.status_code == expected, response.text
    if expected == 403:
        assert 'classes' in response.text


def test_http_token_scope_and_matrix(client):
    from core.dependencies import get_current_user
    http, app, db, cls, a = client
    app.dependency_overrides[get_current_user] = lambda: {**_principal(1), '_auth_kind':'user_api_token', '_token_scopes':['read:education']}
    assert http.get('/api/education/classes').status_code == 200
    assert http.post(f'/api/education/classes/assignments/{a["id"]}/submissions', json={'submission_id':14,'revision':1}).status_code == 403
    app.dependency_overrides[get_current_user] = lambda: {**_principal(1), '_auth_kind':'global_api_key'}
    assert http.get('/api/education/classes').status_code == 403


def test_cross_tenant_foreign_keys_restrict_history(setup):
    db, cls, a = setup
    attach(db, a)
    with pytest.raises(IntegrityError):
        db.execute(text('DELETE FROM education_assignments WHERE id=:id'), {'id': a['id']})
    db.rollback()
    with pytest.raises(IntegrityError):
        db.execute(text('UPDATE education_assignment_submissions SET org_id=2'))
    db.rollback()
