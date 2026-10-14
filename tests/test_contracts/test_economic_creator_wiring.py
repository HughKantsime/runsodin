"""Actual creator HTTP/source transactions; fixtures only, no callback/COGS claim."""
import json
import secrets
import pytest
from fastapi import FastAPI,Request
from fastapi.testclient import TestClient
from sqlalchemy import text,event
from sqlalchemy.orm import Session
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_license_user_limits import signed_license

@pytest.fixture
def creators(review_db,monkeypatch):
    from core.db import get_db
    from core.auth import create_access_token,hash_password
    from core.audit_actor import register_request_session,clear_session
    from core.middleware.audit_actor import AuditActorMiddleware
    from modules.inventory.routes.spools import router as spools
    from modules.jobs.routes.jobs_crud import router as jobs
    from core.registry import registry
    from modules.organizations.services import EducationPolicyService
    from core.errors import OdinError
    from fastapi.responses import JSONResponse
    monkeypatch.setitem(registry._providers,'EducationPolicyProvider',EducationPolicyService())
    @event.listens_for(review_db.get_bind(),'connect')
    def foreign_keys(connection,record):connection.execute('PRAGMA foreign_keys=ON')
    review_db.execute(text("INSERT INTO filament_library(id,brand,name,material,color_hex) VALUES(80,'Owned','Fixture','PLA','ffffff')"))
    review_db.execute(text("INSERT INTO users(id,username,password_hash,role,is_active,group_id,quota_jobs) VALUES(70,'unassigned','fixture','operator',1,NULL,100),(71,'platform','fixture','admin',1,NULL,100)"))
    review_db.execute(text('UPDATE users SET quota_jobs=100 WHERE id=3'));review_db.commit()
    delivered=[]
    def delivery(db,plan):
        # Suppress external transport only; real alert/quota/job staging is retained.
        with Session(review_db.get_bind()) as visible:
            assert visible.execute(text('SELECT count(*) FROM jobs WHERE id>13')).scalar_one()>0
        delivered.append(plan)
    monkeypatch.setattr('modules.notifications.alert_dispatcher.deliver_staged_alert',delivery)
    app=FastAPI()
    from core.middleware.dry_run import dry_run_middleware
    app.middleware('http')(dry_run_middleware)
    app.add_middleware(AuditActorMiddleware);app.include_router(spools,prefix='/api');app.include_router(jobs,prefix='/api');app.include_router(jobs,prefix='/api/v1')
    def database(request:Request):
        with Session(review_db.get_bind(),autoflush=False) as db:
            register_request_session(db,request)
            try:yield db
            finally:clear_session(db)
    @app.exception_handler(OdinError)
    async def error(request,exc):return JSONResponse(status_code=exc.status,content=exc.to_envelope())
    app.dependency_overrides[get_db]=database
    def auth(name='admin'):return {'Authorization':'Bearer '+create_access_token({'sub':name})}
    def token(scopes):
        raw='odin_'+secrets.token_urlsafe(32)
        review_db.execute(text("INSERT INTO api_tokens(user_id,name,token_hash,token_prefix,scopes) VALUES(3,'owned-creator',:h,:p,:s)"),{'h':hash_password(raw),'p':raw[:10],'s':json.dumps(scopes)});review_db.commit()
        return {'X-API-Key':raw}
    return TestClient(app,client=('198.51.100.7',1)),auth,token,delivered

def fresh_spool(db,id):
    return db.execute(text('SELECT s.org_id,s.economic_birth_nonce,s.economic_uid,l.state,l.resource_id,l.identity_uid,l.owner_scope,l.owner_key FROM spools s JOIN cost_source_lifetimes l ON l.birth_nonce=s.economic_birth_nonce WHERE s.id=:id'),{'id':id}).one()

def job_identity(db,id):
    return db.execute(text('SELECT j.charged_to_org_id,j.economic_uid,r.owner_scope,r.owner_key,r.resource_kind,r.resource_id FROM jobs j LEFT JOIN cost_identities r ON r.identity_uid=j.economic_uid WHERE j.id=:id'),{'id':id}).one()

