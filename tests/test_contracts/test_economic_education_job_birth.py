"""Licensed real HTTP Education job birth gap; no runtime authority substitution."""
import uuid
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session
from tests.test_contracts.test_education_rejected_retry_http import retry_case,submission_db,signed_license


def counts(db):
    return {name:db.execute(text('SELECT count(*) FROM '+name)).scalar_one()
            for name in ('print_files','models','jobs','education_submissions','cost_identities',
                         'cost_job_bindings','education_audit_events','education_notification_outbox')}


def test_licensed_upload_fresh_job_has_sanctioned021_org_identity(retry_case):
    db,client,auth,upload=retry_case
    token=str(uuid.uuid4())
    response=upload(token)
    status=response.status_code;assert status==201
    result=response.json()
    response=client.get('/education/submissions',headers=auth('manager'))
    status=response.status_code;assert status==200
    assert any(item['id']==result['id'] for item in response.json()['items'])
    row=db.execute(text('''SELECT j.id,j.economic_uid,j.charged_to_org_id,j.charged_to_user_id,j.submitted_by,j.order_item_id,
        s.org_id,s.job_id,s.print_file_id,s.model_id,s.submitted_by,o.state
        FROM jobs j JOIN education_submissions s ON s.job_id=j.id
        JOIN education_upload_operations o ON o.operation_id=s.operation_id WHERE s.operation_id=:token'''),{'token':token}).one()
    assert (row[0],row[2],row[3],row[4],row[5],row[6],row[7],row[10],row[11])==(result['job_id'],1,1,1,None,1,result['job_id'],1,'committed')
    uid=row[1]
    assert isinstance(uid,str) and str(uuid.UUID(uid))==uid,'Fresh licensed Education upload job lacks prospective021 economic UID'
    registry=db.execute(text('SELECT resource_kind,resource_id,owner_scope,owner_key FROM cost_identities WHERE identity_uid=:uid'),{'uid':uid}).one()
    assert tuple(registry)==('job',result['job_id'],'org',1)
    assert db.execute(text('SELECT count(*) FROM cost_job_bindings WHERE job_uid=:uid'),{'uid':uid}).scalar_one()==0
    # Standalone Education is a sanctioned job identity, not a fabricated order
    # graph or022 spool/attempt lifetime. Replay must retain the original UID.
    replay=upload(token);status=replay.status_code;assert status in (200,201)
    assert replay.json()['job_id']==result['job_id']
    assert db.execute(text('SELECT economic_uid FROM jobs WHERE id=:id'),{'id':result['job_id']}).scalar_one()==uid


def test_actual_upload_graph_failure_rolls_back_all_fresh_job_evidence(retry_case,monkeypatch):
    db,client,auth,upload=retry_case
    token=str(uuid.uuid4());before=counts(db);db.commit()
    original=Session.execute;stage=[]
    def fail_after_outbox(self,statement,*args,**kwargs):
        result=original(self,statement,*args,**kwargs)
        if 'INSERT INTO education_notification_outbox' in str(statement):
            row=original(self,text('''SELECT j.id,j.economic_uid,j.charged_to_org_id,s.org_id
                FROM jobs j JOIN education_submissions s ON s.job_id=j.id WHERE s.operation_id=:token'''),{'token':token}).one()
            stage.append(tuple(row))
            raise RuntimeError('Owned Education graph post-outbox failure')
        return result
    monkeypatch.setattr(Session,'execute',fail_after_outbox)
    with pytest.raises(RuntimeError,match='Owned Education graph post-outbox failure'):upload(token)
    assert len(stage)==1 and stage[0][2:]==(1,1)
    assert isinstance(stage[0][1],str) and str(uuid.UUID(stage[0][1]))==stage[0][1]
    db.expire_all();assert counts(db)==before
    operation=db.execute(text('SELECT state,reserved_bytes,accounted_bytes,reservation_released_at FROM education_upload_operations WHERE operation_id=:token'),{'token':token}).one()
    assert operation[0]=='aborted' and operation[1]>0 and operation[2]==0 and operation[3] is not None

    accounts=db.execute(text('SELECT scope_kind,reserved_bytes,accounted_bytes FROM education_storage_accounts WHERE org_id=1 ORDER BY scope_kind')).all()
    assert [tuple(row) for row in accounts]==[('tenant',0,0),('user',0,0)]


def test_new_creator_preserves_original_raw_insert_values_and_defaults(retry_case):
    db,client,auth,upload=retry_case
    response=upload(str(uuid.uuid4()));assert response.status_code==201
    actual=db.execute(text('SELECT * FROM jobs WHERE id=:id'),{'id':response.json()['job_id']}).mappings().one()
    # Original pre-correction INSERT is the oracle. This is deliberately an
    # unvalued fixture row, never an alternate sanctioned runtime creator.
    reference=db.execute(text('''INSERT INTO jobs
        (model_id,item_name,status,priority,quantity,submitted_by,charged_to_user_id,charged_to_org_id,
         hold,is_locked,print_file_id,payload_sha256,plate_index)
        VALUES (:model_id,:name,'submitted',3,1,:user_id,:user_id,:org_id,:hold,:is_locked,:file_id,:payload_hash,0)
        RETURNING id'''),{'model_id':actual['model_id'],'name':actual['item_name'],
                        'user_id':actual['submitted_by'],'org_id':actual['charged_to_org_id'],
                        'hold':False,'is_locked':False,'file_id':actual['print_file_id'],
                        'payload_hash':actual['payload_sha256']}).scalar_one()
    expected=db.execute(text('SELECT * FROM jobs WHERE id=:id'),{'id':reference}).mappings().one()
    exclude={'id','economic_uid','created_at','updated_at'}
    assert {k:v for k,v in actual.items() if k not in exclude}=={k:v for k,v in expected.items() if k not in exclude}
    assert actual['economic_uid'] is not None and expected['economic_uid'] is None
    for key in ('created_at','updated_at'):
        assert (actual[key] is None)==(expected[key] is None)
        if actual[key] is not None:
            from datetime import datetime
            a=datetime.fromisoformat(str(actual[key]));b=datetime.fromisoformat(str(expected[key]))
            assert abs((a-b).total_seconds())<5
    db.rollback()


