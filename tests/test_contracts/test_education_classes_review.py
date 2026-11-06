"""Regression contracts for the independently reviewed D2 decisions."""
import concurrent.futures
import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session
from core.errors import OdinError
from modules.organizations import education_classes_service as svc
from modules.organizations.education_assignment_submissions import link_submission, assess
from modules.organizations.education_classes_portfolio import portfolio, thumbnail
from modules.organizations.education_classes_privacy import export_classes, erase_classes
from modules.organizations.education_classes_schemas import AssignmentUpdate, ClassUpdate, Assessment, SubmissionLink
from modules.organizations.education_review_service import approve_submission
from modules.organizations.education_schemas import SubmissionApproval
from tests.test_contracts.test_education_classes import setup, client, attach
from tests.test_contracts.test_education_classes_edges import another_submission
from tests.test_contracts.test_education_review_workflow import review_db, _principal


def grade(db, a, revision=1, **fields):
    return assess(db, _principal(2), a['id'], 14, Assessment(revision=revision, **fields))


@pytest.mark.parametrize('user_id,identifiers,feedback,mark,reviewer,expected', [
    (1, set(), '', '', None, [14]),
    (2, set(), '', '', 2, [14]),
    (99, {'mentioned'}, 'By mentioned', '', None, [14]),
    (99, {'mentioned'}, '', 'mentioned', None, [14]),
    (99, {'name_%!'}, 'By name_%!', '', None, [14]),
    (99, {'name_%!'}, 'By nameABC!', '', None, []),
    (99, {'absent'}, 'Unrelated', 'A', None, []),
])
def test_erasure_lock_selects_owners_reviewers_and_text_mentions(
        setup, monkeypatch, user_id, identifiers, feedback, mark, reviewer, expected):
    from modules.organizations.education_classes_privacy import _lock_erased_assessments
    db, _, a = setup
    attach(db, a)
    db.execute(text('UPDATE education_assignment_submissions SET feedback=:f,grade=:g,reviewed_by=:r'),
               {'f': feedback, 'g': mark, 'r': reviewer})
    execute = db.execute
    selected = []

    def sqlite_select(statement, params):
        # Exercise the selection against real rows; PostgreSQL lock behavior is
        # covered separately by the native migration suite.
        sql = str(statement)
        assert sql.endswith('ORDER BY submission_id FOR UPDATE')
        result = execute(text(sql.removesuffix(' FOR UPDATE')), params)
        rows = result.all()
        selected.extend(row[0] for row in rows)
        from types import SimpleNamespace
        return SimpleNamespace(all=lambda: rows)

    monkeypatch.setattr(db, 'execute', sqlite_select)
    _lock_erased_assessments(db, user_id, identifiers)
    assert selected == expected


def test_teacher_export_and_erasure_preserve_students_assessment_and_revisions(setup, monkeypatch):
    db, _, a = setup
    attach(db, a)
    from types import SimpleNamespace
    from modules.organizations import education_assessments
    ids = iter([uuid.UUID(int=2), uuid.UUID(int=1)])
    monkeypatch.setattr(education_assessments, 'uuid', SimpleNamespace(uuid4=lambda: next(ids)))
    grade(db, a, feedback='manager: Good fit', grade='A', learning_status='reviewed')
    grade(db, a, revision=2, feedback='manager: Excellent', grade='A+')
    # Reversed UUID order must not override chronological revision order.
    student = export_classes(db, 1)
    assert student['assessment_revisions'][0]['new_grade'] == 'A'
    assert len(student['assessment_revisions']) == 2
    teacher = export_classes(db, 2)
    assert teacher['assessments'] == teacher['assessment_revisions'] == []
    assert teacher['assessments_reviewed'][0]['role'] == 'reviewer'
    assert len(teacher['assessment_revisions_reviewed']) == 2
    assert all('grade' not in key and 'feedback' not in key for row in teacher['assessment_revisions_reviewed'] for key in row)
    erase_classes(db, 2, {'manager', 'manager@example.test'})
    db.commit()
    assert db.execute(text('SELECT grade,feedback,reviewed_by FROM education_assignment_submissions')).one() == ('A+', '[deleted-user]: Excellent', None)
    history = db.execute(text('SELECT old_grade,new_grade,old_feedback,new_feedback,reviewed_by FROM education_assessment_revisions ORDER BY revision')).all()
    assert history == [('', 'A', '', '[deleted-user]: Good fit', None), ('A', 'A+', '[deleted-user]: Good fit', '[deleted-user]: Excellent', None)]
    erase_classes(db, 1, {'student'})
    db.commit()
    assert all(tuple(row) == ('', '', '', '', None) for row in db.execute(text('SELECT old_grade,new_grade,old_feedback,new_feedback,reviewed_by FROM education_assessment_revisions')))


