"""D1 functional, tenancy, assessment, privacy and physical admission contracts."""
import json
from datetime import datetime, timedelta, timezone
from uuid import uuid4
import pytest
from sqlalchemy import text
from core.errors import OdinError
from modules.organizations import education_training_service as s
from modules.organizations import education_training_policy as policy
from modules.organizations.education_training_schemas import TrainingDefinition, TrainingUpdate, QuizAttempt, SignoffCommand
from tests.test_contracts.test_education_review_workflow import review_db, _principal


@pytest.fixture
def db(review_db):
    return review_db


def definition(**changes):
    return TrainingDefinition(command_id=uuid4(), name='P1S safety', **changes)


def create(db, **changes):
    return s.save_training(db, _principal(2), definition(**changes), center_id=7)


def command(revision=1):
    return SignoffCommand(command_id=uuid4(), revision=revision)


def sign(db, training, kind='signoff'):
    return s.assess(db, _principal(2), training['id'], command(training['revision']), kind=kind, user_id=1)


def missing(db, user=1, printer=9):
    return policy.missing_training(db, org_id=1, center_id=7, user_id=user, printer_id=printer)


QUIZ = [{'prompt': 'Hot nozzle?', 'choices': ['Touch it', 'Keep clear'], 'correct': 1},
        {'prompt': 'Moving parts?', 'choices': ['Keep clear', 'Grab them'], 'correct': 0}]


def quiz(db, training, answers):
    return s.assess(db, _principal(1), training['id'], QuizAttempt(command_id=uuid4(), revision=training['revision'], answers=answers), kind='quiz')


def test_instructor_requirement_refuses_then_signs_revokes_and_audits(db):
    training = create(db, expiry_days=365)
    assert missing(db) == [{'id': training['id'], 'name': 'P1S safety', 'status': 'required'}]
    with pytest.raises(OdinError, match='P1S safety') as error:
        policy.require_printer_training(db, org_id=1, center_id=7, user_id=1, printer_id=9)
    assert error.value.code.value == 'training_required'
    assert sign(db, training)['status'] == 'complete'
    assert not missing(db)
    assert sign(db, training, 'revoke')['status'] == 'revoked'
    assert missing(db)[0]['status'] == 'revoked'
    assert sign(db, training)['status'] == 'complete'
    events = db.execute(text("SELECT action,details_json,result_json FROM education_audit_events WHERE resource_type='training'")).all()
    assert len(events) == 4
    assert any('training.revoke' in e.action and json.loads(e.details_json)['user_id'] == 1 for e in events)


@pytest.mark.parametrize('instructor_first', [False, True])
def test_quiz_and_instructor_both_required_in_either_order(db, instructor_first):
    training = create(db, quiz=QUIZ, pass_mark=75)
    if instructor_first:
        assert sign(db, training)['status'] == 'quiz_required'
    assert quiz(db, training, [0, 0])['quiz_score'] == 50
    result = quiz(db, training, [1, 0])
    assert result['status'] == ('complete' if instructor_first else 'instructor_required')
    if not instructor_first:
        assert missing(db)
        assert sign(db, training)['status'] == 'complete'
    assert not missing(db)


def test_quiz_only_no_answer_key_leak_and_roster_is_instructor_only(db):
    training = create(db, quiz=QUIZ, instructor_required=False, pass_mark=50)
    student = s.list_trainings(db, _principal(1), 7)
    assert all('correct' not in q for q in student['items'][0]['quiz'])
    assert s.list_trainings(db, _principal(2), 7)['items'][0]['quiz'][0]['correct'] == 1
    with pytest.raises(OdinError):
        s.roster(db, _principal(1), training['id'])
    assert quiz(db, training, [0, 0])['status'] == 'complete'
    rows = s.roster(db, _principal(2), training['id'])['items']
    assert rows[0]['status'] == 'complete'
    assert 'quiz_json' not in json.dumps(rows)
    assert 'correct' not in json.dumps(policy.export_training_data(db, 1))