@pytest.mark.parametrize('drift',['principal_org','inactive_user','archived_center','revoked_grant','operation_user'])
def test_graph_rechecks_persisted_authority_after_rename(retry_case,monkeypatch,drift):
    from modules.organizations import education_submission_service as service
    db,client,auth,upload=retry_case
    token=str(uuid.uuid4());before=counts(db);db.commit()
    original=service._commit_submission_graph
    def changed(session,**kwargs):
        if drift=='principal_org':kwargs['principal']={**kwargs['principal'],'group_id':2}
        elif drift=='inactive_user':session.execute(text('UPDATE users SET is_active=0 WHERE id=1'))
        elif drift=='archived_center':session.execute(text("UPDATE education_cost_centers SET state='archived' WHERE id=7"))
        elif drift=='revoked_grant':session.execute(text("UPDATE education_cost_center_grants SET state='revoked' WHERE user_id=1 AND role='student'"))
        else:session.execute(text('UPDATE education_upload_operations SET user_id=2 WHERE operation_id=:id'),{'id':token})
        return original(session,**kwargs)
    monkeypatch.setattr(service,'_commit_submission_graph',changed)
    response=upload(token);assert response.status_code==404
    db.expire_all();assert counts(db)==before


def test_lost_response_replays_same_sanctioned_job_identity(retry_case):
    db,client,auth,upload=retry_case;token=str(uuid.uuid4())
    with pytest.raises(ConnectionError,match='after actual server commit'):
        upload(token,lose_response=True)
    before=db.execute(text('SELECT j.id,j.economic_uid FROM jobs j JOIN education_submissions s ON s.job_id=j.id WHERE s.operation_id=:token'),{'token':token}).one()
    response=upload(token);assert response.status_code in (200,201)
    after=db.execute(text('SELECT j.id,j.economic_uid FROM jobs j JOIN education_submissions s ON s.job_id=j.id WHERE s.operation_id=:token'),{'token':token}).one()
    assert tuple(before)==tuple(after) and before[1] is not None
    assert db.execute(text('SELECT count(*) FROM cost_identities WHERE identity_uid=:uid'),{'uid':before[1]}).scalar_one()==1


def test_failure_after_sanctioned_allocation_rolls_back_identity_and_graph(retry_case,monkeypatch):
    from modules.organizations import education_submission_service as service
    db,client,auth,upload=retry_case;token=str(uuid.uuid4());before=counts(db);db.commit()
    original=service._add_created;captured=[]
    def allocation_then_fail(session,job,kind,**kwargs):
        made=original(session,job,kind,**kwargs)
        uid=session.execute(text('SELECT economic_uid FROM jobs WHERE id=:id'),{'id':job.id}).scalar_one()
        assert session.execute(text('SELECT count(*) FROM cost_identities WHERE identity_uid=:uid'),{'uid':uid}).scalar_one()==1
        captured.append(uid)
        raise RuntimeError('Owned post-allocation failure')
    monkeypatch.setattr(service,'_add_created',allocation_then_fail)
    with pytest.raises(RuntimeError,match='Owned post-allocation failure'):upload(token)
    assert len(captured)==1 and captured[0] is not None
    db.expire_all();assert counts(db)==before
    assert db.execute(text('SELECT state FROM education_upload_operations WHERE operation_id=:token'),{'token':token}).scalar_one()=='aborted'


@pytest.mark.parametrize('with_defaults',[False,True])
def test_raw_default_helper_preserves_database_values_and_sql_null(with_defaults):
    from sqlalchemy import create_engine,select
    from modules.organizations.education_submission_service import _raw_insert_job_defaults
    engine=create_engine('sqlite:///:memory:')
    try:
        with engine.begin() as c:
            extras=[" DEFAULT 2"," DEFAULT 1"," DEFAULT '[\"red\"]'"," DEFAULT 'model'"] if with_defaults else ['']*4
            c.exec_driver_sql('CREATE TABLE jobs(id INTEGER PRIMARY KEY,quantity_on_bed INTEGER'+extras[0]+',is_favorite BOOLEAN'+extras[1]+',required_tags TEXT'+extras[2]+',target_type TEXT'+extras[3]+')')
            c.exec_driver_sql('INSERT INTO jobs DEFAULT VALUES')
        with Session(engine) as session:
            expected=session.execute(text('SELECT quantity_on_bed,is_favorite,required_tags,target_type FROM jobs')).one()
            values=_raw_insert_job_defaults(session)
            selected=session.execute(select(*[values[name].label(name) for name in ('quantity_on_bed','is_favorite','required_tags','target_type')])).one()
            assert tuple(selected)==tuple(expected)
            if not with_defaults:assert tuple(selected)==(None,None,None,None)
    finally:engine.dispose()