@pytest.mark.parametrize('name,scope,key,state',[('admin','org',1,'known'),('platform','global',0,'known'),('unassigned',None,None,'unresolved')])
def test_api_spool_fresh_birth_exact_owner(creators,review_db,name,scope,key,state):
    client,auth,_,_=creators
    response=client.post('/api/spools',headers=auth(name),json={'filament_id':80,'economic_uid':'not-adopted','economic_birth_nonce':'not-adopted','global_authority':True})
    status=response.status_code;assert status==200
    id=response.json()['id'];review_db.expire_all();row=fresh_spool(review_db,id)
    assert len(row[1])==32 and row[1]!='not-adopted' and row[3]==state and row[4]==id
    assert row[2]==row[5] and tuple(row[6:])==(scope,key)
    if state=='known':assert row[2]
    else:assert row[2] is None
    assert review_db.execute(text('SELECT economic_uid,economic_birth_nonce FROM spools WHERE id=:id'),{'id':id}).one()==(row[2],row[1])

@pytest.mark.parametrize('payload',[
    {'filament_id':80},
    {'filament_id':80,'price':0,'purchase_date':'2026-10-13T01:02:03+02:00','initial_weight_g':1234.5,'spool_weight_g':0,'low_stock_threshold_g':0,'notes':'owned'},
    {'filament_id':80,'price':None,'purchase_date':None,'vendor':None,'lot_number':None},
])
def test_spool_effective_defaults_and_datetime_match_original_orm(creators,review_db,payload):
    from modules.inventory.routes._helpers import SpoolCreate
    from modules.inventory.models import Spool
    from core.base import SpoolStatus
    request=SpoolCreate.model_validate(payload)
    # Frozen mapped INSERT reproduces the original route's effective bind/default semantics.
    original=Spool(**request.model_dump(exclude={"purchase_evidence"}),qr_code='OWNED-ORM-REFERENCE',remaining_weight_g=request.initial_weight_g,status=SpoolStatus.ACTIVE,org_id=1)
    review_db.add(original);review_db.commit();reference=original.id
    client,auth,_,_=creators
    response=client.post('/api/spools',headers=auth(),json=payload);status=response.status_code;assert status==200
    fields='filament_id,initial_weight_g,remaining_weight_g,spool_weight_g,price,purchase_date,vendor,lot_number,storage_location,notes,pa_profile,low_stock_threshold_g,status,org_id'
    review_db.expire_all()
    expected=review_db.execute(text('SELECT '+fields+' FROM spools WHERE id=:id'),{'id':reference}).one()
    actual=review_db.execute(text('SELECT '+fields+' FROM spools WHERE id=:id'),{'id':response.json()['id']}).one()
    assert actual==expected
    assert review_db.execute(text('SELECT economic_uid,economic_birth_nonce FROM spools WHERE id=:id'),{'id':reference}).one()==(None,None)

@pytest.mark.parametrize('path,body',[
    ('/api/jobs',{'item_name':'owned single','hold':True,'due_date':None}),
    ('/api/jobs/bulk',[{'item_name':'owned bulk A'},{'item_name':'owned bulk B'}]),
    ('/api/jobs/batch',{'item_name':'owned batch','printer_ids':[9,10],'queue_only':True}),
])
def test_generic_creators_prospective_ids_and_exact_billing(creators,review_db,path,body):
    client,auth,_,_=creators
    response=client.post(path,headers=auth(),json=body);status=response.status_code;assert status in (200,201)
    review_db.expire_all();rows=review_db.execute(text('SELECT id FROM jobs WHERE id>13 ORDER BY id')).scalars().all()
    assert len(rows)==(1 if path=='/api/jobs' else 2)
    for id in rows:
        row=job_identity(review_db,id)
        assert row[0]==1 and row[1] and tuple(row[2:])==('org',1,'job',id)
        assert review_db.execute(text('SELECT count(*) FROM cost_job_bindings WHERE job_uid=:uid'),{'uid':row[1]}).scalar_one()==0
    assert len({job_identity(review_db,id)[1] for id in rows})==len(rows)
    assert review_db.execute(text('SELECT jobs_used FROM quota_usage WHERE user_id=3')).scalar_one()==len(rows)

