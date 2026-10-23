"""Atomic platform recovery preserves retained tenant identities and authority."""
import json
import pytest
from sqlalchemy import text, create_engine, event
from sqlalchemy.orm import Session
from core.schema import bootstrap_database
from core.errors import OdinError
from modules.organizations.education_superadmin_restore import restore_superadmin


@pytest.fixture
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path/'restore.db'}")
    @event.listens_for(engine, 'connect')
    def foreign_keys(conn, _):
        conn.execute('PRAGMA foreign_keys=ON')
    bootstrap_database(engine)
    with engine.begin() as c:
        c.execute(text("INSERT INTO groups(id,name,is_org) VALUES(1,'school',1)"))
        c.execute(text("INSERT INTO users(id,username,password_hash,role,is_active,group_id) VALUES(1,'platform','fixture','admin',TRUE,NULL),(2,'stranded','fixture','admin',TRUE,1),(3,'teacher','fixture','viewer',TRUE,1)"))
        c.execute(text("INSERT INTO education_cost_centers(id,org_id,name_key,display_name,code_key,code,created_by) VALUES(1,1,'c','C','c','C',1)"))
        c.execute(text("INSERT INTO education_cost_center_grants(org_id,cost_center_id,user_id,role,state,granted_by) VALUES(1,1,2,'manager','active',1)"))
    with Session(engine) as session:
        yield session
    engine.dispose()


def actor(**changes):
    return dict({'id':1,'role':'admin','group_id':None,'is_active':True,'_auth_kind':'session_jwt'}, **changes)


def test_restore_no_platform_history_required_and_audited(db):
    result = restore_superadmin(db, actor=actor(), user_id=2, confirmation='stranded')
    assert result['status'] == 'restored'
    assert db.execute(text('SELECT group_id FROM users WHERE id=2')).scalar() is None
    assert db.execute(text('SELECT state FROM education_cost_center_grants WHERE user_id=2')).scalar() == 'revoked'
    assert db.execute(text('SELECT ended_at FROM education_memberships WHERE user_id=2')).scalar() is not None
    details = db.execute(text("SELECT details FROM audit_logs WHERE action='user.platform_superadmin_restored'")).scalar_one()
    if isinstance(details,str): details=json.loads(details)
    assert details['confirmation']=='stranded'
    assert details['before']['group_id']==1 and details['after']['group_id'] is None
    assert db.execute(text('PRAGMA foreign_key_check')).all()==[]


@pytest.mark.parametrize('changes,confirmation,status', [({'_auth_kind':'user_api_token'},'stranded',403), ({'id':3},'stranded',403), ({},'wrong',422)])
def test_refuse_unauthorized_or_wrong_confirmation(db,changes,confirmation,status):
    with pytest.raises(OdinError) as error:
        restore_superadmin(db,actor=actor(**changes),user_id=2,confirmation=confirmation)
    assert error.value.status==status
    assert db.execute(text('SELECT group_id FROM users WHERE id=2')).scalar()==1
    assert db.execute(text('SELECT state FROM education_cost_center_grants WHERE user_id=2')).scalar()=='active'


@pytest.mark.parametrize('work', ['upload','lease','storage','command'])
def test_live_work_refused_atomically(db,work):
    if work in ('upload','lease'):
        db.execute(text("INSERT INTO education_upload_operations(operation_id,org_id,user_id,state,lease_owner) VALUES('u',1,2,:state,:lease)"), {'state':'reserved' if work=='upload' else 'committed','lease': 'worker' if work=='lease' else None})
    elif work=='storage':
        db.execute(text("INSERT INTO education_storage_accounts(org_id,scope_kind,scope_id,user_id,reserved_bytes) VALUES(1,'user',2,2,10)"))
    else:
        db.execute(text("INSERT INTO education_commands(org_id,actor_kind,actor_id,action,command_id,request_hash,state) VALUES(1,'user','2','test','c','hash','pending')"))
    db.commit()
    with pytest.raises(OdinError) as error:
        restore_superadmin(db,actor=actor(),user_id=2,confirmation='stranded')
    assert error.value.status==409
    assert db.execute(text('SELECT group_id FROM users WHERE id=2')).scalar()==1
    assert db.execute(text('SELECT state FROM education_cost_center_grants WHERE user_id=2')).scalar()=='active'


def test_audit_failure_rolls_back_restore(db,monkeypatch):
    import modules.organizations.education_superadmin_restore as service
    def fail(*args,**kwargs): raise RuntimeError('synthetic audit failure')
    monkeypatch.setattr(service,'log_audit',fail)
    with pytest.raises(RuntimeError):restore_superadmin(db,actor=actor(),user_id=2,confirmation='stranded')
    assert db.execute(text('SELECT group_id FROM users WHERE id=2')).scalar()==1
    assert db.execute(text('SELECT state FROM education_cost_center_grants WHERE user_id=2')).scalar()=='active'


def test_credentials_revoked_in_same_transaction(db):
    db.execute(text("INSERT INTO active_sessions(user_id,token_jti) VALUES(2,'synthetic-session')"))
    db.execute(text("INSERT INTO api_tokens(user_id,name,token_hash,token_prefix) VALUES(2,'synthetic','synthetic-hash','synthetic')"))
    db.commit()
    restore_superadmin(db,actor=actor(),user_id=2,confirmation='stranded')
    assert db.execute(text('SELECT COUNT(*) FROM active_sessions WHERE user_id=2')).scalar()==0
    assert db.execute(text('SELECT COUNT(*) FROM api_tokens WHERE user_id=2')).scalar()==0
    assert db.execute(text("SELECT COUNT(*) FROM token_blacklist WHERE jti='synthetic-session'")).scalar()==1