@pytest.mark.parametrize('scope,value,expected', [('machine_type', ' p1s ', True), ('model', 'Bambu Lab P1S', True), ('model', 'other', False), ('machine_type', 'X1C', False), ('printer_group', '', True)])
def test_scope_matches_real_printer_fields(db, scope, value, expected):
    if not expected:
        with pytest.raises(OdinError, match='match at least one'):
            create(db, scope_kind=scope, scope_value=value)
        db.rollback()
    else:
        create(db, scope_kind=scope, scope_value=value)
    assert bool(missing(db)) is expected
    assert not policy.missing_training(db, org_id=2, center_id=7, user_id=1, printer_id=9)


def test_expiry_exact_boundary_and_revision_invalidate_completion(db):
    training = create(db, expiry_days=1)
    result = sign(db, training)
    row = dict(db.execute(text('SELECT * FROM education_training_records')).mappings().one())
    t = dict(db.execute(text('SELECT * FROM education_trainings')).mappings().one())
    expiry = policy.utc(row['expires_at'])
    assert policy.record_status(t, row, expiry - timedelta(microseconds=1)) == 'complete'
    assert policy.record_status(t, row, expiry) == 'expired'
    changed = s.save_training(db, _principal(2), TrainingUpdate(edit_version=1, **definition(expiry_days=2, quiz=QUIZ).model_dump(), revision=1), training_id=training['id'])
    assert missing(db)[0]['status'] == 'outdated'
    assert changed['revision'] == 2
    with pytest.raises(OdinError, match='reload_required'):
        sign(db, training)
    db.rollback()
    assert sign(db, changed)['status'] == 'quiz_required'
    assert quiz(db, changed, [1, 0])['status'] == 'complete'


def test_revocation_resets_both_factors_and_no_expiry_extension_on_repeat(db):
    training = create(db, quiz=QUIZ)
    quiz(db, training, [1, 0]); sign(db, training)
    with pytest.raises(OdinError, match='already complete'):
        sign(db, training)
    db.rollback()
    sign(db, training, 'revoke')
    assert sign(db, training)['status'] == 'quiz_required'
    assert quiz(db, training, [1, 0])['status'] == 'complete'


@pytest.mark.parametrize('principal', [_principal(1), _principal(4), _principal(5, 'admin', 2), _principal(3, 'admin', None)])
def test_manager_authority_and_tenant_isolation(db, principal):
    training = create(db)
    with pytest.raises(OdinError):
        s.save_training(db, principal, definition(), center_id=7)
    db.rollback()
    with pytest.raises(OdinError):
        s.assess(db, principal, training['id'], command(), kind='signoff', user_id=1)
    db.rollback()


def test_live_manager_revocation_inactive_student_and_cross_tenant_targets(db):
    training = create(db)
    for user in (4, 5):
        with pytest.raises(OdinError):
            s.assess(db, _principal(2), training['id'], command(), kind='signoff', user_id=user)
        db.rollback()
    db.execute(text("UPDATE education_cost_center_grants SET state='revoked' WHERE user_id=2")); db.commit()
    with pytest.raises(OdinError):
        sign(db, training)
    db.rollback()
    db.execute(text('UPDATE users SET is_active=FALSE WHERE id=1')); db.commit()
    with pytest.raises(OdinError):
        s.assess(db, _principal(3, 'admin'), training['id'], command(), kind='signoff', user_id=1)


def test_teacher_admin_bypass_only_for_submitter(db):
    create(db)
    assert not missing(db, user=2)
    assert not missing(db, user=3)
    assert missing(db, user=1)
    db.execute(text("UPDATE education_cost_center_grants SET state='revoked' WHERE user_id=2")); db.commit()
    assert missing(db, user=2)