@pytest.mark.parametrize('name,scope,key',[('platform','global',0),('unassigned',None,None)])
def test_generic_null_owner_not_inferred_global(creators,review_db,name,scope,key):
    client,auth,_,_=creators
    response=client.post('/api/jobs',headers=auth(name),json={'item_name':'owned null','economic_uid':'retired-id'});status=response.status_code;assert status==201
    id=response.json()['id'];review_db.expire_all();row=job_identity(review_db,id)
    assert row[0] is None and tuple(row[2:4])==(scope,key)
    assert bool(row[1])==(scope=='global')

@pytest.mark.parametrize('owner,name,known',[(1,'admin',True),(2,'platform',True),(None,'admin',False),(None,'platform',True)])
def test_repeat_uses_copied_billed_owner_and_new_uid_only(creators,review_db,owner,name,known):
    client,auth,_,_=creators
    review_db.execute(text("INSERT INTO jobs(id,item_name,status,quantity,priority,charged_to_org_id) VALUES(90,'owned repeat','completed',1,3,:owner)"),{'owner':owner});review_db.commit()
    response=client.post('/api/jobs/90/repeat',headers=auth(name));status=response.status_code;assert status==200
    review_db.expire_all();id=review_db.execute(text('SELECT max(id) FROM jobs')).scalar_one();row=job_identity(review_db,id)
    assert row[0]==owner and bool(row[1])==known
    if known:assert tuple(row[2:4])==(('global',0) if owner is None else ('org',owner))
    assert review_db.execute(text('SELECT economic_uid FROM jobs WHERE id=90')).scalar_one() is None

@pytest.mark.parametrize('path,body,status',[
    ('/api/spools',{'filament_id':80},403),
    ('/api/jobs/bulk',[{'item_name':'unauthorized'}],403),
    ('/api/jobs',{'item_name':'edu blocked','model_id':12},409),
    ('/api/jobs/bulk',[{'item_name':'ok'},{'item_name':'edu blocked','model_id':12}],409),
])
def test_denials_leave_no_prospective_sources(creators,review_db,path,body,status):
    client,auth,_,_=creators
    name='student' if status==403 else 'admin'
    before=review_db.execute(text('SELECT count(*) FROM cost_identities')).scalar_one()
    response=client.post(path,headers=auth(name),json=body);actual=response.status_code;assert actual==status
    review_db.expire_all();assert review_db.execute(text('SELECT count(*) FROM cost_identities')).scalar_one()==before
    assert review_db.execute(text('SELECT count(*) FROM cost_source_lifetimes')).scalar_one()==0
    assert review_db.execute(text('SELECT count(*) FROM jobs WHERE id>13')).scalar_one()==0
    assert review_db.execute(text('SELECT count(*) FROM quota_usage')).scalar_one()==0

@pytest.mark.parametrize('path,body',[
    ('/api/spools',{'filament_id':80}),('/api/jobs',{'item_name':'scoped'}),
])
def test_actual_scoped_read_token_does_not_create(creators,review_db,path,body):
    client,_,token,_=creators;headers=token(['read'])
    response=client.post(path,headers=headers,json=body);status=response.status_code;assert status==403
    assert review_db.execute(text('SELECT count(*) FROM cost_identities')).scalar_one()==0

def test_spool_helper_failure_rolls_back_inventory_and_audit(creators,review_db,monkeypatch):
    from core import economic_source_creation as source
    original=source._execute
    def fail(executor,statement,parameters=()):
        if statement.startswith('INSERT INTO cost_source_lifetimes'):raise RuntimeError('Owned birth failure')
        return original(executor,statement,parameters)
    monkeypatch.setattr(source,'_execute',fail)
    client,auth,_,_=creators
    with pytest.raises(RuntimeError,match='Owned birth failure'):
        client.post('/api/spools',headers=auth(),json={'filament_id':80})
    review_db.expire_all()
    for table in ('spools','cost_source_lifetimes','cost_identities','audit_logs'):
        assert review_db.execute(text('SELECT count(*) FROM '+table)).scalar_one()==0