def test_partial_updates_preserve_unsent_rules_class_state_and_marks(setup):
    db, cls, a = setup
    updated = svc.save_assignment(db, _principal(2), cls['id'], AssignmentUpdate(revision=1, title='New title'), a['id'])
    for field in ('submission_quota', 'max_print_seconds', 'max_material_grams', 'allowed_materials'):
        assert updated[field] == a[field]
    attach(db, a)
    grade(db, a, feedback='Keep this', grade='A', learning_status='reviewed')
    result = grade(db, a, revision=2, learning_status='complete')
    assert (result['feedback'], result['grade']) == ('Keep this', 'A')
    svc.update_class(db, _principal(2), cls['id'], ClassUpdate(revision=1, state='archived'))
    result = svc.update_class(db, _principal(2), cls['id'], ClassUpdate(revision=2, title='Archived title'))
    assert result['state'] == 'archived' and result['term'] == cls['term']


def test_grading_archived_center_retains_history_and_authority(setup):
    db, _, a = setup
    attach(db, a)
    db.execute(text("UPDATE education_cost_centers SET state='archived'")); db.commit()
    assert grade(db, a, grade='A')['grade'] == 'A'
    assert db.execute(text('SELECT count(*) FROM education_assessment_revisions')).scalar_one() == 1
    with pytest.raises(OdinError):
        grade(db, a, grade='B')  # stale revision creates no history
    db.rollback()
    assert db.execute(text('SELECT count(*) FROM education_assessment_revisions')).scalar_one() == 1
    db.execute(text("UPDATE education_cost_center_grants SET state='revoked' WHERE user_id=2")); db.commit()
    with pytest.raises(OdinError):
        grade(db, a, revision=2, grade='B')


@pytest.mark.parametrize('change', ['deadline', 'rule', 'student_grant', 'closed', 'archived'])
def test_repeat_link_never_auto_approves_after_admission_changes(setup, change):
    db, _, a = setup
    attach(db, a)
    statements = {
        'deadline': "UPDATE education_assignments SET due_at='2000-01-01T00:00:00+00:00'",
        'rule': 'UPDATE education_assignments SET max_print_seconds=1',
        'student_grant': "UPDATE education_cost_center_grants SET role='manager' WHERE user_id=1",
        'closed': "UPDATE education_assignments SET state='closed'",
        'archived': "UPDATE education_classes SET state='archived'",
    }
    db.execute(text("UPDATE education_assignments SET auto_printer_ids='[9]',auto_print_seconds=600,auto_material_grams=20"))
    db.execute(text(statements[change])); db.commit()
    assert attach(db, a)['auto_approval'] == 'submitted'
    assert db.execute(text('SELECT status FROM education_submissions WHERE id=14')).scalar_one() == 'submitted'


def test_concurrent_approvals_consume_quota_once(setup):
    db, _, a = setup
    another_submission(db)
    attach(db, a)
    link_submission(db, _principal(1), a['id'], SubmissionLink(submission_id=20, revision=1))
    # The second fixture lacks compatibility facts; copy the actual compatible payload facts.
    db.execute(text("UPDATE print_files SET blob_state='present',storage_bytes=100,compatibility_facts_json=(SELECT compatibility_facts_json FROM print_files WHERE id=11) WHERE id=20"))
    db.commit()
    def approve(n):
        with Session(db.get_bind()) as session:
            try:
                approve_submission(session, submission_id=n, principal=_principal(2),
                    body=SubmissionApproval(revision=2, printer_id=9, command_id=uuid.uuid4()))
                return 'approved'
            except OdinError as error:
                session.rollback()
                return str(error)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(approve, [14, 20]))
    assert results.count('approved') == 1
    assert any('quota' in result for result in results)
    assert db.execute(text('SELECT COUNT(*) FROM education_assignment_submissions WHERE first_approved_at IS NOT NULL')).scalar_one() == 1