def test_mutation_replay_is_exact_and_does_not_duplicate_history(db):
    body = definition()
    first = s.save_training(db, _principal(2), body, center_id=7)
    assert s.save_training(db, _principal(2), body, center_id=7) == first
    cmd = command()
    result = s.assess(db, _principal(2), first['id'], cmd, kind='signoff', user_id=1)
    replay = s.assess(db, _principal(2), first['id'], cmd, kind='signoff', user_id=1)
    assert replay == result
    assert db.execute(text("SELECT COUNT(*) FROM education_audit_events WHERE resource_type='training'")).scalar_one() == 2
    with pytest.raises(OdinError, match='different input'):
        s.save_training(db, _principal(2), body.model_copy(update={'name': 'Changed'}), center_id=7)


def test_submission_admission_and_approval_block_untrained_student(db):
    from modules.organizations.education_submission_service import begin_upload_attempt
    from modules.organizations.education_review_service import approve_submission, preview_submission_compatibility
    from modules.organizations.education_schemas import SubmissionApproval
    training = create(db)
    with pytest.raises(OdinError, match='Training required'):
        begin_upload_attempt(db, operation_id=str(uuid4()), principal=_principal(1), cost_center_id=7)
    db.rollback()
    with pytest.raises(OdinError, match='Training required'):
        preview_submission_compatibility(db, submission_id=14, printer_id=9, revision=1, principal=_principal(2))
    db.rollback()
    body = SubmissionApproval(command_id=uuid4(), revision=1, printer_id=9)
    with pytest.raises(OdinError, match='Training required'):
        approve_submission(db, submission_id=14, body=body, principal=_principal(2))
    db.rollback()
    sign(db, training)
    assert approve_submission(db, submission_id=14, body=body, principal=_principal(2))['status'] == 'pending'
    from modules.organizations.education_policy import scheduler_context, advance_schedule, authorize_dispatch, reserve_dispatch
    assert scheduler_context(db, job_id=13)['authorized']
    assert advance_schedule(db, submission_id=14, job_id=13, printer_id=9, expected_revision=2)
    db.commit()
    assert authorize_dispatch(db, job_id=13, printer_id=9, expected_revision=3)['authorized']
    from modules.organizations.education_policy import lock_dispatch_reservation
    reservation = reserve_dispatch(db, job_id=13, printer_id=9, expected_revision=3, extension='.3mf')
    assert reservation and lock_dispatch_reservation(db, reservation=reservation)
    db.commit()
    sign(db, training, 'revoke')
    assert not scheduler_context(db, job_id=13)['authorized']
    assert not authorize_dispatch(db, job_id=13, printer_id=9, expected_revision=3)['authorized']
    assert reserve_dispatch(db, job_id=13, printer_id=9, expected_revision=3, extension='.3mf') is None
    assert not lock_dispatch_reservation(db, reservation=reservation)


def test_multiple_requirements_all_required_archive_removes_only_one(db):
    a = create(db); b = create(db)
    sign(db, a)
    assert [r['id'] for r in missing(db)] == [b['id']]
    s.save_training(db, _principal(2), TrainingUpdate(edit_version=1, **definition(active=False).model_dump(), revision=1), training_id=b['id'])
    assert not missing(db)
    assert len(s.list_trainings(db, _principal(1), 7)['items']) == 1
    assert len(s.list_trainings(db, _principal(2), 7)['items']) == 2


def test_privacy_export_and_erasure_keep_numeric_history_revoke_evidence(db):
    training = create(db); sign(db, training)
    exported = policy.export_training_data(db, 1)
    assert len(exported['records']) == 1
    assert len(exported['events']) == 1
    assert not policy.export_training_data(db, 4)['records']
    policy.erase_training_data(db, 1, 3); db.commit()
    assert missing(db)[0]['status'] == 'revoked'
    assert db.execute(text("SELECT COUNT(*) FROM education_audit_events WHERE action='training.erase'")).scalar_one() == 1
    assert len(policy.export_training_data(db, 1)['events']) == 2


@pytest.mark.parametrize('changes', [{'instructor_required': False}, {'pass_mark': 0}, {'expiry_days': 0}, {'expiry_days': 3651}, {'scope_kind': 'model'}, {'quiz': [{'prompt': 'Q', 'choices': ['a','b'], 'correct': 2}]}])
def test_bounded_definition_validation(changes):
    with pytest.raises(ValueError):
        definition(**changes)


