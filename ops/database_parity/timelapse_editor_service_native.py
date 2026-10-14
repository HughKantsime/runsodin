"""Actual PostgreSQL editor journal and fixed codec; owned synthetic camera input."""
import os,secrets,tempfile,json,subprocess,time,stat
from pathlib import Path
from contextlib import contextmanager
from types import SimpleNamespace

def main():
    with tempfile.TemporaryDirectory(prefix='odin-editor-owned-') as folder:
        root=Path(folder);root.chmod(0o700);credential=Path(os.environ['EDITOR_NATIVE_PASSWORD_FILE'])
        assert os.getuid()==10001 and credential.is_file() and not credential.is_symlink() and stat.S_IMODE(credential.stat().st_mode)==0o600 and credential.stat().st_uid==10001
        os.environ.update(DATABASE_URL=os.environ['EDITOR_NATIVE_URL'],DATABASE_PASSWORD_FILE=str(credential),ODIN_DATA_DIR=folder,JWT_SECRET_KEY=secrets.token_urlsafe(48))
        from sqlalchemy import text,event,select,func
        from sqlalchemy.orm import Session
        from core.db import engine
        from core.schema import bootstrap_database
        from core.db_compat import IS_POSTGRES
        from core.audit_actor import RequestAuditScope
        from core.auth import create_access_token
        from starlette.requests import Request
        from modules.archives import timelapse_capture as capture
        from modules.archives.timelapse_action_authority import capture_action_credential
        from modules.archives.timelapse_edit import execute_edit
        from core.timelapse_recipes import make_recipe
        bootstrap_database(engine)
        assert engine.dialect.name=='postgresql' and IS_POSTGRES
        media=root/'media';media.mkdir(mode=0o700);capture.TIMELAPSE_DIR=media;capture.SessionLocal=lambda:Session(engine)
        with engine.begin() as c:
            c.execute(text("INSERT INTO groups(id,name,is_org) VALUES(1,'Owned editor school',true),(2,'Owned foreign',true)"))
            c.execute(text("INSERT INTO users(id,username,password_hash,role,is_active,group_id) VALUES(3,'editor-admin','fixture','admin',true,1)"))
            c.execute(text("INSERT INTO printers(id,name,org_id,shared,timelapse_enabled,is_active) VALUES(77,'Owned fixture',1,false,true,true)"))
            c.execute(text("INSERT INTO print_files(id,filename,original_filename,stored_path,org_id) VALUES(111,'plain.3mf','plain.3mf','/owned/plain.3mf',1)"))
            c.execute(text("INSERT INTO jobs(id,item_name,printer_id,print_file_id,charged_to_org_id,status) VALUES(444,'Owned fixture',77,111,1,'printing')"))
        with Session(engine) as db:ticket=capture.get_or_create_timelapse(db,77,444,return_ticket=True)
        payload=subprocess.run(['/usr/bin/ffmpeg','-nostdin','-v','error','-f','lavfi','-i','color=c=gray:s=64x64','-frames:v','1','-f','image2pipe','-vcodec','mjpeg','-'],capture_output=True,check=True).stdout
        @contextmanager
        def stream(*a,**kw):yield SimpleNamespace(status_code=200,headers={},iter_raw=lambda **k:[payload])
        original_stream=capture.httpx.stream;capture.httpx.stream=stream
        try:
            for i in range(7):assert capture.capture_frame(77,ticket.timelapse_id,nonce=ticket.nonce,job_id=444)
        finally:capture.httpx.stream=original_stream
        with engine.begin() as c:c.execute(text("UPDATE jobs SET status='completed' WHERE id=444"))
        assert capture.encode_timelapse(ticket.timelapse_id,printer_id=77,nonce=ticket.nonce,job_id=444)
        with Session(engine) as db:nonce=db.execute(text('SELECT ownership_operation_id FROM timelapses WHERE id=:id'),{'id':ticket.timelapse_id}).scalar_one()
        token=create_access_token({'sub':'editor-admin','role':'admin'})
        request=Request({'type':'http','headers':[(b'authorization',('Bearer '+token).encode())],'query_string':b'','odin.private.audit_actor_scope':RequestAuditScope(None)})
        with Session(engine) as db:evidence=capture_action_credential(request,db,token,kind='bearer_jwt',carrier='bearer',purpose='edit')
        print('editor-native-actualPG-bootstrap-frameReceipts-encoder-source-pass',flush=True)
        source=next(media.glob('encode_*.mp4'));original=source.read_bytes()
        speed_results=[]
        for index,speed in enumerate((.5,1,1.5,2,4,8)):
            identifier=f'{index+1:032x}';recipe=make_recipe('speed',multiplier=speed)
            result=execute_edit(engine,str(media),ticket.timelapse_id,evidence,identifier,nonce=nonce,recipe=recipe)
            assert result['frame_count']==(7*recipe.speed_denominator+recipe.speed_numerator-1)//recipe.speed_numerator
            assert result['result_tid']!=ticket.timelapse_id and result['result_lifetime']==result['result_nonce']
            assert execute_edit(engine,str(media),ticket.timelapse_id,evidence,identifier,nonce=nonce,recipe=recipe)==result
            assert source.read_bytes()==original
            speed_results.append(result)
            print('editor-native-speed-'+str(speed)+'-actualcodec-freshLifetime-replay-pass',flush=True)
        trim=make_recipe('trim',start_seconds=.04,end_seconds=.2)
        first=execute_edit(engine,str(media),ticket.timelapse_id,evidence,'a'*32,nonce=nonce,recipe=trim)
        assert first['frame_count']==4 and first['source_lifetime']==first['result_lifetime']
        second=execute_edit(engine,str(media),ticket.timelapse_id,evidence,'b'*32,nonce=first['result_nonce'],recipe=make_recipe('trim',start_seconds=0,end_seconds=.1))
        old=execute_edit(engine,str(media),ticket.timelapse_id,evidence,'a'*32,nonce=nonce,recipe=trim)
        assert not old['playback_available'] and old['frame_count']==4 and second['frame_count']==3
        assert source.read_bytes()==original
        with Session(engine) as db:
            assert db.execute(text('SELECT count(*) FROM timelapse_retained_versions')).scalar_one()==2
            assert db.execute(text("SELECT count(*) FROM timelapse_publications WHERE kind IN ('trim','speed') AND phase<>'complete'")).scalar_one()==0
        print('editor-native-trim-retained-laterTrim-oldReceipt-noNewerPlayback-pass',flush=True)
        # Both table predicate and exact-row NOWAIT contention fail before an
        # operation/private output is created, and release the root guard.
        for mode in ('table','row'):
            blocker=engine.connect();transaction=blocker.begin()
            try:
                if mode=='table':blocker.execute(text('LOCK TABLE printers IN ROW EXCLUSIVE MODE'))
                else:blocker.execute(text('SELECT id FROM timelapses WHERE id=:id FOR UPDATE'),{'id':ticket.timelapse_id})
                refused=False;started=time.monotonic()
                try:execute_edit(engine,str(media),ticket.timelapse_id,evidence,'d'*32,nonce=second['result_nonce'],recipe=make_recipe('speed',multiplier=2))
                except Exception as error:
                    refused=getattr(getattr(error,'orig',None),'sqlstate',None)=='55P03'
                assert refused and time.monotonic()-started<3
            finally:transaction.rollback();blocker.close()
            with Session(engine) as db:assert db.execute(text("SELECT count(*) FROM timelapse_publications WHERE operation_id=:id"),{'id':'d'*32}).scalar_one()==0
            assert not (media/('publish_'+'d'*32)).exists()
        print('editor-native-table-and-row-NOWAIT-refusal-noOperation-noPrivateOutput-lockReleased-pass',flush=True)
        # Actual deferred commit rejection: codec and rename succeed, final
        # metadata+audit+CAS transaction fails atomically, durable published bytes
        # remain for exact same-operation recovery after the owned fault removal.
        with engine.begin() as c:
            c.execute(text("CREATE FUNCTION public.editor_owned_commit_failure() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF NEW.status='ready' AND NEW.filename LIKE 'speed_%' THEN RAISE EXCEPTION 'owned deferred editor failure'; END IF; RETURN NEW; END $$"))
            c.execute(text('CREATE CONSTRAINT TRIGGER editor_owned_commit_failure AFTER UPDATE ON timelapses DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION public.editor_owned_commit_failure()'))
        failed=False
        try:execute_edit(engine,str(media),ticket.timelapse_id,evidence,'c'*32,nonce=second['result_nonce'],recipe=make_recipe('speed',multiplier=2))
        except Exception:failed=True
        assert failed
        with Session(engine) as db:
            assert db.execute(text("SELECT phase FROM timelapse_publications WHERE operation_id=:id"),{'id':'c'*32}).scalar_one()=='published'
            result_tid=db.execute(text("SELECT result_tid FROM timelapse_publications WHERE operation_id=:id"),{'id':'c'*32}).scalar_one()
            assert db.execute(text('SELECT status FROM timelapses WHERE id=:id'),{'id':result_tid}).scalar_one()=='encoding'
            assert db.execute(text("SELECT count(*) FROM audit_logs WHERE action='timelapse_edit_complete' AND entity_id=:id"),{'id':result_tid}).scalar_one()==0
        with engine.begin() as c:
            c.execute(text('DROP TRIGGER editor_owned_commit_failure ON timelapses'));c.execute(text('DROP FUNCTION public.editor_owned_commit_failure()'))
        recovered=execute_edit(engine,str(media),ticket.timelapse_id,evidence,'c'*32,nonce=second['result_nonce'],recipe=make_recipe('speed',multiplier=2))
        assert recovered['result_tid']==result_tid and recovered['phase']=='complete'
        print('editor-native-realDeferredCOMMIT-fullMutationAuditCASrollback-exactPublishedResume-pass',flush=True)
        from modules.archives.timelapse_snapshot_graph import SnapshotGraphReadView,fence_graph_scope
        from core.timelapse_media_lock import acquire_media_guard
        from core.timelapse_manifest import _root
        from core.timelapse_publication_schema import publications,facts
        from core.timelapse_publication_validation import operation
        from modules.archives.timelapse_action_authority import fence_authorization
        # Real PG frozen-validator equivalence and predicate writer exclusion.
        with acquire_media_guard(media,ticket.timelapse_id) as guard,_root(media) as (_,identity),Session(engine) as db:
            scope=fence_graph_scope(db,guard,identity)
            ops=[dict(r) for r in db.execute(select(publications)).mappings()]
            graph=SnapshotGraphReadView(db,ops,scope=scope,lifetime=ops[0]['source_lifetime'])
            try:
                for op in ops:operation(db,op);operation(graph,op)
                import modules.archives.timelapse_snapshot_graph as graph_module
                from modules.archives.timelapse_retirement import RetirementUnavailable
                saved_budget=graph_module.MAX_BYTES
                statements=[]
                def record_bound(c,cur,statement,*args):statements.append(statement)
                event.listen(engine,'before_cursor_execute',record_bound)
                try:
                    graph_module.MAX_BYTES=sum(len(v.encode('utf8'))+16 if isinstance(v,str) else len(v)+16 if isinstance(v,bytes) else 64 for r in ops for v in r.values())-1
                    try:SnapshotGraphReadView(db,ops,scope=scope,lifetime=ops[0]['source_lifetime'])
                    except RetirementUnavailable as error:assert error.reason=='manifest_limit'
                    else:raise AssertionError('NULL byte bound did not refuse')
                    assert len(statements)==1 and 'CASE WHEN' in statements[0]
                finally:
                    graph_module.MAX_BYTES=saved_budget
                    event.remove(engine,'before_cursor_execute',record_bound)
                print('editor-native-nullable-pretransfer-byte-refusal-before-full-rows-pass',flush=True)
                with engine.connect() as writer:
                    tx=writer.begin()
                    try:
                        writer.execute(text("SET LOCAL lock_timeout='100ms'"))
                        try:writer.execute(facts.update().where(facts.c.operation_id==ops[0]['operation_id']).values(digest_sha256=facts.c.digest_sha256))
                        except Exception as error:assert getattr(error.orig,'sqlstate',None)=='55P03'
                        else:raise AssertionError('Graph predicate writer was not fenced')
                    finally:tx.rollback()
            finally:graph.close()
        # Independent malformed retained-row oracle outside related-life scope.
        # Replica mode is owned superuser fixture corruption only, rolled back.
        publication_origin=next(r for r in ops if r['kind']=='speed')
        with acquire_media_guard(media,ticket.timelapse_id) as guard,_root(media) as (_,identity),Session(engine) as db:
            scope=fence_graph_scope(db,guard,identity)
            db.execute(text("SET LOCAL session_replication_role='replica'"))
            bad=dict(publication_origin,operation_id='e'*32,source_lifetime='d'*32,
                     private_directory='publish_'+'e'*32,destination_path='unrelated.mp4')
            assert db.execute(select(func.count()).select_from(publications).where(publications.c.operation_id==bad['operation_id'])).scalar_one()==0
            db.execute(publications.insert().values(**bad))
            related=[dict(r) for r in db.execute(select(publications).where((publications.c.source_lifetime==publication_origin['source_lifetime'])|(publications.c.result_lifetime==publication_origin['source_lifetime']))).mappings()]
            assert bad['operation_id'] not in {r['operation_id'] for r in related}
            graph=SnapshotGraphReadView(db,related,scope=scope,lifetime=publication_origin['source_lifetime'])
            try:
                count_query=select(func.count()).select_from(publications).where(publications.c.result_nonce==publication_origin['result_nonce'],publications.c.kind!='frame')
                assert db.execute(count_query).scalar_one()==graph.execute(count_query).scalar_one()==2
                from core.schema.migrator import MigrationError
                for reader in (db,graph):
                    try:operation(reader,publication_origin)
                    except MigrationError:pass
                    else:raise AssertionError('Initial speed duplicate result admitted')
            finally:graph.close();db.rollback()
        print('editor-native-freshGraph-frozenEquivalence-predicate55P03-initialSpeedOutsidePredicate-refusal-pass',flush=True)
        from modules.archives.timelapse_lifetime_prepare import prepare_request_lifetime_retirement
        from modules.archives.timelapse_lifetime_action import retire_sealed_lifetime
        from core.timelapse_request_budget import VerificationBudget
        from modules.archives.timelapse_retirement import RetirementUnavailable
        with Session(engine) as db:retire_evidence=capture_action_credential(request,db,token,kind='bearer_jwt',carrier='bearer',purpose='retire')
        # Owned instrumentation only; production modules remain unchanged.
        from timelapse_retirement_profile import RetirementProfile
        profiler=RetirementProfile()
        def profile(label):
            print('editor-native-retirement-profile-'+json.dumps(profiler.snapshot(label),sort_keys=True),flush=True)
        try:
            profiler.install(engine)
            # Incoming result ownership and original outgoing readonly dependency,
            # both actualPG/data+codec lifetimes and current signed principal.
            incoming=speed_results[0]
            prepare_request_lifetime_retirement(engine,str(media),incoming['result_tid'],retire_evidence,expected_nonce=incoming['result_nonce'],budget=VerificationBudget())
            retire_sealed_lifetime(engine,str(media),incoming['result_tid'],retire_evidence,expected_nonce=incoming['result_nonce'])
            assert source.read_bytes()==original
            profile('incoming_speed_retirement')
            print('editor-native-speedResult-retirement-preserves-original-pass',flush=True)
            preserved=speed_results[1]
            with Session(engine) as db:preserved_name=db.execute(text('SELECT filename FROM timelapses WHERE id=:id'),{'id':preserved['result_tid']}).scalar_one()
            preserved_bytes=(media/preserved_name).read_bytes()
            prepare_request_lifetime_retirement(engine,str(media),ticket.timelapse_id,retire_evidence,expected_nonce=second['result_nonce'],budget=VerificationBudget())
            retire_sealed_lifetime(engine,str(media),ticket.timelapse_id,retire_evidence,expected_nonce=second['result_nonce'])
            profile('original_31member_retirement')
            assert (media/preserved_name).read_bytes()==preserved_bytes and not source.exists()
            # Historical immutable source proof after physical deletion; a reused
            # original numeric ID supplies no source authority or alternate bytes.
            with engine.begin() as c:c.execute(text("INSERT INTO timelapses(id,printer_id,print_job_id,filename,status) VALUES(:id,77,444,'reused-new-file.mp4','ready')"),{'id':ticket.timelapse_id})
            prepare_request_lifetime_retirement(engine,str(media),preserved['result_tid'],retire_evidence,expected_nonce=preserved['result_nonce'],budget=VerificationBudget())
            retire_sealed_lifetime(engine,str(media),preserved['result_tid'],retire_evidence,expected_nonce=preserved['result_nonce'])
            with Session(engine) as db:
                assert db.execute(text('SELECT filename FROM timelapses WHERE id=:id'),{'id':ticket.timelapse_id}).scalar_one()=='reused-new-file.mp4'
                assert db.execute(text('SELECT count(*) FROM timelapses WHERE id=:id'),{'id':preserved['result_tid']}).scalar_one()==0
            profile('incoming_after_source_retired_reusedID')
            print('editor-native-originalRetirement-preserves-speed-thenHistoricalFacts-reusedID-notReopened-pass',flush=True)

        finally:
            try:
                if profiler.metrics:profile('unfinished_at_exit')
            finally:profiler.close()

        print('editor-native-pass',flush=True)

if __name__=='__main__':main()