@pytest.mark.parametrize('state', ['submitted', 'rejected', 'cancelled', 'failed'])
def test_never_approved_submissions_do_not_consume_quota(setup, state):
    db, _, a = setup
    attach(db, a)
    db.execute(text('UPDATE education_submissions SET status=:state WHERE id=14'), {'state': state}); db.commit()
    another_submission(db)
    assert link_submission(db, _principal(1), a['id'], SubmissionLink(submission_id=20, revision=1))['linked']


def test_linked_approval_survives_classes_license_lapse(setup, tier_license, monkeypatch):
    import copy
    import license_manager as lm
    db, _, a = setup
    attach(db, a)
    tiers = copy.deepcopy(lm.TIERS)
    tiers['education']['features'].remove('classes')
    monkeypatch.setattr(lm, 'TIERS', tiers)
    tier_license('education', features=['education_workflows'])
    assert approve_submission(db, submission_id=14, principal=_principal(2),
        body=SubmissionApproval(revision=2, printer_id=9, command_id=uuid.uuid4()))['status'] == 'pending'


def test_auto_approval_tries_next_printer_and_marks_automatic(setup):
    db, _, a = setup
    # First printer incompatible; second compatible. Sorted IDs are deterministic.
    db.execute(text('UPDATE printers SET nozzle_diameter=0.6 WHERE id=9'))
    db.execute(text('UPDATE printers SET nozzle_diameter=0.4 WHERE id=10'))
    db.execute(text("UPDATE education_assignments SET auto_printer_ids='[9,10]',auto_print_seconds=600,auto_material_grams=20")); db.commit()
    assert attach(db, a)['auto_approval'] == 'approved'
    assert db.execute(text('SELECT approved_printer_id FROM education_submissions')).scalar_one() == 10
    auto = db.execute(text("SELECT actor_kind,details_json FROM education_audit_events WHERE action='assignment.auto_approve'")).one()
    assert auto.actor_kind == 'system' and json.loads(auto.details_json)['automatic'] is True
    decision = db.execute(text("SELECT details_json FROM education_audit_events WHERE action='submission.approve'")).scalar_one()
    assert json.loads(decision)['approver'] == 'automatic'


@pytest.mark.parametrize('role,center', [('student', 7), ('manager', 8)])
def test_classmate_and_other_center_manager_cannot_read_peer_portfolio(setup, role, center):
    db, _, a = setup
    attach(db, a)
    grade(db, a, feedback='Private', grade='A')
    if center == 8:
        db.execute(text("INSERT INTO education_cost_centers(id,org_id,name_key,code_key,display_name,code,created_by) VALUES(8,1,'other','other','Other','other',3)"))
    db.execute(text("INSERT INTO education_cost_center_grants(org_id,cost_center_id,user_id,role,state,granted_by) VALUES(1,:c,4,:role,'active',3)"), {'c': center, 'role': role}); db.commit()
    assert portfolio(db, _principal(4))['items'] == []
    if role == 'student':
        assert portfolio(db, _principal(4), assignment_id=a['id'])['items'] == []
    else:
        with pytest.raises(OdinError):
            portfolio(db, _principal(4), assignment_id=a['id'])
    with pytest.raises(OdinError):
        thumbnail(db, _principal(4), 14)


def test_read_routes_write_no_audit_rows(client):
    from core.dependencies import get_current_user
    http, app, db, cls, a = client
    attach(db, a)
    app.dependency_overrides[get_current_user] = lambda: _principal(2)
    before = [db.execute(text('SELECT COUNT(*) FROM ' + table)).scalar_one() for table in ('education_audit_events', 'audit_logs')]
    for suffix in ('', '/portfolio', f'/{cls["id"]}/roster', f'/{cls["id"]}/assignments', f'/assignments/{a["id"]}/submissions'):
        assert http.get('/api/education/classes' + suffix).status_code == 200
    after = [db.execute(text('SELECT COUNT(*) FROM ' + table)).scalar_one() for table in ('education_audit_events', 'audit_logs')]
    assert after == before