@pytest.mark.parametrize('answers', [[], [1], [2, 0], [-1, 0]])
def test_invalid_quiz_answers_cannot_grant_completion(db, answers):
    training = create(db, quiz=QUIZ, instructor_required=False)
    with pytest.raises(OdinError):
        quiz(db, training, answers)
    db.rollback()
    assert missing(db)


def test_concurrent_quiz_and_instructor_signoff_keep_both_factors(db):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from sqlalchemy.orm import Session
    training = create(db, quiz=QUIZ)
    barrier = Barrier(2)
    def assess(which):
        with Session(db.get_bind()) as other:
            barrier.wait(timeout=5)
            return quiz(other, training, [1, 0]) if which == 'quiz' else sign(other, training)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(assess, ['quiz', 'instructor']))
    assert 'complete' in {r['status'] for r in results}
    assert not missing(db)
    record = db.execute(text('SELECT quiz_passed_at,instructor_at,completed_at FROM education_training_records')).one()
    assert all(record)


def test_concurrent_replayed_signoff_has_single_evidence_event(db):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from sqlalchemy.orm import Session
    training = create(db)
    cmd = command()
    barrier = Barrier(2)
    def assess(_):
        with Session(db.get_bind()) as other:
            barrier.wait(timeout=5)
            return s.assess(other, _principal(2), training['id'], cmd, kind='signoff', user_id=1)
    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = list(pool.map(assess, [1, 2]))
    assert a == b
    assert db.execute(text("SELECT count(*) FROM education_audit_events WHERE action LIKE 'training.signoff:%'")).scalar_one() == 1


def test_empty_requirements_preserve_existing_admission(db):
    from modules.organizations.education_submission_service import begin_upload_attempt
    assert missing(db) == []
    assert begin_upload_attempt(db, operation_id=str(uuid4()), principal=_principal(1), cost_center_id=7) is None


def test_newly_created_training_cannot_be_skipped_at_upload_graph_commit(db):
    # A training added while a file streams must be checked again at graph birth.
    from modules.organizations.education_submission_service import _commit_submission_graph
    create(db)
    db.execute(text("UPDATE education_upload_operations SET state='renamed' WHERE operation_id='upload-1'")); db.commit()
    with pytest.raises(OdinError, match='Training required'):
        _commit_submission_graph(db, operation_id='upload-1', principal=_principal(1), cost_center_id=7,
                                 original_filename='part.3mf', final_path=None, byte_count=100, digest='x',
                                 metadata=None, compatibility_meta={})


def test_cross_tenant_raw_evidence_and_history_deletion_are_rejected(db):
    from sqlalchemy.exc import IntegrityError
    from modules.organizations.education_policy import assert_user_hard_delete_allowed, assert_org_hard_delete_allowed
    from fastapi import HTTPException
    training = create(db); sign(db, training)
    with pytest.raises(IntegrityError):
        db.execute(text('UPDATE education_training_records SET org_id=2 WHERE training_id=:id'), {'id': training['id']})
    db.rollback()
    with pytest.raises(HTTPException, match='Education history'):
        assert_user_hard_delete_allowed(db, 1)
    with pytest.raises(HTTPException, match='Education history'):
        assert_org_hard_delete_allowed(db, 1)
    with pytest.raises(IntegrityError):
        db.execute(text('DELETE FROM users WHERE id=1'))
    db.rollback()
    assert not missing(db)


def test_instructor_can_revoke_retained_evidence_after_student_leaves(db):
    training = create(db); sign(db, training)
    db.execute(text("UPDATE education_cost_center_grants SET state='revoked' WHERE user_id=1"))
    db.execute(text('UPDATE users SET is_active=FALSE WHERE id=1')); db.commit()
    assert sign(db, training, 'revoke')['status'] == 'revoked'
    rows = s.roster(db, _principal(2), training['id'])['items']
    assert rows[0]['active'] is False and rows[0]['status'] == 'revoked'
