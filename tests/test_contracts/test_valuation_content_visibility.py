"""Bounded alias closure cannot adopt foreign/shared-path/operation-only files."""
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine,text
from sqlalchemy.orm import Session
from modules.archives.valuation_content_visibility import fence_content_membership,assert_content_visible


@pytest.fixture
def db():
    engine=create_engine('sqlite://')
    with Session(engine) as db:
        for sql in [
            'CREATE TABLE jobs(id INTEGER,print_file_id INTEGER,model_id INTEGER)',
            'CREATE TABLE models(id INTEGER,print_file_id INTEGER,org_id INTEGER)',
            'CREATE TABLE print_files(id INTEGER,model_id INTEGER,stored_path TEXT,org_id INTEGER,job_id INTEGER)',
            'CREATE TABLE education_submissions(id INTEGER,job_id INTEGER,print_file_id INTEGER,model_id INTEGER,operation_id TEXT)',
            'CREATE TABLE education_upload_operations(operation_id TEXT,final_path TEXT)']:
            db.execute(text(sql))
        db.execute(text('INSERT INTO jobs VALUES(1,2,3)'))
        db.execute(text('INSERT INTO models VALUES(3,2,1)'))
        db.execute(text("INSERT INTO print_files VALUES(2,3,'file',1,1)"))
        yield db
    engine.dispose()


def visible(db):
    fence_content_membership(db)
    return assert_content_visible(db,graph={'job_id':1,'owner_scope':'org','owner_key':1},principal={'id':1,'group_id':1,'role':'admin'})


def test_complete_generic_file_model_closure(db):
    assert visible(db)==(2,)


@pytest.mark.parametrize('sql',[
    "INSERT INTO print_files VALUES(4,NULL,'file',2,NULL)",
    "INSERT INTO print_files VALUES(4,3,'other',2,NULL)",
    'UPDATE models SET org_id=2',
    'UPDATE models SET print_file_id=99'])
def test_foreign_or_missing_secondary_alias_refuses(db,sql):
    db.execute(text(sql))
    with pytest.raises(HTTPException) as error:visible(db)
    assert error.value.status_code==404


def test_alias_overflow_refuses_instead_of_truncation(db):
    for number in range(4,21):
        db.execute(text("INSERT INTO print_files VALUES(:id,3,'file',1,NULL)"),{'id':number})
    with pytest.raises(HTTPException) as error:visible(db)
    assert error.value.status_code==404


from tests.test_contracts.test_education_dispatch_policy import actual_education_dispatch
from tests.test_contracts.test_education_review_workflow import review_db


def test_actual_education_owner_visible_and_wrong_tenant_refused(actual_education_dispatch,monkeypatch):
    from core.registry import registry
    from modules.organizations.services import EducationPolicyService
    db,uploaded=actual_education_dispatch
    monkeypatch.setitem(registry._providers,'EducationPolicyProvider',EducationPolicyService())
    row=db.execute(text('SELECT submitted_by,org_id,print_file_id FROM education_submissions WHERE id=:id'),{'id':uploaded['id']}).first()
    graph={'job_id':uploaded['job_id'],'owner_scope':'org','owner_key':row.org_id}
    principal={'id':row.submitted_by,'group_id':row.org_id,'role':'viewer'}
    fence_content_membership(db)
    assert row.print_file_id in assert_content_visible(db,graph=graph,principal=principal)
    from core.errors import OdinError
    with pytest.raises(OdinError) as error:
        assert_content_visible(db,graph=graph,principal=dict(principal,group_id=row.org_id+1))
    assert error.value.status==404


@pytest.mark.parametrize('obstacle',['revoked_manager','archived_center','orphan_operation'])
def test_actual_education_visibility_changes_refuse(actual_education_dispatch,monkeypatch,obstacle):
    from core.registry import registry
    from core.errors import OdinError
    from modules.organizations.services import EducationPolicyService
    db,uploaded=actual_education_dispatch
    monkeypatch.setitem(registry._providers,'EducationPolicyProvider',EducationPolicyService())
    row=db.execute(text('SELECT org_id,cost_center_id,operation_id FROM education_submissions WHERE id=:id'),{'id':uploaded['id']}).first()
    graph={'job_id':uploaded['job_id'],'owner_scope':'org','owner_key':row.org_id}
    principal={'id':2,'group_id':row.org_id,'role':'viewer'}
    fence_content_membership(db)
    assert assert_content_visible(db,graph=graph,principal=principal)
    if obstacle=='revoked_manager':
        db.execute(text("UPDATE education_cost_center_grants SET state='revoked',revoked_by=3,revoked_at=CURRENT_TIMESTAMP WHERE user_id=2 AND role='manager' AND cost_center_id=:center"),{'center':row.cost_center_id})
    elif obstacle=='archived_center':
        db.execute(text("UPDATE education_cost_centers SET state='archived' WHERE id=:center"),{'center':row.cost_center_id})
    else:
        # Actual operation schema: a second allocated upload may share a final
        # path but has no canonical submission and must block this audience.
        db.execute(text('''INSERT INTO education_upload_operations(operation_id,org_id,user_id,state,reserved_bytes,accounted_bytes,final_path)
            SELECT :other,org_id,user_id,state,reserved_bytes,accounted_bytes,final_path FROM education_upload_operations WHERE operation_id=:operation'''),{'other':'f'*32,'operation':row.operation_id})
    with pytest.raises((OdinError,HTTPException)) as error:
        assert_content_visible(db,graph=graph,principal=principal)
    assert getattr(error.value,'status',getattr(error.value,'status_code',None))==404