def test_teacher_gdpr_http_keeps_student_marks_after_downgrade(client, tier_license):
    from core.dependencies import get_current_user
    from modules.organizations.routes_sessions import router
    http, app, db, _, a = client
    attach(db, a)
    grade(db, a, feedback='manager: Keep learning', grade='A')
    app.include_router(router, prefix='/api')
    tier_license(None)
    app.dependency_overrides[get_current_user] = lambda: _principal(3, 'admin')
    response = http.get('/api/users/2/export')
    assert response.status_code == 200, response.text
    classes = response.json()['education_classes']
    assert classes['assessments'] == classes['assessment_revisions'] == []
    assert classes['assessment_revisions_reviewed'][0]['role'] == 'reviewer'
    response = http.delete('/api/users/2/erase')
    assert response.status_code == 200, response.text
    assert db.execute(text('SELECT grade,feedback,reviewed_by FROM education_assignment_submissions')).one() == ('A', '[deleted-user]: Keep learning', None)
    assert db.execute(text('SELECT new_grade,new_feedback,reviewed_by FROM education_assessment_revisions')).one() == ('A', '[deleted-user]: Keep learning', None)


def test_erasure_prefilters_and_pages_mentions_without_changing_unrelated_text(setup):
    from sqlalchemy import event
    db, cls, _ = setup
    db.execute(text('INSERT INTO education_classes(id,org_id,cost_center_id,title,term,created_by) '
        'VALUES(:id,1,7,:title,:term,2)'), [
            {'id': f'chunk-{n:04}', 'title': 'manager course' if n < 405 else 'Unrelated course', 'term': 'Fall'}
            for n in range(410)])
    db.commit()
    statements = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith('SELECT id,title,term FROM education_classes'):
            statements.append(statement)
    engine = db.get_bind()
    event.listen(engine, 'before_cursor_execute', capture)
    try:
        erase_classes(db, 2, {'manager'})
        db.commit()
    finally:
        event.remove(engine, 'before_cursor_execute', capture)
    assert len(statements) >= 3
    assert all('LIKE' in sql and 'LIMIT 200' in sql for sql in statements)
    assert db.execute(text("SELECT COUNT(*) FROM education_classes WHERE title='[deleted-user] course'")).scalar_one() == 405
    assert db.execute(text("SELECT COUNT(*) FROM education_classes WHERE title='Unrelated course' AND revision=1")).scalar_one() == 5


def test_incomplete_partial_auto_rule_returns_422_without_changing_policy(client):
    from core.dependencies import get_current_user
    http, app, db, cls, a = client
    app.dependency_overrides[get_current_user] = lambda: _principal(2)
    response = http.put(f'/api/education/classes/{cls["id"]}/assignments/{a["id"]}',
                        json={'revision': 1, 'auto_printer_ids': [9]})
    assert response.status_code == 422, response.text
    db.rollback()
    assert db.execute(text('SELECT auto_printer_ids,revision FROM education_assignments')).one() == ('[]', 1)


def test_erasure_prefilter_preserves_unicode_identifier_case_on_sqlite(setup):
    db, cls, _ = setup
    db.execute(text('UPDATE education_classes SET title=:title WHERE id=:id'),
               {'id': cls['id'], 'title': 'MÁNAGER course'})
    db.commit()
    erase_classes(db, 2, {'MÁNAGER'})
    assert db.execute(text('SELECT title FROM education_classes WHERE id=:id'), {'id': cls['id']}).scalar_one() == '[deleted-user] course'


@pytest.mark.parametrize('status', ['submitted', 'pending', 'scheduled', 'printing', 'completed', 'failed', 'rejected', 'cancelled'])
def test_repeat_link_returns_current_status_without_auto_approval(setup, monkeypatch, status):
    db, _, a = setup
    attach(db, a)
    db.execute(text('UPDATE education_submissions SET status=:status WHERE id=14'), {'status': status})
    db.commit()
    from modules.organizations import education_assignment_submissions as links
    monkeypatch.setattr(links, 'try_auto_approve', lambda *args: pytest.fail('repeat link must not auto approve'))
    assert attach(db, a)['auto_approval'] == status
