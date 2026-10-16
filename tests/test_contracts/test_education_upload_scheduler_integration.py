"""Real parsed submission -> approval -> scheduler regression boundary."""
import io
import json
import uuid
import zipfile
from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from tests.test_contracts.test_education_review_workflow import review_db, _principal
from tests.test_contracts.test_education_submission_upload import _sliced_3mf, _ample_disk


def _upload_and_approve(db, tmp_path, machine):
    from modules.organizations.education_submission_service import process_sliced_3mf_submission
    from modules.organizations.education_review_service import approve_submission
    from modules.organizations.education_schemas import SubmissionApproval

    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(_sliced_3mf())) as original, zipfile.ZipFile(output, 'w') as target:
        for name in original.namelist():
            data = original.read(name)
            if name == 'Metadata/project_settings.config':
                settings = json.loads(data)
                settings['printer_model'] = machine
                data = json.dumps(settings).encode()
            target.writestr(name, data)
    # Functional upload/dispatch tests own their capacity facts; separate storage tests cover real guard boundaries.
    with pytest.MonkeyPatch.context() as capacity:
        _ample_disk(capacity)
        result = process_sliced_3mf_submission(
            db, source=io.BytesIO(output.getvalue()), original_filename='pilot.3mf',
            operation_id=str(uuid.uuid4()), principal=_principal(1), cost_center_id=7,
            storage_root=tmp_path / 'uploads',
        )
    flags = db.execute(text('SELECT hold,is_locked FROM jobs WHERE id=:id'), {'id': result['job_id']}).one()
    assert tuple(flags) == (False, False)
    approved = approve_submission(db, submission_id=result['id'],
        body=SubmissionApproval(revision=result['lifecycle_revision'], printer_id=9, command_id=uuid.uuid4()),
        principal=_principal(2))
    assert approved['compatibility']['compatible']
    return result


def _schedule(db, monkeypatch):
    from modules.jobs.scheduler import Scheduler, registry
    from modules.organizations.services import EducationPolicyService
    original = registry.get_provider
    monkeypatch.setattr(registry, 'get_provider',
        lambda name: EducationPolicyService() if name == 'EducationPolicyProvider' else original(name))
    return Scheduler().run(db, start_date=datetime(2026, 10, 6, 12, tzinfo=timezone.utc))


@pytest.mark.parametrize('machine', ['Bambu Lab P1S', 'P1S', 'C12'])
def test_uploaded_job_schedules_on_approved_pin_without_default_or_alias_edits(review_db, tmp_path, monkeypatch, machine):
    uploaded = _upload_and_approve(review_db, tmp_path, machine)
    result = _schedule(review_db, monkeypatch)
    assert result.success and result.scheduled_count == 1
    assert [a.printer_id for a in result.assignments] == [9]
    states = review_db.execute(text('SELECT s.status,j.status,s.approved_printer_id,j.printer_id FROM education_submissions s JOIN jobs j ON j.id=s.job_id WHERE s.id=:id'), {'id': uploaded['id']}).one()
    assert tuple(states) == ('scheduled', 'scheduled', 9, 9)


def test_uploaded_job_rechecks_machine_drift_without_reassigning(review_db, tmp_path, monkeypatch):
    uploaded = _upload_and_approve(review_db, tmp_path, 'C12')
    # Printer 10 remains compatible; losing the pin's compatibility must not reassign.
    review_db.execute(text("UPDATE printers SET machine_type='X1C' WHERE id=9"))
    review_db.execute(text('UPDATE printers SET nozzle_diameter=0.4 WHERE id=10'))
    review_db.commit()
    result = _schedule(review_db, monkeypatch)
    assert result.scheduled_count == 0
    row = review_db.execute(text('SELECT status,approved_printer_id FROM education_submissions WHERE id=:id'), {'id': uploaded['id']}).one()
    assert tuple(row) == ('submitted', None)


def test_explicit_hold_still_blocks_uploaded_job(review_db, tmp_path, monkeypatch):
    uploaded = _upload_and_approve(review_db, tmp_path, 'C12')
    review_db.execute(text('UPDATE jobs SET hold=:held WHERE id=:id'), {'held': True, 'id': uploaded['job_id']})
    review_db.commit()
    assert _schedule(review_db, monkeypatch).scheduled_count == 0
    assert review_db.execute(text('SELECT status FROM jobs WHERE id=:id'), {'id': uploaded['job_id']}).scalar_one() == 'pending'


def _generic_c12_job(review_db):
    """A non-Education, model-bound job whose library file was sliced for C12 (Bambu Studio: P1S)."""
    review_db.execute(text("INSERT INTO models(id,name,org_id,build_time_hours) VALUES(960,'Generic C12 part',1,1)"))
    review_db.execute(text("INSERT INTO print_files(filename,model_id,printer_model,org_id) VALUES('generic.3mf',960,'C12',1)"))
    review_db.execute(text("INSERT INTO jobs(id,item_name,status,charged_to_org_id,charged_to_user_id,quantity,duration_hours,"
                           "hold,priority,model_id) VALUES(960,'Generic C12 part','pending',1,1,1,1,FALSE,3,960)"))
    review_db.commit()
    return 960


def test_non_education_job_schedules_on_matching_model_through_model_table(review_db, tmp_path, monkeypatch):
    """Generic C12 job lands on a P1S (previously asserted 0 scheduled: 'C12' was compared verbatim to 'P1S')."""
    job = _generic_c12_job(review_db)
    result = _schedule(review_db, monkeypatch)
    assert result.scheduled_count == 1
    status, printer = review_db.execute(text('SELECT status,printer_id FROM jobs WHERE id=:id'), {'id': job}).one()
    assert status == 'scheduled' and printer in (9, 10)  # both fixture printers are P1S


def test_non_education_job_refuses_a_different_model(review_db, tmp_path, monkeypatch):
    job = _generic_c12_job(review_db)
    review_db.execute(text("UPDATE printers SET model='Bambu Lab X1 Carbon', machine_type='X1C'"))
    review_db.commit()
    assert _schedule(review_db, monkeypatch).scheduled_count == 0
    assert review_db.execute(text('SELECT status FROM jobs WHERE id=:id'), {'id': job}).scalar_one() == 'pending'


def test_dispatch_loads_hash_of_the_actual_uploaded_submission(review_db, tmp_path, monkeypatch):
    import hashlib
    from core import db as core_db
    from modules.printers import dispatch

    uploaded = _upload_and_approve(review_db, tmp_path, 'P1S')
    monkeypatch.setattr(core_db, 'engine', review_db.get_bind())
    job = dispatch._load_job(uploaded['job_id'])
    assert job is not None
    assert job['education_submission_id'] == uploaded['id']
    with open(job['stored_path'], 'rb') as payload:
        assert job['file_hash'] == hashlib.sha256(payload.read()).hexdigest()
