"""Retained report ownership predicates against sanctioned Education creation."""
import uuid
import pytest
from sqlalchemy import text
from tests.test_contracts.test_education_rejected_retry_http import retry_case, submission_db, signed_license


def proved(db, identifier):
    from core.report_job_ownership import proved_job_owner_sql, proved_education_job_sql
    return db.execute(text('SELECT j.id FROM jobs j WHERE j.id=:id AND ('+
                          proved_job_owner_sql()+') AND ('+proved_education_job_sql()+')'),
                      {'id':identifier}).scalar_one_or_none()


def test_sanctioned_education_job_has_readonly_report_owner(retry_case):
    db,client,auth,upload=retry_case
    response=upload(str(uuid.uuid4()));assert response.status_code==201
    identifier=response.json()['job_id']
    before=db.execute(text('SELECT economic_uid,charged_to_org_id FROM jobs WHERE id=:id'),{'id':identifier}).one()
    assert proved(db,identifier)==identifier
    assert db.execute(text('SELECT economic_uid,charged_to_org_id FROM jobs WHERE id=:id'),{'id':identifier}).one()==before


def test_legacy_job_is_not_given_report_ownership(retry_case):
    db,*_=retry_case
    identifier=db.execute(text("INSERT INTO jobs(item_name,charged_to_org_id,submitted_by) VALUES('Legacy',1,1) RETURNING id")).scalar_one()
    assert proved(db,identifier) is None
    db.rollback()


@pytest.mark.parametrize('drift',['submitter','orphan_submission'])
def test_education_marked_graph_drift_cannot_become_generic(retry_case,drift):
    db,client,auth,upload=retry_case
    response=upload(str(uuid.uuid4()));assert response.status_code==201
    identifier=response.json()['job_id'];assert proved(db,identifier)==identifier
    if drift=='submitter':
        db.execute(text('UPDATE jobs SET submitted_by=2 WHERE id=:id'),{'id':identifier})
    else:
        db.execute(text('DELETE FROM education_submissions WHERE job_id=:id'),{'id':identifier})
    assert proved(db,identifier) is None
    db.rollback()


def test_shared_path_associated_file_cannot_hide_education_marker(retry_case):
    from core.economic_identity import _add_created
    from modules.jobs.models import Job
    db,client,auth,upload=retry_case
    response=upload(str(uuid.uuid4()));assert response.status_code==201
    edu=response.json()['job_id']
    generic=_add_created(db,Job(item_name='Generic',charged_to_org_id=1,submitted_by=1),'job',owner_org_id=1)
    db.commit();assert proved(db,generic.id)==generic.id
    # A distinct associated file aliases the retained Education operation path.
    db.execute(text('''INSERT INTO print_files(filename,original_filename,stored_path,org_id,created_by,job_id)
      SELECT 'alias.3mf','alias.3mf',f.stored_path,1,1,:generic FROM print_files f
      JOIN jobs j ON j.print_file_id=f.id WHERE j.id=:edu'''),{'generic':generic.id,'edu':edu})
    assert proved(db,generic.id) is None
    assert proved(db,edu)==edu
    db.rollback()


def test_reverse_model_primary_file_marker_is_not_generic(retry_case):
    from core.economic_identity import _add_created
    from modules.jobs.models import Job
    db,client,auth,upload=retry_case
    response=upload(str(uuid.uuid4()));assert response.status_code==201
    edu=response.json()['job_id']
    file_id=db.execute(text("INSERT INTO print_files(filename,original_filename,stored_path,org_id,created_by) VALUES('reverse.3mf','reverse.3mf','/owned/reverse.3mf',1,1) RETURNING id")).scalar_one()
    generic=_add_created(db,Job(item_name='Generic reverse alias',charged_to_org_id=1,submitted_by=1,print_file_id=file_id),'job',owner_org_id=1)
    db.commit();assert proved(db,generic.id)==generic.id
    # Only the reverse model-primary relationship points to this file; the
    # submission's own file remains different and its job is the other job.
    db.execute(text('UPDATE models SET print_file_id=:file WHERE id=(SELECT model_id FROM jobs WHERE id=:edu)'),{'file':file_id,'edu':edu})
    assert proved(db,generic.id) is None
    db.rollback()


def test_edu_graph_with_additional_conflicting_associated_file_refuses(retry_case):
    db,client,auth,upload=retry_case
    first=upload(str(uuid.uuid4()));second=upload(str(uuid.uuid4()))
    assert first.status_code==second.status_code==201
    a=first.json()['job_id'];b=second.json()['job_id']
    assert proved(db,a)==a and proved(db,b)==b
    db.execute(text('UPDATE print_files SET job_id=:a WHERE id=(SELECT print_file_id FROM jobs WHERE id=:b)'),{'a':a,'b':b})
    assert proved(db,a) is None
    db.rollback()
