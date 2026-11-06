"""D2 contention, real HTTP privacy, bounded media and no-class regression."""
import concurrent.futures
import hashlib
import json
import uuid
import zipfile
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.orm import Session
from core.errors import OdinError
from modules.organizations.education_classes_schemas import AssignmentCreate, SubmissionLink, Assessment, AssignmentUpdate
from modules.organizations.education_assignment_submissions import link_submission, assess
from modules.organizations.education_classes_portfolio import thumbnail
from modules.organizations import education_classes_service as svc
from tests.test_contracts.test_education_classes import setup, client, attach
from tests.test_contracts.test_education_review_workflow import review_db, _principal


def another_submission(db, n=20):
    db.execute(text("INSERT INTO education_upload_operations(operation_id,org_id,user_id,state) VALUES(:op,1,1,'committed')"), {'op': str(n)})
    db.execute(text("INSERT INTO print_files(id,filename,org_id,created_by,print_time_seconds,total_weight_grams,filaments_json) VALUES(:n,'new.3mf',1,1,500,15,'[{\"type\":\"PLA\"}]')"), {'n': n})
    db.execute(text("INSERT INTO models(id,name,print_file_id,org_id) VALUES(:n,'new',:n,1)"), {'n': n})
    db.execute(text("INSERT INTO jobs(id,item_name,model_id,status,submitted_by,charged_to_user_id,charged_to_org_id) VALUES(:n,'new',:n,'submitted',1,1,1)"), {'n': n})
    db.execute(text("INSERT INTO education_submissions(id,org_id,operation_id,job_id,print_file_id,model_id,cost_center_id,submitted_by,status) VALUES(:n,1,:op,:n,:n,:n,7,1,'submitted')"), {'n': n, 'op': str(n)})
    db.commit()


def test_concurrent_pending_links_do_not_consume_approval_quota(setup):
    db, _, a = setup
    another_submission(db)
    engine = db.get_bind()
    def submit(n):
        with Session(engine) as session:
            try:
                link_submission(session, _principal(1), a['id'], SubmissionLink(submission_id=n, revision=1))
                return 'linked'
            except OdinError as error:
                session.rollback()
                return str(error)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, [14, 20]))
    assert results.count('linked') == 2
    assert db.execute(text('SELECT count(*) FROM education_assignment_submissions')).scalar_one() == 2


def test_second_assignment_cannot_relabel_history(setup):
    db, cls, a = setup
    attach(db, a)
    b = svc.save_assignment(db, _principal(2), cls['id'], AssignmentCreate(title='Other'))
    with pytest.raises(OdinError, match='another assignment'):
        link_submission(db, _principal(1), b['id'], SubmissionLink(submission_id=14, revision=2))
    db.rollback()


def test_assignment_edits_use_revision_and_preserve_assessments(setup):
    db, cls, a = setup
    attach(db, a)
    body = AssignmentUpdate(title='Updated', revision=1)
    result = svc.save_assignment(db, _principal(2), cls['id'], body, a['id'])
    assert result['revision'] == 2
    with pytest.raises(OdinError, match='reload'):
        svc.save_assignment(db, _principal(2), cls['id'], body, a['id'])
    db.rollback()
    assert db.execute(text('SELECT count(*) FROM education_assignment_submissions')).scalar_one() == 1


def test_unassigned_submission_review_does_not_require_classes(setup, tier_license):
    from modules.organizations.education_review_service import approve_submission
    from modules.organizations.education_schemas import SubmissionApproval
    db, _, _ = setup
    # Removing the D2 tier flag is a test-only seam for a deployment with D2 disabled.
    import license_manager as lm
    import copy
    original = lm.TIERS
    try:
        lm.TIERS = copy.deepcopy(original)
        lm.TIERS['education']['features'].remove('classes')
        tier_license('education', features=['education_workflows'])
        result = approve_submission(db, submission_id=14, principal=_principal(2),
            body=SubmissionApproval(revision=1, printer_id=9, command_id=uuid.uuid4()))
        assert result['status'] == 'pending'
    finally:
        lm.TIERS = original


def test_thumbnail_hash_binding_and_authorization(setup, tmp_path):
    db, _, a = setup
    path = tmp_path/'file.3mf'
    png = b'\x89PNG\r\n\x1a\nfixture'
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr('Metadata/plate_1.png', png)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    db.execute(text('UPDATE print_files SET stored_path=:p,file_hash=:h,storage_bytes=:b WHERE id=11'), {'p': str(path), 'h': digest, 'b': path.stat().st_size})
    db.execute(text("UPDATE education_upload_operations SET final_path=:p WHERE operation_id='upload-1'"), {'p': str(path)})
    db.commit()
    attach(db, a)
    assert thumbnail(db, _principal(1), 14) == png
    assert thumbnail(db, _principal(2), 14) == png
    for principal in (_principal(4), _principal(5, 'admin', 2)):
        with pytest.raises(OdinError):
            thumbnail(db, principal, 14)
    path.write_bytes(b'tampered')
    with pytest.raises(OdinError, match='unavailable'):
        thumbnail(db, _principal(1), 14)


