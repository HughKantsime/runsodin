"""Round-one review regressions: shared printers, cooldown, privacy and queues."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4
import pytest
from sqlalchemy import event, text
from core.errors import OdinError
from modules.organizations import education_training_service as s
from modules.organizations import education_training_policy as policy
from modules.organizations.education_training_schemas import TrainingUpdate
from tests.test_contracts.test_education_training import db, create, sign, quiz, definition, missing, QUIZ
from tests.test_contracts.test_education_review_workflow import review_db, _principal


def second_class(db):
    db.execute(text("INSERT INTO education_cost_centers(id,org_id,name_key,code_key,display_name,code,state,revision,created_by) "
                    "VALUES(8,1,'robotics','rob','Robotics','ROB','active',1,3)"))
    db.execute(text("INSERT INTO education_cost_center_grants(org_id,cost_center_id,user_id,role,state,granted_by) "
                    "VALUES(1,8,2,'manager','active',3)"))
    db.execute(text("INSERT INTO education_cost_center_printers(org_id,cost_center_id,printer_id,state,granted_by) "
                    "VALUES(1,8,9,'active',3)"))
    db.commit()


def test_shared_printer_requires_every_class_training_and_student_can_complete_it(db):
    second_class(db)
    first = create(db)
    other = s.save_training(db, _principal(2), definition(), center_id=8)
    sign(db, first)
    assert [t['id'] for t in missing(db)] == [other['id']]
    assert missing(db, printer=10) == []  # Not authorized for the other class.
    assert {t['id'] for t in s.list_trainings(db, _principal(1), 7)['items']} == {first['id'], other['id']}
    assert s.training_access(db, _principal(1), other['id'])[1] is False
    assert s.roster(db, _principal(2), other['id'])['items'][0]['user_id'] == 1
    sign(db, other)
    assert missing(db) == []
    assert policy.missing_training(db, org_id=2, center_id=8, user_id=5, printer_id=9) == []


def test_school_wide_requirement_applies_outside_owner_class_printer_set(db):
    second_class(db)
    training = s.save_training(db, _principal(3, 'admin'), definition(school_wide=True, quiz=QUIZ, instructor_required=False), center_id=8)
    assert missing(db, printer=10)[0]['id'] == training['id']
    assert quiz(db, training, [1, 0])['status'] == 'complete'
    assert not missing(db, printer=10)


@pytest.mark.parametrize('changes', [{'name': 'Renamed'}, {'description': 'Clearer instructions'}, {'expiry_days': 2}, {'active': False}])
def test_non_requirement_edits_keep_completion_and_original_expiry(db, changes):
    training = create(db, expiry_days=1); sign(db, training)
    before = db.execute(text('SELECT completed_at,expires_at FROM education_training_records')).one()
    body = definition(expiry_days=1).model_dump(); body.update(changes)
    saved = s.save_training(db, _principal(3, 'admin'), TrainingUpdate(edit_version=1, **body, revision=1), training_id=training['id'])
    assert saved['revision'] == 1
    assert db.execute(text('SELECT completed_at,expires_at FROM education_training_records')).one() == before
    assert not missing(db)


@pytest.mark.parametrize('changes', [{'quiz': QUIZ}, {'pass_mark': 90}, {'scope_kind': 'model', 'scope_value': 'Bambu Lab P1S'}, {'school_wide': True}, {'instructor_required': False}])
def test_requirement_edits_invalidate_prior_completion(db, changes):
    training = create(db, quiz=QUIZ); quiz(db, training, [1, 0]); sign(db, training)
    body = definition(quiz=QUIZ).model_dump(); body.update(changes)
    if changes == {'quiz': QUIZ}:
        body['quiz'] = [dict(QUIZ[0], prompt='Changed question'), QUIZ[1]]
    saved = s.save_training(db, _principal(3, 'admin'), TrainingUpdate(edit_version=1, **body, revision=1), training_id=training['id'])
    assert saved['revision'] == 2
    assert missing(db)[0]['status'] == 'outdated'


def test_three_failed_attempts_persist_cooldown_and_roster_count(db):
    training = create(db, quiz=QUIZ, instructor_required=False)
    for _ in range(3):
        result = quiz(db, training, [0, 1])
        assert not {'correct', 'answers', 'question_results'}.intersection(result)
    assert result['quiz_attempts'] == 3 and result['cooldown_until']
    with pytest.raises(OdinError) as error:
        quiz(db, training, [1, 0])
    assert error.value.status == 429
    db.rollback()
    row = s.roster(db, _principal(2), training['id'])['items'][0]['record']
    assert row['quiz_attempts'] == 3 and row['cooldown_until']
    db.execute(text('UPDATE education_training_records SET cooldown_until=:past'), {'past': datetime.now(timezone.utc)-timedelta(seconds=1)}); db.commit()
    assert quiz(db, training, [1, 0])['status'] == 'complete'
    assert s.roster(db, _principal(2), training['id'])['items'][0]['record']['quiz_attempts'] == 4


def test_teacher_export_excludes_student_records_and_erasure_preserves_revocation(db):
    training = create(db); sign(db, training)
    assert policy.export_training_data(db, 2)['records'] == []
    sign(db, training, 'revoke')
    revoked = db.execute(text('SELECT revoked_at FROM education_training_records')).scalar_one()
    policy.erase_training_data(db, 1, 3); db.commit()
    assert db.execute(text('SELECT revoked_at FROM education_training_records')).scalar_one() == revoked


def test_list_roster_preview_are_read_only_and_export_filters_in_sql(db):
    from modules.organizations.education_review_service import preview_submission_compatibility
    training = create(db)
    statements = []
    def capture(conn, cursor, sql, parameters, context, many):
        statements.append(sql)
    event.listen(db.get_bind(), 'before_cursor_execute', capture)
    try:
        s.list_trainings(db, _principal(1), 7)
        s.roster(db, _principal(2), training['id'])
        with pytest.raises(OdinError, match='Training required'):
            preview_submission_compatibility(db, submission_id=14, printer_id=9, revision=1, principal=_principal(2))
        policy.export_training_data(db, 1)
    finally:
        event.remove(db.get_bind(), 'before_cursor_execute', capture)
    assert not any(sql.lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE')) for sql in statements)
    assert any("resource_type='training' AND (actor_id=" in sql for sql in statements)


@pytest.mark.parametrize('change', ['revoked', 'expired'])
def test_queued_job_returns_to_review_with_missing_training_name(db, monkeypatch, change):
    from modules.organizations.education_review_service import approve_submission
    from modules.organizations.education_schemas import SubmissionApproval
    from tests.test_contracts.test_education_scheduler_policy import _install_policy, _run
    training = create(db, expiry_days=1); sign(db, training)
    approve_submission(db, submission_id=14, principal=_principal(2), body=SubmissionApproval(command_id=uuid4(), revision=1, printer_id=9))
    if change == 'revoked':
        sign(db, training, 'revoke')
    else:
        db.execute(text('UPDATE education_training_records SET expires_at=:past'), {'past': datetime.now(timezone.utc)-timedelta(seconds=1)}); db.commit()
    db.execute(text("UPDATE jobs SET hold=FALSE,duration_hours=1,quantity=1,quantity_on_bed=1 WHERE id=13")); db.commit()
    _install_policy(monkeypatch)
    result = _run(db)
    assert result.success, result.errors
    assert db.execute(text('SELECT status FROM education_submissions WHERE id=14')).scalar_one() == 'submitted'
    assert 'P1S safety' in db.execute(text('SELECT notes FROM jobs WHERE id=13')).scalar_one()


def test_approval_to_different_class_printer_checks_that_printers_training(db):
    from modules.organizations.education_review_service import approve_submission
    from modules.organizations.education_schemas import SubmissionApproval
    db.execute(text("UPDATE printers SET model='Special P1S',nozzle_diameter=0.4 WHERE id=10")); db.commit()
    training = create(db, scope_kind='model', scope_value='Special P1S')
    assert not missing(db, printer=9)
    body = SubmissionApproval(command_id=uuid4(), revision=1, printer_id=10)
    with pytest.raises(OdinError, match='Training required'):
        approve_submission(db, submission_id=14, principal=_principal(2), body=body)
    db.rollback(); sign(db, training)
    assert approve_submission(db, submission_id=14, principal=_principal(2), body=body)['status'] == 'pending'
    assert db.execute(text('SELECT approved_printer_id FROM education_submissions')).scalar_one() == 10


def test_upload_commit_checks_active_student_before_training(db):
    from modules.organizations.education_submission_service import _commit_submission_graph
    create(db)
    db.execute(text("UPDATE education_upload_operations SET state='renamed' WHERE operation_id='upload-1'"))
    db.execute(text("UPDATE education_cost_center_grants SET state='revoked' WHERE user_id=1")); db.commit()
    with pytest.raises(OdinError) as error:
        _commit_submission_graph(db, operation_id='upload-1', principal=_principal(1), cost_center_id=7,
                                 original_filename='part.3mf', final_path=None, byte_count=100, digest='x', metadata=None, compatibility_meta={})
    assert error.value.status == 404


def test_printer_edit_warns_when_requirement_loses_a_match(db):
    from modules.organizations.education_training_scope import printer_edit_warnings
    from modules.printers.models import Printer
    create(db, scope_kind='machine_type', scope_value='P1S')
    printer = db.get(Printer, 9)
    assert printer_edit_warnings(db, printer, {'machine_type': 'X1C'}) == ['Printer no longer matches training: P1S safety. Review its training scope.']
    assert not printer_edit_warnings(db, printer, {'name': 'Renamed'})
    assert not printer_edit_warnings(db, printer, {'machine_type': ' p1s '})
    db.execute(text("UPDATE printers SET machine_type='X1C'")); db.commit()
    assert s.list_trainings(db, _principal(2), 7)['items'][0]['matched_printers'] == []


def test_scheduler_does_not_hold_training_authority_lock(db):
    from modules.organizations.education_review_service import approve_submission
    from modules.organizations.education_schemas import SubmissionApproval
    from modules.organizations.education_policy import advance_schedule
    training = create(db); sign(db, training)
    approve_submission(db, submission_id=14, principal=_principal(2), body=SubmissionApproval(command_id=uuid4(), revision=1, printer_id=9))
    statements = []
    def capture(conn, cursor, sql, parameters, context, many):
        statements.append(sql)
    event.listen(db.get_bind(), 'before_cursor_execute', capture)
    try:
        assert advance_schedule(db, submission_id=14, job_id=13, printer_id=9, expected_revision=2)
    finally:
        event.remove(db.get_bind(), 'before_cursor_execute', capture)
    assert not any(sql.startswith(('UPDATE education_cost_centers', 'UPDATE groups')) for sql in statements)


def test_dispatch_reconciliation_also_names_missing_training(db):
    from modules.organizations.education_review_service import approve_submission
    from modules.organizations.education_schemas import SubmissionApproval
    from modules.organizations.education_policy import reconcile_dispatch_denial
    training = create(db); sign(db, training)
    approve_submission(db, submission_id=14, principal=_principal(2), body=SubmissionApproval(command_id=uuid4(), revision=1, printer_id=9))
    sign(db, training, 'revoke')
    assert reconcile_dispatch_denial(db, submission_id=14, job_id=13, expected_revision=2, reason='dispatch_policy_denied')
    assert 'P1S safety' in db.execute(text('SELECT notes FROM jobs WHERE id=13')).scalar_one()


def test_printer_update_response_carries_training_warning(db, monkeypatch):
    from modules.printers.routes_crud import update_printer
    from modules.printers.schemas import PrinterUpdate, PrinterResponse
    create(db, scope_kind='model', scope_value='Bambu Lab P1S')
    db.execute(text("UPDATE printers SET slot_count=4,tags='[]',timelapse_enabled=FALSE,shared=FALSE WHERE id=9")); db.commit()
    printer = update_printer(9, PrinterUpdate(model='Bambu Lab X1C'), _principal(3, 'admin'), db)
    assert 'P1S safety' in PrinterResponse.model_validate(printer).training_warnings[0]