def test_later_bulk_factory_failure_rolls_back_every_job_quota_alert(creators,review_db,monkeypatch):
    from core import economic_identity
    original=economic_identity._add_created;calls=[]
    def fail(db,resource,kind,**kwargs):
        calls.append(resource.item_name)
        result=original(db,resource,kind,**kwargs)
        if len(calls)==2:raise RuntimeError('Owned second creator failure')
        return result
    monkeypatch.setattr(economic_identity,'_add_created',fail)
    client,auth,_,delivered=creators
    with pytest.raises(RuntimeError,match='Owned second creator failure'):
        client.post('/api/jobs/bulk',headers=auth(),json=[{'item_name':'first'},{'item_name':'second'}])
    review_db.expire_all()
    assert calls==['first','second'] and not delivered
    assert review_db.execute(text('SELECT count(*) FROM jobs WHERE id>13')).scalar_one()==0
    for table in ('quota_usage','cost_identities','audit_logs','alerts'):
        assert review_db.execute(text('SELECT count(*) FROM '+table)).scalar_one()==0


@pytest.mark.parametrize('path,body',[('/api/spools',{'filament_id':80}),('/api/jobs',{'item_name':'audit rollback'})])
def test_creator_audit_failure_rolls_back_complete_source_and_quota(creators,review_db,monkeypatch,path,body):
    def fail(connection,uid):raise RuntimeError('Owned creator audit refusal')
    monkeypatch.setattr('core.audit_actor._erased',fail)
    client,auth,_,delivered=creators
    with pytest.raises(RuntimeError,match='Owned creator audit refusal'):
        client.post(path,headers=auth(),json=body)
    review_db.expire_all()
    assert not delivered
    assert review_db.execute(text('SELECT count(*) FROM jobs WHERE id>13')).scalar_one()==0
    for table in ('spools','cost_source_lifetimes','cost_identities','quota_usage','audit_logs','alerts'):
        assert review_db.execute(text('SELECT count(*) FROM '+table)).scalar_one()==0

def test_dryrun_preserves_no_new_job_or_identity(creators,review_db):
    client,auth,_,delivered=creators
    headers=auth();headers['X-Dry-Run']='true'
    response=client.post('/api/v1/jobs',headers=headers,json={'item_name':'owned dry run'});status=response.status_code;assert status==201
    assert response.json()['dry_run'] is True
    review_db.expire_all();assert not delivered
    for table in ('cost_identities','quota_usage','audit_logs','alerts'):
        assert review_db.execute(text('SELECT count(*) FROM '+table)).scalar_one()==0
    assert review_db.execute(text('SELECT count(*) FROM jobs WHERE id>13')).scalar_one()==0

def test_actual_viewer_approval_creation_keeps_status_and_known_owner(creators,review_db,signed_license):
    signed_license(tier='education',features=[])
    review_db.execute(text("INSERT INTO system_config(key,value) VALUES('require_job_approval','true')"));review_db.commit()
    client,auth,_,_=creators
    response=client.post('/api/jobs',headers=auth('student'),json={'item_name':'owned approval','hold':True});status=response.status_code;assert status==201
    result=response.json();assert result['status']=='submitted' and result['hold'] is True
    assert review_db.execute(text('SELECT submitted_by FROM jobs WHERE id=:id'),{'id':result['id']}).scalar_one()==1
    review_db.expire_all();row=job_identity(review_db,result['id']);assert row[0]==1 and row[1] and tuple(row[2:4])==('org',1)

def test_source_delete_numeric_reuse_keeps_retired_birth_history(creators,review_db):
    client,auth,_,_=creators
    response=client.post('/api/spools',headers=auth(),json={'filament_id':80});status=response.status_code;assert status==200
    oldid=response.json()['id'];review_db.expire_all();old=fresh_spool(review_db,oldid)
    review_db.execute(text('DELETE FROM spools WHERE id=:id'),{'id':oldid});review_db.commit()
    response=client.post('/api/spools',headers=auth(),json={'filament_id':80,'economic_uid':old[2],'economic_birth_nonce':old[1]});status=response.status_code;assert status==200
    newid=response.json()['id'];review_db.expire_all();new=fresh_spool(review_db,newid)
    assert newid==oldid and new[1]!=old[1] and new[2]!=old[2]
    assert review_db.execute(text('SELECT count(*) FROM cost_source_lifetimes WHERE resource_id=:id'),{'id':oldid}).scalar_one()==2
    with pytest.raises(Exception):
        review_db.execute(text('UPDATE spools SET economic_uid=:uid WHERE id=:id'),{'uid':old[2],'id':newid})
    review_db.rollback()