def test_actual_gdpr_routes_include_and_erase_d2_after_downgrade(client, tier_license):
    from core.dependencies import get_current_user
    from modules.organizations.routes_sessions import router
    http, app, db, _, a = client
    attach(db, a)
    assess(db, _principal(2), a['id'], 14, Assessment(revision=1, feedback='Private feedback', grade='A', learning_status='complete'))
    app.include_router(router, prefix='/api')
    tier_license(None)
    app.dependency_overrides[get_current_user] = lambda: _principal(3, 'admin')
    exported = http.get('/api/users/1/export')
    assert exported.status_code == 200, exported.text
    assert exported.json()['education_classes']['assessments'][0]['feedback'] == 'Private feedback'
    erased = http.delete('/api/users/1/erase')
    assert erased.status_code == 200, erased.text
    assert db.execute(text('SELECT feedback,grade FROM education_assignment_submissions')).one() == ('', '')


@pytest.mark.parametrize('fields', [
    {'max_print_seconds': 0}, {'max_material_grams': float('nan')}, {'max_material_grams': float('inf')},
    {'submission_quota': 0}, {'due_at': '2026-11-20T12:00:00'}, {'allowed_materials': ['']},
    {'auto_printer_ids': [1]}, {'auto_print_seconds': 600}, {'auto_material_grams': 10},
    {'auto_printer_ids': [-1], 'auto_print_seconds': 600, 'auto_material_grams': 10},
    {'extra': 'refuse'},
])
def test_invalid_assignment_policy_refused(fields):
    with pytest.raises(ValidationError):
        AssignmentCreate(title='Assignment', **fields)


def test_d2_responses_cannot_be_cached_as_idempotent_personal_data(client):
    from core.dependencies import get_current_user
    from core.middleware.idempotency import _response_is_cacheable
    from starlette.responses import JSONResponse
    http, app, db, _, a = client
    attach(db, a)
    app.dependency_overrides[get_current_user] = lambda: _principal(2)
    response = http.put(f'/api/education/classes/assignments/{a["id"]}/submissions/14',
                        json={'revision': 1, 'learning_status': 'reviewed', 'feedback': 'Private feedback'})
    assert response.status_code == 200
    assert response.headers['cache-control'] == 'private, no-store'
    candidate = JSONResponse(response.json(), headers={'Cache-Control': response.headers['cache-control']})
    assert _response_is_cacheable(candidate, candidate.body)[0] is False


def test_assessment_cannot_repopulate_an_erased_or_inactive_student(setup):
    db, _, a = setup
    attach(db, a)
    db.execute(text('UPDATE users SET is_active=FALSE WHERE id=1')); db.commit()
    with pytest.raises(OdinError):
        assess(db, _principal(2), a['id'], 14, Assessment(revision=1, learning_status='reviewed', feedback='Private'))
    db.rollback()
    assert db.execute(text('SELECT feedback FROM education_assignment_submissions')).scalar_one() == ''


def test_review_limits_follow_the_selected_replacement_file(setup):
    from modules.organizations.education_assignment_submissions import validate_assigned_approval
    from modules.organizations.education_review_service import _reviewable_submission
    db, _, a = setup
    attach(db, a)
    another_submission(db)
    db.execute(text('UPDATE print_files SET total_weight_grams=50 WHERE id=20')); db.commit()
    row = _reviewable_submission(db, submission_id=14, principal=_principal(2))
    validate_assigned_approval(db, row)
    with pytest.raises(OdinError, match='material weight'):
        validate_assigned_approval(db, row, print_file_id=20)
    db.rollback()


def test_failed_approved_attempt_still_consumes_quota(setup):
    db, _, a = setup
    db.execute(text("UPDATE education_assignments SET auto_print_seconds=600,auto_material_grams=20,auto_printer_ids='[9]'")); db.commit()
    assert attach(db, a)['auto_approval'] == 'approved'
    db.execute(text("UPDATE education_submissions SET status='failed' WHERE id=14")); db.commit()
    another_submission(db)
    with pytest.raises(OdinError, match='quota'):
        link_submission(db, _principal(1), a['id'], SubmissionLink(submission_id=20, revision=1))
    db.rollback()
