"""Owned actual PostgreSQL final retirement COMMIT/replay controls."""
import os,secrets,stat,tempfile,subprocess,json,re,traceback
from pathlib import Path
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch
PHASE='bootstrap'

def main():
    global PHASE
    private=Path(os.environ['RETIREMENT_NATIVE_PASSWORD_FILE'])
    assert os.getuid()==10001 and stat.S_IMODE(private.stat().st_mode)==0o600
    with tempfile.TemporaryDirectory(prefix='owned-retirement-commit-') as folder:
        os.environ.update(DATABASE_URL='postgresql+psycopg://postgres@pg/postgres',DATABASE_PASSWORD_FILE=str(private),ODIN_DATA_DIR=folder,JWT_SECRET_KEY=secrets.token_urlsafe(48))
        from sqlalchemy import text,event
        from sqlalchemy.exc import DBAPIError
        from sqlalchemy.orm import Session
        from core.db import engine
        from core.db_compat import IS_POSTGRES
        from core.schema import bootstrap_database
        from core.models import AuditLog
        from core.audit_actor import RequestAuditScope
        from core.auth import create_access_token
        from starlette.requests import Request
        from modules.archives import timelapse_capture as capture,timelapse_lifetime_action as service,timelapse_lifetime_prepare as preparation
        from modules.archives.timelapse_action_authority import capture_action_credential
        from modules.archives.timelapse_lifetime_prepare import prepare_request_lifetime_retirement
        from core.timelapse_request_budget import VerificationBudget
        from core import timelapse_action_fs as filesystem
        from core.database_config import create_database_engine
        import psycopg
        from psycopg import sql
        assert engine.dialect.name=='postgresql' and IS_POSTGRES
        bootstrap_database(engine,Path('/source/backend'))
        media=Path(folder)/'media';media.mkdir(mode=0o700);capture.TIMELAPSE_DIR=media;capture.SessionLocal=lambda:Session(engine)
        with engine.begin() as c:
            c.exec_driver_sql("INSERT INTO groups(id,name,is_org) VALUES(1,'Owned commit fixture',true)")
            c.exec_driver_sql("INSERT INTO users(id,username,password_hash,role,is_active,group_id) VALUES(3,'commit-admin','fixture','admin',true,1)")
            c.exec_driver_sql("INSERT INTO print_files(id,filename,original_filename,stored_path,org_id) VALUES(111,'plain.3mf','plain.3mf','/owned/plain.3mf',1)")
        token=create_access_token({'sub':'commit-admin','role':'admin'})
        request=Request({'type':'http','headers':[(b'authorization',('Bearer '+token).encode())],'query_string':b'','odin.private.audit_actor_scope':RequestAuditScope(None)})
        with Session(engine) as db:evidence=capture_action_credential(request,db,token,kind='bearer_jwt',carrier='bearer',purpose='retire')
        jpeg=subprocess.run(['/usr/bin/ffmpeg','-nostdin','-v','error','-f','lavfi','-i','color=c=gray:s=64x64','-frames:v','1','-f','image2pipe','-vcodec','mjpeg','-'],capture_output=True,check=True).stdout
        @contextmanager
        def stream(*args,**kwargs):yield SimpleNamespace(status_code=200,headers={},iter_raw=lambda **kwargs:[jpeg])
        def metadata():
            with engine.connect() as c:
                return tuple(tuple(tuple(row) for row in c.exec_driver_sql('SELECT * FROM '+table+' ORDER BY 1,2')) for table in ('timelapse_frame_namespaces','timelapse_retained_versions','timelapse_retirement_container_receipts'))
        actual_sharing=preparation._preparation_sharing
        collision_reports=[]
        def observed_sharing(db,inv,tid,**kwargs):
            links,expected=preparation._links(inv)
            selected={}
            for path in links:
                value=expected[path];physical=value.get('fact') or value.get('space') or {}
                if not value.get('absent') and physical.get('inode') is not None:
                    selected.setdefault((physical['device'],physical['inode']),set()).add(value['kind'])
            rows=db.execute(text("SELECT f.role,f.device,f.inode,p.result_lifetime,p.phase,p.state,p.result_tid,p.result_nonce FROM timelapse_publication_facts f JOIN timelapse_publications p ON p.operation_id=f.operation_id WHERE p.result_lifetime<>:life AND f.role IN ('output','private_creation','private_root')"),{'life':inv['lifetime']}).mappings().all()
            proof=[]
            for row in rows:
                kinds=selected.get((row['device'],row['inode']))
                if not kinds:continue
                retirements=db.execute(text('SELECT n.operation_id,n.phase,i.state FROM timelapse_retirement_lifetime_bindings n JOIN timelapse_retirements i ON i.operation_id=n.operation_id WHERE n.lifetime_nonce=:life'),{'life':row['result_lifetime']}).mappings().all()
                states=[]
                for retirement in retirements:
                    objects=db.execute(text('SELECT kind,state FROM timelapse_retirement_objects WHERE operation_id=:op AND device=:dev AND inode=:ino'),{'op':retirement['operation_id'],'dev':row['device'],'ino':row['inode']}).all()
                    remaining=db.execute(text("SELECT count(*) FROM timelapse_retirement_objects WHERE operation_id=:op AND state NOT IN ('retired','originally_absent')"),{'op':retirement['operation_id']}).scalar_one()
                    states.append({'binding_phase':retirement['phase'],'retirement_state':retirement['state'],'remaining_objects':remaining,'matching_objects':[{'kind':obj[0],'state':obj[1]} for obj in objects]})
                live=db.execute(text('SELECT count(*) FROM timelapses WHERE id=:id AND ownership_operation_id=:nonce'),{'id':row['result_tid'],'nonce':row['result_nonce']}).scalar_one()
                proof.append({'fact_role':row['role'],'current_kinds':sorted(kinds),'publication_phase':row['phase'],'publication_state':row['state'],'exact_live_rows':live,'retirements':states})
            report={'matching_fact_count':len(proof),'matches':proof}
            collision_reports.append(report);print('retirement-final-commit-collision-proof '+json.dumps(report,sort_keys=True),flush=True)
            return actual_sharing(db,inv,tid,**kwargs)
        combinations=list((p,b) for p in (False,True) for b in ('deferred','disconnect','lost_ack'))
        if os.environ.get('RETIREMENT_FINAL_DIAGNOSTIC')=='collision':combinations=[(True,'deferred'),(True,'disconnect')]
        if os.environ.get('RETIREMENT_FINAL_DIAGNOSTIC')=='sequential':combinations=[(True,'deferred' if index%2==0 else 'disconnect') for index in range(8)]
        administration_engine=engine
        isolated=os.environ.get('RETIREMENT_FINAL_DIAGNOSTIC','0')=='0'
        for index,(physical,boundary) in enumerate(combinations):
            case_database=None
            if isolated:
                PHASE='case_database'
                case_database='owned_retirement_case_'+str(index)
                with psycopg.connect(host='pg',dbname='postgres',user='postgres',password=private.read_text(),autocommit=True) as admin:
                    admin.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(case_database)))
                engine=create_database_engine('postgresql+psycopg://postgres@pg/'+case_database,role='timelapse',password_file=str(private))
                bootstrap_database(engine,Path('/source/backend'))
                case_root=Path(folder)/('case_'+str(index));case_root.mkdir(mode=0o700)
                media=case_root/'media';media.mkdir(mode=0o700)
                capture.TIMELAPSE_DIR=media;capture.SessionLocal=lambda:Session(engine)
                with engine.begin() as c:
                    c.exec_driver_sql("INSERT INTO groups(id,name,is_org) VALUES(1,'Owned commit fixture',true)")
                    c.exec_driver_sql("INSERT INTO users(id,username,password_hash,role,is_active,group_id) VALUES(3,'commit-admin','fixture','admin',true,1)")
                    c.exec_driver_sql("INSERT INTO print_files(id,filename,original_filename,stored_path,org_id) VALUES(111,'plain.3mf','plain.3mf','/owned/plain.3mf',1)")
                with Session(engine) as db:evidence=capture_action_credential(request,db,token,kind='bearer_jwt',carrier='bearer',purpose='retire')
                assert evidence.database is engine
            printer=77+index;job=444+index
            PHASE='seed_source'
            with engine.begin() as c:
                name='Owned commit fixture '+str(index)
                assert c.execute(text('SELECT count(*) FROM printers WHERE id=:id OR name=:name'),{'id':printer,'name':name}).scalar_one()==0
                c.execute(text("INSERT INTO printers(id,name,org_id,shared,timelapse_enabled,is_active) VALUES(:id,:name,1,false,true,true)"),{'id':printer,'name':name})
                c.execute(text("INSERT INTO jobs(id,item_name,printer_id,print_file_id,charged_to_org_id,status) VALUES(:id,'Owned commit fixture',:printer,111,1,'printing')"),{'id':job,'printer':printer})
            PHASE='capture_birth'
            with Session(engine) as db:ticket=capture.get_or_create_timelapse(db,printer,job,return_ticket=True)
            if os.environ.get('RETIREMENT_FINAL_DIAGNOSTIC')=='1' and index==1:
                print('retirement-final-commit-diagnostic-complete',flush=True);return
            PHASE='capture_encode'
            if physical:
                with patch.object(capture.httpx,'stream',stream):
                    for _ in range(2):assert capture.capture_frame(printer,ticket.timelapse_id,nonce=ticket.nonce,job_id=job)
            with engine.begin() as c:c.execute(text("UPDATE jobs SET status='completed' WHERE id=:id"),{'id':job})
            assert capture.encode_timelapse(ticket.timelapse_id,printer_id=printer,nonce=ticket.nonce,job_id=job) is physical
            with engine.connect() as c:nonce=c.execute(text('SELECT ownership_operation_id FROM timelapses WHERE id=:id'),{'id':ticket.timelapse_id}).scalar_one()
            PHASE='prepare_retirement'
            observation_start=len(collision_reports)
            try:
                with patch.object(preparation,'_preparation_sharing',observed_sharing):
                    prepare_request_lifetime_retirement(engine,str(media),ticket.timelapse_id,evidence,expected_nonce=nonce,budget=VerificationBudget())
            except service.RetirementUnavailable:
                if os.environ.get('RETIREMENT_FINAL_DIAGNOSTIC')!='collision':raise
                assert collision_reports and collision_reports[-1]['matching_fact_count']>0
                print('retirement-final-commit-diagnostic-complete',flush=True);return
            before=metadata();calls=[];fired=[];received=[];cleanup_start=[];original_drivers=[]
            actual_commit=Session.commit;actual_rename=filesystem.rename_noreplace;actual_unlink=os.unlink;actual_rmdir=os.rmdir
            def rename(*a,**kw):calls.append('rename');return actual_rename(*a,**kw)
            def unlink(*a,**kw):calls.append('unlink');return actual_unlink(*a,**kw)
            def rmdir(*a,**kw):calls.append('rmdir');return actual_rmdir(*a,**kw)
            def connected(driver,record):received.append(driver)
            event.listen(engine,'connect',connected)
            if boundary=='deferred':
                with engine.begin() as c:
                    c.exec_driver_sql("CREATE FUNCTION owned_retirement_commit_failure() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'Owned deferred commit failure' USING ERRCODE='23514'; END $$")
                    c.exec_driver_sql("CREATE CONSTRAINT TRIGGER owned_retirement_commit_failure AFTER INSERT ON audit_logs DEFERRABLE INITIALLY DEFERRED FOR EACH ROW WHEN (NEW.action='retirement_complete') EXECUTE FUNCTION owned_retirement_commit_failure()")
            def commit(db):
                final=any(isinstance(entry,AuditLog) and entry.action=='retirement_complete' for entry in db.new)
                if final:original_drivers.append(db.connection().connection.driver_connection)
                if final and boundary in ('disconnect','lost_ack') and not fired:
                    db.flush()
                    if boundary=='disconnect':
                        pid=db.connection().connection.driver_connection.info.backend_pid
                        with engine.begin() as observer:assert observer.execute(text('SELECT pg_terminate_backend(:pid)'),{'pid':pid}).scalar_one()
                        fired.append(True);cleanup_start.append(len(received));return actual_commit(db)
                    actual_commit(db);fired.append(True);cleanup_start.append(len(received));raise RuntimeError('Owned final ACK loss')
                try:return actual_commit(db)
                except DBAPIError:
                    if final:cleanup_start.append(len(received))
                    raise
            try:
                PHASE='final_fault'
                with patch.object(Session,'commit',commit),patch.object(filesystem,'rename_noreplace',rename),patch.object(os,'unlink',unlink),patch.object(os,'rmdir',rmdir):
                    try:service.retire_sealed_lifetime(engine,str(media),ticket.timelapse_id,evidence,expected_nonce=nonce)
                    except DBAPIError as error:
                        state=getattr(error.orig,'sqlstate',None)
                        if boundary=='deferred':assert state=='23514'
                        else:assert boundary=='disconnect' and error.connection_invalidated
                    except RuntimeError:assert boundary=='lost_ack' and fired
                    else:raise AssertionError('Final fault was not exercised')
                    assert cleanup_start and len(received)==cleanup_start[-1],'Cleanup reconnected'
                    prior=tuple(calls)
                    assert bool(prior) is physical
                    with Session(engine) as db:
                        assert original_drivers and db.connection().connection.driver_connection is not original_drivers[-1]
                        count=db.execute(text("SELECT count(*) FROM audit_logs WHERE action='retirement_complete' AND entity_id=:id"),{'id':ticket.timelapse_id}).scalar_one()
                        live=db.execute(text('SELECT count(*) FROM timelapses WHERE id=:id'),{'id':ticket.timelapse_id}).scalar_one()
                        assert count==(1 if boundary=='lost_ack' else 0) and live==(0 if boundary=='lost_ack' else 1)
                        assert db.execute(text("SELECT count(*) FROM timelapse_retirement_objects WHERE operation_id=:nonce AND state NOT IN ('retired','originally_absent')"),{'nonce':nonce}).scalar_one()==0
                    if boundary!='lost_ack':assert metadata()==before
                    PHASE='replay'
                    if boundary=='deferred':
                        with engine.begin() as c:
                            c.exec_driver_sql('DROP TRIGGER owned_retirement_commit_failure ON audit_logs');c.exec_driver_sql('DROP FUNCTION owned_retirement_commit_failure()')
                    with patch.object(Session,'commit',actual_commit):
                        if boundary=='lost_ack':assert service.sealed_lifetime_receipt(engine,evidence,operation_id=nonce,original_id=ticket.timelapse_id).operation_id==nonce
                        else:assert service.retire_sealed_lifetime(engine,str(media),ticket.timelapse_id,evidence,expected_nonce=nonce).state=='complete'
                    assert tuple(calls)==prior
                    with Session(engine) as db:assert db.execute(text("SELECT count(*) FROM audit_logs WHERE action='retirement_complete' AND entity_id=:id"),{'id':ticket.timelapse_id}).scalar_one()==1
            finally:event.remove(engine,'connect',connected)
            if case_database is not None:
                engine.dispose()
                with psycopg.connect(host='pg',dbname='postgres',user='postgres',password=private.read_text(),autocommit=True) as admin:
                    admin.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(case_database)))
                    assert admin.execute('SELECT count(*) FROM pg_database WHERE datname=%s',(case_database,)).fetchone()[0]==0
                engine=administration_engine
            print('retirement-final-commit-native-'+('physical' if physical else 'zero')+'-'+boundary+'-rollback-or-replay-oneAudit-noRepeatedFS-pass',flush=True)
            if os.environ.get('RETIREMENT_FINAL_DIAGNOSTIC')=='sequential':
                observed_collision=any(report['matching_fact_count']>0 for report in collision_reports[observation_start:])
                print('retirement-sequential-bounded-progress '+json.dumps({'completed_lifetimes':index+1,'collision_before_preparation':observed_collision}),flush=True)
                if observed_collision:break
        if os.environ.get('RETIREMENT_FINAL_DIAGNOSTIC')=='sequential':
            assert any(report['matching_fact_count']>0 for report in collision_reports),'Actual inode reuse was not observed; no sequential qualification'
            print('retirement-retired-fact-sequential-observed-reuse-complete-pass',flush=True)
        if isolated:print('retirement-final-commit-isolated-six-databases-roots-dropped-pass',flush=True)
        print('retirement-final-commit-diagnostic-complete' if os.environ.get('RETIREMENT_FINAL_DIAGNOSTIC')=='collision' else 'retirement-final-commit-native-pass',flush=True)

if __name__=='__main__':
    try:main()
    except BaseException as error:
        state=getattr(getattr(error,'orig',None),'sqlstate',None)
        diag=getattr(getattr(error,'orig',None),'diag',None)
        def identifier(value):return value if isinstance(value,str) and re.fullmatch('[A-Za-z_][A-Za-z_0-9]{0,127}',value) else 'unknown'
        print('retirement-final-commit-native-failure '+json.dumps({'class':type(error).__name__,'sqlstate':state if isinstance(state,str) and len(state)==5 else 'unknown','phase':PHASE,'relation':identifier(getattr(diag,'table_name',None)),'constraint':identifier(getattr(diag,'constraint_name',None)),'frames':[{'file':Path(frame.filename).name,'function':frame.name,'line':frame.lineno} for frame in traceback.extract_tb(error.__traceback__)[-6:]]}),flush=True)
        raise RuntimeError('Owned native final COMMIT probe failed; sanitized classification above') from None