from tests.test_contracts.test_education_review_workflow import review_db, _principal


def _platform_for_review(db):
    db.execute(text("INSERT INTO users(id,username,password_hash,role,is_active) VALUES(6,'platform','fixture','admin',TRUE)"))
    db.execute(text("UPDATE users SET role='admin' WHERE id=1"))
    db.commit()
    return actor(id=6)


def test_terminal_history_reads_survive_restore(review_db):
    from modules.organizations.education_review_service import list_visible_submissions
    platform=_platform_for_review(review_db)
    review_db.execute(text("UPDATE jobs SET status='completed' WHERE id=13"))
    review_db.execute(text("UPDATE education_submissions SET status='completed' WHERE id=14"))
    review_db.commit()
    before=list_visible_submissions(review_db,principal=_principal(3,role='admin'))['items']
    assert len(before)==1
    restore_superadmin(review_db,actor=platform,user_id=1,confirmation='student')
    after=list_visible_submissions(review_db,principal=_principal(3,role='admin'))['items']
    assert after==before
    assert review_db.execute(text('PRAGMA foreign_key_check')).all()==[]
    from modules.organizations.education_access import capabilities_for
    assert capabilities_for(review_db,_principal(1,role='admin',org_id=None))['education_enabled'] is False


@pytest.mark.parametrize('kind',['submitted','approved','monitor'])
def test_live_submission_or_monitor_refused(review_db,kind):
    platform=_platform_for_review(review_db)
    target=1
    if kind=='approved':
        review_db.execute(text('UPDATE education_submissions SET approved_by=3 WHERE id=14'))
        target=3
    if kind=='monitor':
        review_db.execute(text("UPDATE jobs SET status='completed' WHERE id=13"))
        review_db.execute(text("UPDATE education_submissions SET status='completed' WHERE id=14"))
        review_db.execute(text("INSERT INTO education_monitor_claims(claim_id,org_id,submission_id,job_id,printer_id,authority_revision,token_digest,state) VALUES('claim',1,14,13,9,1,'synthetic','awaiting')"))
    else:
        review_db.execute(text("UPDATE jobs SET status='pending' WHERE id=13"))
    review_db.commit()
    with pytest.raises(OdinError) as error:
        restore_superadmin(review_db,actor=platform,user_id=target,confirmation='admin' if target==3 else 'student')
    assert error.value.status==409
    assert review_db.execute(text('SELECT group_id FROM users WHERE id=:id'),{'id':target}).scalar()==1


def test_retained_identity_does_not_grant_or_allow_generic_tenant_change(db):
    from fastapi import HTTPException
    from modules.organizations.education_policy import assert_user_tenant_change_allowed
    with pytest.raises(HTTPException) as error:assert_user_tenant_change_allowed(db,2)
    assert error.value.status_code==409
    db.rollback()
    db.execute(text('UPDATE users SET group_id=NULL WHERE id=2'))
    db.commit()
    assert db.execute(text('SELECT org_id FROM education_memberships WHERE user_id=2')).scalar()==1
    from modules.organizations.education_membership import retain_membership
    with pytest.raises(OdinError):retain_membership(db,2,1)


def test_completed_idempotency_is_retained_and_not_live_work(db):
    db.execute(text("INSERT INTO education_commands(org_id,actor_kind,actor_id,action,command_id,request_hash,state,result_json,completed_at) VALUES(1,'user','2','old','done','hash','complete','{}',CURRENT_TIMESTAMP)"))
    db.commit()
    restore_superadmin(db,actor=actor(),user_id=2,confirmation='stranded')
    assert db.execute(text("SELECT state FROM education_commands WHERE command_id='done'")).scalar()=='complete'


@pytest.mark.parametrize('kind,group,status',[('session_jwt',None,200),('bearer_jwt',None,200),('user_api_token',None,403),('session_jwt',1,403)])
def test_route_enforces_interactive_platform_authority(db,kind,group,status):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from fastapi.responses import JSONResponse
    from core.db import get_db
    from core.dependencies import get_current_user
    from modules.organizations.routes_users import router
    app=FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db]=lambda: db
    app.dependency_overrides[get_current_user]=lambda: actor(_auth_kind=kind,group_id=group)
    @app.exception_handler(OdinError)
    async def error_handler(request,exc):return JSONResponse(status_code=exc.status,content=exc.to_envelope())
    with TestClient(app) as client:
        response=client.post('/users/2/restore-superadmin',json={'confirmation':'stranded'})
    assert response.status_code==status


def test_usage_history_survives_restore(db):
    from core.economic_identity import _add_created
    from modules.jobs.models import Job
    from modules.reporting.education_usage import build_education_usage_report
    _add_created(db,Job(item_name='retained usage',status='completed',submitted_by=2,charged_to_org_id=1,quantity=1,duration_hours=1),'job',owner_org_id=1)
    db.commit()
    viewer={'id':3,'role':'admin','group_id':1}
    before=build_education_usage_report(30,db,viewer)
    assert before['summary']['total_jobs']==1
    db.rollback()
    restore_superadmin(db,actor=actor(),user_id=2,confirmation='stranded')
    after=build_education_usage_report(30,db,viewer)
    assert {k:v for k,v in before.items() if k!='as_of'}=={k:v for k,v in after.items() if k!='as_of'}
