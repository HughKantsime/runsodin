"""Affected actual PostgreSQL HTTP creators, isolated owned fixtures only."""
import os
from pathlib import Path
import secrets
import tempfile
from concurrent.futures import ThreadPoolExecutor


def main():
    with tempfile.TemporaryDirectory(prefix='economic-creator-owned-') as folder:
        root=Path(folder);root.chmod(0o700)
        credential=root/'credential';credential.write_text(secrets.token_urlsafe(48));credential.chmod(0o600)
        # Set real dialect BEFORE settings/core.db/db_compat or route imports.
        os.environ['DATABASE_URL']=os.environ['ECONOMIC_CREATOR_NATIVE_URL']
        os.environ['DATABASE_PASSWORD_FILE']=str(credential)
        os.environ['ODIN_DATA_DIR']=folder
        os.environ['JWT_SECRET_KEY']=secrets.token_urlsafe(48)
        os.environ.pop('API_KEY',None)
        from sqlalchemy import text
        from sqlalchemy.orm import Session
        from fastapi import FastAPI,Request
        from fastapi.testclient import TestClient
        from fastapi.responses import JSONResponse
        from core.db import engine,get_db,IS_POSTGRES,IS_SQLITE
        from core.db_compat import sql
        from core.schema import bootstrap_database
        from core.auth import create_access_token,hash_password
        from core.audit_actor import register_request_session,clear_session
        from core.middleware.audit_actor import AuditActorMiddleware
        from core.errors import OdinError
        from core.registry import registry
        from modules.organizations.services import EducationPolicyService
        from modules.inventory.routes import spools
        from modules.jobs.routes import jobs_crud
        from modules.inventory.models import Spool
        from modules.inventory.routes._helpers import SpoolCreate
        from core.base import SpoolStatus
        from modules.notifications import alert_dispatcher
        from core import economic_identity,economic_source_creation,audit_actor
        assert IS_POSTGRES and not IS_SQLITE and sql.is_postgres and sql.now()=='NOW()'
        assert engine.dialect.name=='postgresql'
        bootstrap_database(engine)
        with engine.begin() as c:
            c.execute(text("INSERT INTO groups(id,name,is_org) VALUES(1,'owned',TRUE),(2,'other',TRUE)"))
            c.execute(text("INSERT INTO users(id,username,password_hash,role,is_active,group_id,quota_jobs) VALUES(3,'org-admin','fixture','admin',TRUE,1,100),(70,'unassigned','fixture','operator',TRUE,NULL,100),(71,'platform','fixture','admin',TRUE,NULL,100)"))
            c.execute(text("INSERT INTO filament_library(id,brand,name,material,color_hex) VALUES(80,'Owned','Fixture','PLA','ffffff')"))
            c.execute(text("INSERT INTO printers(id,name,model,is_active,api_type,org_id) VALUES(9,'owned9','P1S',TRUE,'bambu',1),(10,'owned10','P1S',TRUE,'bambu',1)"))
        registry._providers['EducationPolicyProvider']=EducationPolicyService()
        delivered=[]
        original_delivery=alert_dispatcher.deliver_staged_alert
        alert_dispatcher.deliver_staged_alert=lambda db,plan:delivered.append(plan)
        app=FastAPI();app.add_middleware(AuditActorMiddleware)
        app.include_router(spools.router,prefix='/api');app.include_router(jobs_crud.router,prefix='/api')
        @app.exception_handler(OdinError)
        async def error(request,exc):return JSONResponse(status_code=exc.status,content=exc.to_envelope())
        def database(request:Request):
            with Session(engine,autoflush=False) as db:
                register_request_session(db,request)
                try:yield db
                finally:clear_session(db)
        app.dependency_overrides[get_db]=database
        client=TestClient(app,client=('198.51.100.7',1))
        def auth(name='org-admin'):return {'Authorization':'Bearer '+create_access_token({'sub':name})}
        def post(path,body,name='org-admin'):
            response=client.post('/api/'+path,headers=auth(name),json=body)
            status=response.status_code
            assert status in (200,201), 'Owned creator HTTP status '+str(status)
            return response.json()
        def row(statement,params=None):
            with engine.connect() as c:return c.execute(text(statement),params or {}).one()
        def scalar(statement,params=None):return row(statement,params)[0]
        def spool(id):return row('SELECT s.org_id,s.economic_birth_nonce,s.economic_uid,l.state,l.owner_scope,l.owner_key,l.resource_id,l.identity_uid FROM spools s JOIN cost_source_lifetimes l ON l.birth_nonce=s.economic_birth_nonce WHERE s.id=:id',{'id':id})
        def job(id):return row('SELECT j.charged_to_org_id,j.economic_uid,r.owner_scope,r.owner_key,r.resource_id FROM jobs j LEFT JOIN cost_identities r ON r.identity_uid=j.economic_uid WHERE j.id=:id',{'id':id})
        for name,scope,key,state in [('org-admin','org',1,'known'),('platform','global',0,'known'),('unassigned',None,None,'unresolved')]:
            id=post('spools',{'filament_id':80,'economic_birth_nonce':'retired','economic_uid':'retired'},name)['id']
            r=spool(id);assert len(r[1])==32 and r[1]!='retired' and r[3]==state and tuple(r[4:6])==(scope,key) and r[6]==id and r[2]==r[7]
            assert bool(r[2])==(state=='known')
        print('economic-creator-native-real-PG-config-org-global-unknown-birth-pass')
        # Mapped reference INSERT reproduces original route bind/default effects on real PG.
        columns='filament_id,initial_weight_g,remaining_weight_g,spool_weight_g,price,purchase_date,vendor,lot_number,storage_location,notes,pa_profile,low_stock_threshold_g,status,org_id'
        payloads=[{'filament_id':80}, {'filament_id':80,'price':None,'purchase_date':None,'notes':None},
                  {'filament_id':80,'price':0,'purchase_date':'2026-10-13T01:02:03+02:00','initial_weight_g':1234.5,'spool_weight_g':0,'low_stock_threshold_g':0}]
        for index,payload in enumerate(payloads):
            request=SpoolCreate.model_validate(payload)
            with Session(engine) as db:
                old=Spool(**request.model_dump(),qr_code='OWNED-REFERENCE-'+str(index),remaining_weight_g=request.initial_weight_g,status=SpoolStatus.ACTIVE,org_id=1)
                db.add(old);db.commit();reference=old.id
            id=post('spools',payload)['id']
            assert row('SELECT '+columns+' FROM spools WHERE id=:id',{'id':reference})==row('SELECT '+columns+' FROM spools WHERE id=:id',{'id':id})
            assert row('SELECT economic_uid,economic_birth_nonce FROM spools WHERE id=:id',{'id':reference})==(None,None)
        print('economic-creator-native-original-ORM-default-null-zero-enum-datetime-pass')
        for name,scope,key in [('org-admin','org',1),('platform','global',0),('unassigned',None,None)]:
            id=post('jobs',{'item_name':'owned '+name,'hold':True,'economic_uid':'old-id'},name)['id']
            r=job(id);assert tuple(r[2:4])==(scope,key) and bool(r[1])==(scope is not None)
            if scope is not None:assert r[4]==id
        bulk=post('jobs/bulk',[{'item_name':'owned A'},{'item_name':'owned B'}])
        batch=post('jobs/batch',{'item_name':'owned batch','printer_ids':[9,10],'queue_only':True})
        for result in bulk+batch:
            r=job(result['id']);assert r[0]==1 and r[1] and tuple(r[2:4])==('org',1)
        for owner,name,known in [(1,'org-admin',True),(2,'platform',True),(None,'org-admin',False),(None,'platform',True)]:
            with engine.begin() as c:
                original=c.execute(text("INSERT INTO jobs(item_name,status,charged_to_org_id,priority,quantity) VALUES('owned legacy','completed',:org,3,1) RETURNING id"),{'org':owner}).scalar_one()
            result=post('jobs/'+str(original)+'/repeat',{},name)
            r=job(result['new_job_id']);assert r[0]==owner and bool(r[1])==known and scalar('SELECT economic_uid FROM jobs WHERE id=:id',{'id':original}) is None
        print('economic-creator-native-single-bulk-batch-repeat-inserted-owner-no-oldcopy-pass')
        # Actual hashed token scope denial; no trusted-auth override.
        raw='odin_'+secrets.token_urlsafe(32)
        with engine.begin() as c:c.execute(text("INSERT INTO api_tokens(user_id,name,token_hash,token_prefix,scopes) VALUES(3,'owned',:h,:p,:s)"),{'h':hash_password(raw),'p':raw[:10],'s':'["read"]'})
        before=scalar('SELECT count(*) FROM cost_identities')
        response=client.post('/api/spools',headers={'X-API-Key':raw},json={'filament_id':80});status=response.status_code;assert status==403
        assert scalar('SELECT count(*) FROM cost_identities')==before
        def counts():
            return tuple(scalar('SELECT count(*) FROM '+table) for table in ('spools','jobs','cost_identities','cost_source_lifetimes','audit_logs','alerts'))+(scalar('SELECT COALESCE(sum(jobs_used),0) FROM quota_usage'),len(delivered))
        before=counts();original=economic_source_creation._execute
        def failed_birth(executor,statement,parameters=()):
            if statement.startswith('INSERT INTO cost_source_lifetimes'):raise RuntimeError('Owned native birth failure')
            return original(executor,statement,parameters)
        economic_source_creation._execute=failed_birth
        try:
            try:post('spools',{'filament_id':80})
            except RuntimeError as error:assert str(error)=='Owned native birth failure'
            else:raise AssertionError('Birth failure did not refuse')
        finally:economic_source_creation._execute=original
        assert counts()==before
        original=economic_identity._add_created;calls=[]
        def failed_second(db,resource,kind,**kwargs):
            result=original(db,resource,kind,**kwargs);calls.append(resource.item_name)
            if len(calls)==2:raise RuntimeError('Owned native later batch failure')
            return result
        economic_identity._add_created=failed_second
        try:
            try:post('jobs/bulk',[{'item_name':'rollback first'},{'item_name':'rollback second'}])
            except RuntimeError as error:assert str(error)=='Owned native later batch failure'
            else:raise AssertionError('Batch failure did not refuse')
        finally:economic_identity._add_created=original
        assert calls==['rollback first','rollback second'] and counts()==before
        original=audit_actor._erased
        def failed_actor(connection,uid):raise RuntimeError('Owned native actor failure')
        audit_actor._erased=failed_actor
        try:
            for path,body in [('spools',{'filament_id':80}),('jobs',{'item_name':'rollback audit'})]:
                try:post(path,body)
                except RuntimeError as error:assert str(error)=='Owned native actor failure'
                else:raise AssertionError('Audit failure did not refuse')
                assert counts()==before
        finally:audit_actor._erased=original
        print('economic-creator-native-helper-laterbatch-actor-atomic-rollback-pass')
        # Real same numericID on PostgreSQL requires explicit server-sequence reuse in fixture.
        id=post('spools',{'filament_id':80})['id'];old=spool(id)
        with engine.begin() as c:
            c.execute(text('DELETE FROM spools WHERE id=:id'),{'id':id})
            c.execute(text("SELECT setval(pg_get_serial_sequence('spools','id'),:id,false)"),{'id':id})
        new_id=post('spools',{'filament_id':80,'economic_uid':old[2],'economic_birth_nonce':old[1]})['id'];new=spool(new_id)
        assert new_id==id and new[1]!=old[1] and new[2]!=old[2]
        assert scalar('SELECT count(*) FROM cost_source_lifetimes WHERE resource_id=:id',{'id':id})==2
        with engine.connect() as c:
            try:c.execute(text('UPDATE spools SET economic_uid=:uid WHERE id=:id'),{'uid':old[2],'id':id})
            except Exception as error:
                assert error.orig.sqlstate=='23514' and type(error.orig).__name__=='CheckViolation'
                assert 'economic source birth differs' in str(error.orig)
                c.rollback()
            else:raise AssertionError('Retired identity resurrected')
        print('economic-creator-native-explicit-sequence-IDreuse-fresh-birth-history-pass')
        with ThreadPoolExecutor(max_workers=2) as pool:ids=list(pool.map(lambda index:post('spools',{'filament_id':80})['id'],range(2)))
        assert len(set(ids))==2 and len({spool(id)[1] for id in ids})==2 and len({spool(id)[2] for id in ids})==2
        alert_dispatcher.deliver_staged_alert=original_delivery
        engine.dispose()
        print('economic-creator-native-concurrent-distinct-creators-pass')
    print('economic-creator-native-pass')

if __name__=='__main__':main()
