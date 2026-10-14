"""Owned full-bootstrap canonical executable inventory, no application resources."""
import os
import tempfile
from pathlib import Path
from sqlalchemy.orm import Session
from sqlalchemy import create_engine,text
from core.schema import bootstrap_database


def main():
    import uuid
    credential=Path(tempfile.mkdtemp(prefix='canonical-owned-config-'))/'password'
    credential.write_text(uuid.uuid4().hex);credential.chmod(0o600)
    os.environ['DATABASE_PASSWORD_FILE']=str(credential)
    os.environ['DATABASE_URL']=os.environ['CANONICAL_NATIVE_URL'].replace('postgresql+psycopg://','postgresql://')
    os.environ['JWT_SECRET_KEY']=uuid.uuid4().hex+uuid.uuid4().hex
    engine=create_engine(os.environ['CANONICAL_NATIVE_URL'],hide_parameters=True)
    result=bootstrap_database(engine)
    with engine.connect() as c:
        functions=c.execute(text("SELECT p.proname,pg_get_function_identity_arguments(p.oid),p.proconfig,p.prosecdef,p.provolatile,p.proparallel,p.proisstrict,p.proleakproof,p.prokind FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public' ORDER BY p.proname,p.oid")).all()
        triggers=c.execute(text("SELECT c.relname,t.tgname,t.tgenabled,pg_get_triggerdef(t.oid) FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND NOT t.tgisinternal ORDER BY c.relname,t.tgname")).all()
        print('canonical-functions=',[tuple(r) for r in functions])
        print('canonical-triggers=',[tuple(r) for r in triggers])
        print('canonical-bootstrap-fingerprint=',result['schema_fingerprint'])
    from modules.system.postgres_canonical_executables import validate_executables
    with engine.connect() as c:validate_executables(c)
    from modules.system.backup_service import BackupValidationError
    negative_inventory=[
        "ALTER FUNCTION public.orders_economic_guard() SECURITY DEFINER",
        "ALTER FUNCTION public.orders_economic_guard() SET search_path TO private,public",
        "ALTER FUNCTION public.orders_economic_guard() PARALLEL SAFE",
        "ALTER FUNCTION public.orders_economic_guard() STRICT",
        "ALTER FUNCTION public.orders_economic_guard() COST 200",
        "CREATE FUNCTION public.orders_economic_guard(integer) RETURNS integer LANGUAGE sql AS 'SELECT 1'",
        "CREATE FUNCTION public.unapproved() RETURNS integer LANGUAGE sql AS 'SELECT 1'",
        "ALTER TABLE public.orders DISABLE TRIGGER orders_economic_guard",
        "DROP TRIGGER orders_economic_guard ON public.orders",
        "DROP TRIGGER orders_economic_guard ON public.orders; CREATE TRIGGER orders_economic_guard BEFORE INSERT OR UPDATE ON public.orders FOR EACH ROW EXECUTE FUNCTION public.jobs_economic_guard()",
    ]
    from modules.system.postgres_canonical_executables import canonical_functions, sql_statements
    original_body=canonical_functions()['cost_participation_guard'][0]
    reordered_body=original_body.replace(' RETURN NEW;\n','').replace(' END IF;\n',' END IF;\n RETURN NEW;\n',1)
    assert sql_statements(reordered_body)==sql_statements(original_body)
    negative_inventory.append('CREATE OR REPLACE FUNCTION public.cost_participation_guard() RETURNS trigger LANGUAGE plpgsql SET search_path TO pg_catalog, public AS $$'+reordered_body+'$$')
    negative_inventory += [
        "ALTER FUNCTION public.spools_economic_birth_guard() SECURITY DEFINER",
        "ALTER FUNCTION public.print_jobs_economic_birth_guard() SET search_path TO public",
        "ALTER TABLE public.cost_source_lifetimes DISABLE TRIGGER cost_source_lifetimes_guard",
        "DROP TRIGGER spools_economic_birth_guard ON public.spools",
        "CREATE OR REPLACE FUNCTION public.cost_source_lifetimes_guard() RETURNS trigger LANGUAGE plpgsql SET search_path TO pg_catalog, public AS $$ BEGIN RETURN NEW; END; $$",
    ]
    assert len(functions)==15 and len(triggers)==21
    from core.audit_actor_schema import PG_BODY
    negative_inventory += [
        'CREATE OR REPLACE FUNCTION public.audit_actor_erasure_immutable() RETURNS trigger LANGUAGE plpgsql SET search_path TO pg_catalog AS $$'+PG_BODY+' $$',
        'ALTER FUNCTION public.audit_actor_erasure_immutable() SET search_path TO pg_catalog,public',
        'DROP TRIGGER audit_actor_erasures_no_truncate ON public.audit_actor_erasures',
        'ALTER TABLE public.audit_actor_erasures DISABLE TRIGGER audit_actor_erasures_no_truncate',
        'DROP TRIGGER audit_actor_erasures_no_truncate ON public.audit_actor_erasures; CREATE TRIGGER audit_actor_erasures_no_truncate BEFORE TRUNCATE ON public.audit_actor_erasures FOR EACH STATEMENT EXECUTE FUNCTION public.orders_economic_guard()',
        'ALTER TABLE public.timelapse_retirement_objects DISABLE TRIGGER timelapse_retirement_objects_membership_guard',
    ]
    for statement in negative_inventory:
        with engine.connect() as c:
            c.exec_driver_sql(statement)
            try:validate_executables(c)
            except BackupValidationError:pass
            else:raise AssertionError('Noncanonical native executable admitted')
            c.rollback()
    print('canonical-native-executable-negative-count=',len(negative_inventory))
    # One complete COPY checks immediate self references at statement end.
    with engine.begin() as c:
        c.exec_driver_sql('CREATE TEMP TABLE canonical_self_copy(id integer PRIMARY KEY,parent integer REFERENCES canonical_self_copy(id))')
        raw=c.connection.driver_connection
        with raw.cursor() as cursor:
            with cursor.copy('COPY canonical_self_copy(id,parent) FROM STDIN') as copy:
                copy.write('2\t1\n1\t\\N\n')
        assert c.exec_driver_sql('SELECT count(*) FROM canonical_self_copy').scalar_one()==2
    print('canonical-native-self-referential-complete-copy-pass')
    # A deferred COPY edge is still checked at the data transaction commit.
    with engine.connect() as c:
        c.exec_driver_sql('CREATE TEMP TABLE canonical_copy_history(nonce text PRIMARY KEY)')
        c.exec_driver_sql('CREATE TEMP TABLE canonical_copy_birth(nonce text REFERENCES canonical_copy_history(nonce) DEFERRABLE INITIALLY DEFERRED)')
        raw=c.connection.driver_connection
        with raw.cursor() as cursor:
            with cursor.copy('COPY canonical_copy_birth(nonce) FROM STDIN') as copy:copy.write('orphan\n')
        try:c.commit()
        except Exception:c.rollback()
        else:raise AssertionError('Deferred orphan COPY committed')
    print('canonical-native-deferred-copy-orphan-commit-rejected-pass')
    engine.dispose()
    print('canonical-native-inventory-pass')
    restore_probe()


def restore_probe():
    import uuid
    from core.economic_identity import _add_created
    from modules.orders.models import Order, OrderItem, Product
    from modules.jobs.models import Job
    from modules.printers.models import Printer
    from modules.inventory.models import FilamentLibrary
    from core.economic_source_creation import _create_spool,_create_attempt
    from modules.system import postgres_backup_service as backup
    from modules.system.postgres_canonical_executables import validate_executables
    from modules.system.backup_service import BackupValidationError
    from core.database_config import create_database_engine
    maintenance='postgresql://postgres@pg/postgres'
    source='postgresql://postgres@pg/canonical_source'
    admin=create_engine(os.environ['CANONICAL_NATIVE_URL'],isolation_level='AUTOCOMMIT',hide_parameters=True)
    with admin.connect() as c:c.exec_driver_sql('CREATE DATABASE canonical_source')
    try:
        with tempfile.TemporaryDirectory(prefix='canonical-restore-') as folder:
            root=Path(folder);(root/'backups').mkdir()
            app_file=root/'app-secret';maintenance_file=root/'maintenance-secret'
            for file in (app_file,maintenance_file):file.write_text(uuid.uuid4().hex);file.chmod(0o600)
            os.environ['DATABASE_PASSWORD_FILE']=str(app_file)
            engine=create_database_engine(source,role='restore',password_file=str(app_file))
            bootstrap_database(engine)
            with Session(engine) as db:
                _add_created(db,Order(org_id=None,order_number='Synthetic prospective identified order',revenue=42),
                    'order',owner_org_id=None,global_authority=True)
                product=Product(name='Synthetic source backup',org_id=7)
                printer=Printer(name='Synthetic source backup',org_id=7,api_type='bambu')
                filament=FilamentLibrary(brand='Synthetic',name='Backup',material='PLA')
                db.add_all([product,printer,filament]);db.flush()
                order=_add_created(db,Order(org_id=7),'order',owner_org_id=7)
                item=_add_created(db,OrderItem(order_id=order.id,product_id=product.id),'item',owner_org_id=7)
                job=_add_created(db,Job(order_item_id=item.id,charged_to_org_id=7,item_name='Source backup'),'job',owner_org_id=7)
                spool_values={'org_id':7,'filament_id':filament.id,'price':0,'initial_weight_g':1000,'status':'active'}
                attempt_values={'printer_id':printer.id,'scheduled_job_id':job.id,'status':'running','started_at':'2026-10-13T12:00:00Z'}
                known_spool=_create_spool(db,spool_values,owner_org_id=7)
                known_attempt=_create_attempt(db,attempt_values)
                unknown_values={k:v for k,v in attempt_values.items() if k!='scheduled_job_id'}
                retired=_create_attempt(db,unknown_values)
                db.execute(text('DELETE FROM print_jobs WHERE id=:id'),{'id':retired.id})
                db.execute(text("SELECT setval(pg_get_serial_sequence('print_jobs','id'),:id,false)"),{'id':retired.id})
                reused=_create_attempt(db,unknown_values)
                unknown_spool=_create_spool(db,spool_values)
                assert reused.id==retired.id and reused.birth_nonce!=retired.birth_nonce
                assert known_spool.identity_uid and known_attempt.identity_uid
                assert unknown_spool.identity_uid is None and reused.identity_uid is None
                db.execute(text("INSERT INTO system_config(key,value) VALUES('canonical_marker','\"original\"')"))
                db.commit()
            # Real zero-frame source/service admission; no camera, ffmpeg or retirement.
            os.environ.setdefault('JWT_SECRET_KEY',uuid.uuid4().hex+uuid.uuid4().hex)
            from modules.archives.timelapse_capture import get_or_create_timelapse
            from modules.archives.timelapse_encode import encode_owned_timelapse
            from modules.archives.timelapse_lifetime_prepare import prepare_lifetime_retirement
            from core.timelapse_retirement_membership_schema import bindings,members,receipts
            from core.audit_actor import RequestAuditScope,bind_verified_actor,stage_actor,stamp_actor,erase_actor_history
            from core.models import AuditLog
            from sqlalchemy import event,select
            if not event.contains(AuditLog,'after_insert',stamp_actor):event.listen(AuditLog,'after_insert',stamp_actor)
            media=root/'media';media.mkdir(mode=0o700)
            with Session(engine) as db:
                p=Printer(name='Owned zero backup',org_id=None,shared=True,api_type='synthetic',is_active=True,timelapse_enabled=True)
                db.add(p);db.flush()
                fid=db.execute(text("INSERT INTO print_files(filename,original_filename,stored_path,org_id) VALUES('owned.3mf','owned.3mf','/owned/fixture.3mf',NULL) RETURNING id")).scalar_one()
                job=Job(item_name='Owned zero backup',printer_id=p.id,print_file_id=fid,status='printing',charged_to_org_id=None)
                db.add(job);db.flush();pid,jid=p.id,job.id
                uid=db.execute(text("INSERT INTO users(username,password_hash,role,is_active,group_id) VALUES('owned-zero-backup','invalid-owned-fixture','admin',TRUE,NULL) RETURNING id")).scalar_one()
                actor_id=db.execute(text("INSERT INTO users(username,password_hash,role,is_active,group_id) VALUES('owned-actor-backup','invalid-owned-fixture','admin',TRUE,NULL) RETURNING id")).scalar_one()
                db.commit();ticket=get_or_create_timelapse(db,pid,jid,return_ticket=True);assert ticket is not None
                db.execute(text("UPDATE jobs SET status='completed' WHERE id=:id"),{'id':jid});db.commit()
            principal={'id':uid,'role':'admin','group_id':None,'_auth_kind':'session_jwt'}
            assert not encode_owned_timelapse(sessions=lambda:Session(engine),root=media,timelapse_id=ticket.timelapse_id,printer_id=pid,nonce=ticket.nonce,job_id=jid)
            assert prepare_lifetime_retirement(engine,media,ticket.timelapse_id,principal,expected_nonce=ticket.nonce).created
            with engine.begin() as c:
                assert c.execute(select(bindings.c.phase,bindings.c.object_count)).one()==('sealed',0)
                assert c.execute(select(members)).first() is None and c.execute(select(receipts)).first() is None
                c.execute(text("DELETE FROM audit_logs WHERE action='timelapse_encode_failed' AND entity_id=:id"),{'id':ticket.timelapse_id})
            def replay():
                assert not prepare_lifetime_retirement(engine,media,ticket.timelapse_id,principal,expected_nonce=ticket.nonce).created
                with engine.connect() as c:
                    assert c.execute(select(receipts)).first() is None
                    assert c.execute(text("SELECT count(*) FROM audit_logs WHERE action='timelapse_encode_failed' AND entity_id=:id"),{'id':ticket.timelapse_id}).scalar_one()==0
            replay();bootstrap_database(engine)
            def staged(db,action):
                scope=RequestAuditScope('127.0.0.1');scope.register(db)
                bind_verified_actor(db,{'id':actor_id,'username':'owned-actor-backup','_auth_kind':'bearer_jwt'})
                item=AuditLog(action=action,details={'original':'kept exactly'},ip_address='127.0.0.1')
                stage_actor(db,item);db.add(item);return item
            with Session(engine) as db:
                staged(db,'canonical_actor_original');db.commit()
                db.execute(text('SELECT id FROM users WHERE id=:id FOR UPDATE'),{'id':actor_id})
                db.execute(text("UPDATE users SET username='erased-owned-backup' WHERE id=:id"),{'id':actor_id})
                erase_actor_history(db,actor_id);db.commit()
            def erased_stamp():
                with Session(engine) as db:
                    item=staged(db,'canonical_actor_suppression');db.flush()
                    assert db.execute(text('SELECT actor_user_id,actor_username,actor_auth_kind,ip_address FROM audit_logs WHERE id=:id'),{'id':item.id}).one()==(None,None,None,None)
                    db.rollback()
            erased_stamp()
            for statement in ('UPDATE audit_actor_erasures SET erased_at=erased_at','DELETE FROM audit_actor_erasures','TRUNCATE audit_actor_erasures',
                "UPDATE timelapse_retirement_lifetime_bindings SET object_count=1",'DELETE FROM timelapse_retirement_lifetime_bindings'):
                with engine.connect() as c:
                    try:c.exec_driver_sql(statement)
                    except Exception:c.rollback()
                    else:raise AssertionError('Retained immutable authority mutated')
            print('canonical-native-sealed-audit-pruned-replay-erasure-ledger-fixture-pass')
            def marker():
                with engine.connect() as c:return c.execute(text("SELECT value::text FROM system_config WHERE key='canonical_marker'")).scalar_one()
            def economic_snapshot():
                with engine.connect() as c:
                    return {table:c.execute(text(f'SELECT row_to_json(t)::text FROM {table} t ORDER BY row_to_json(t)::text')).scalars().all()
                        for table in ('spools','print_jobs','cost_source_lifetimes','cost_identities','cost_job_bindings','cost_attempt_bindings','timelapse_retirement_lifetime_bindings','timelapse_retirement_members','timelapse_retirement_container_receipts','timelapse_retirement_objects','audit_actor_erasures')}
            original_snapshot=economic_snapshot
            def economic_snapshot():
                snapshot=original_snapshot()
                with engine.connect() as c:
                    snapshot['original_actor_history']=c.execute(text("SELECT row_to_json(t)::text FROM audit_logs t WHERE action='canonical_actor_original' ORDER BY id")).scalars().all()
                    assert c.execute(text("SELECT actor_user_id,actor_username,actor_auth_kind,ip_address FROM audit_logs WHERE action='canonical_actor_original'")).one()==(None,None,None,None)
                return snapshot
            birth_snapshot=economic_snapshot()
            assert len(birth_snapshot['cost_source_lifetimes'])==5
            original=marker()
            # Actual app backup performs canonical disposable restoration validation.
            try:
                candidate,metadata=backup.create_postgres_backup(source,maintenance_url=maintenance,
                    password_file=str(app_file),maintenance_password_file=str(maintenance_file),base_dir=root)
            except BackupValidationError as exc:
                # Owned synthetic fixture only: expose the actual constraint diagnosis.
                cause=exc.__cause__
                print('canonical-owned-restore-stderr=',getattr(cause,'stderr',None))
                raise
            assert metadata['restore_validated'] is True and marker()==original
            print('canonical-native-application-backup-pass')
            with engine.begin() as c:c.execute(text("UPDATE system_config SET value='\"changed\"' WHERE key='canonical_marker'"))
            backup.stage_postgres_restore(candidate,source,maintenance,password_file=str(app_file),
                maintenance_password_file=str(maintenance_file),base_dir=root)
            engine.dispose()
            result=backup.apply_pending_postgres_restore(source,maintenance,acknowledgement=metadata['sha256'],
                password_file=str(app_file),maintenance_password_file=str(maintenance_file),base_dir=root)
            assert result['status']=='restored'
            assert marker()==original and economic_snapshot()==birth_snapshot
            print('canonical-native-deferred-birth-cycle-immediate-binding-order-restore-pass')
            with engine.connect() as c:validate_executables(c)
            replay();erased_stamp()
            print('canonical-native-actual-restore-sealed-replay-actor-ledger-pass')
            # Actual injected post-restore failure must restore the current source.
            with engine.begin() as c:c.execute(text("UPDATE system_config SET value='\"rollback-original\"' WHERE key='canonical_marker'"))
            rollback_original=marker()
            backup.stage_postgres_restore(candidate,source,maintenance,password_file=str(app_file),
                maintenance_password_file=str(maintenance_file),base_dir=root)
            engine.dispose()
            try:
                backup.apply_pending_postgres_restore(source,maintenance,acknowledgement=metadata['sha256'],
                    password_file=str(app_file),maintenance_password_file=str(maintenance_file),base_dir=root,fail_after_restore=True)
            except RuntimeError as exc:assert 'Injected post-restore failure' in str(exc)
            else:raise AssertionError('Injected restore failure expected')
            assert marker()==rollback_original and economic_snapshot()==birth_snapshot
            assert not backup.postgres_paths(root).manifest.exists()
            with engine.connect() as c:validate_executables(c)
            replay();erased_stamp()
            print('canonical-native-actual-rollback-sealed-replay-actor-ledger-pass')
            # Actual failed rollback keeps exclusive session lock and durable marker.
            backup.stage_postgres_restore(candidate,source,maintenance,password_file=str(app_file),
                maintenance_password_file=str(maintenance_file),base_dir=root)
            engine.dispose()
            real_restore=backup._restore_postgres_archive
            calls=[]
            def fail_recovery(*args,**kwargs):
                if args[1]==source:
                    calls.append(True)
                    if len(calls)==2:raise RuntimeError('Synthetic recovery failure')
                return real_restore(*args,**kwargs)
            backup._restore_postgres_archive=fail_recovery
            try:
                try:
                    backup.apply_pending_postgres_restore(source,maintenance,acknowledgement=metadata['sha256'],
                        password_file=str(app_file),maintenance_password_file=str(maintenance_file),base_dir=root,fail_after_restore=True)
                except BackupValidationError as exc:assert 'recovery incomplete' in str(exc)
                else:raise AssertionError('Failed recovery expected')
            finally:backup._restore_postgres_archive=real_restore
            held_connection,held_engine=backup._FAILED_RESTORE_BARRIERS[source]
            manifest=backup._read_pending_manifest(backup.postgres_paths(root))
            assert manifest['state']=='restore_in_progress' and manifest['recovery_incomplete'] is True
            blocked=create_database_engine(source,role='api',password_file=str(app_file))
            from sqlalchemy.exc import SQLAlchemyError
            try:
                try:
                    with blocked.connect():pass
                except SQLAlchemyError:pass
                else:raise AssertionError('Incomplete recovery barrier did not block API connection')
            finally:blocked.dispose()
            print('canonical-native-failed-recovery-barrier-held-pass')
            # Controlled fixture session loss models loss of the process advisory lock.
            held_connection.close();held_engine.dispose();backup._FAILED_RESTORE_BARRIERS.pop(source)
            from scripts import wait_for_database_schema as gate
            os.environ.update(DATABASE_URL=source,ODIN_DATA_DIR=str(root),ODIN_DB_ROLE='api',ODIN_SCHEMA_WAIT_SECONDS='5')
            from core.postgres_restore_gate import RestoreConsumerBlocked
            try:gate.main()
            except RestoreConsumerBlocked:pass
            else:raise AssertionError('Unfinished restore startup was admitted')
            assert backup.postgres_paths(root).manifest.exists()
            print('canonical-native-unfinished-marker-follower-startup-blocked-pass')
            original=marker()
            # Same-name executable payload is extracted as evidence, never executed.
            with engine.begin() as c:
                c.exec_driver_sql("CREATE OR REPLACE FUNCTION public.audit_actor_erasure_immutable() RETURNS trigger LANGUAGE plpgsql SET search_path TO pg_catalog AS $$ BEGIN UPDATE public.system_config SET value='\"EXECUTED\"' WHERE key='canonical_marker'; RETURN NEW; END; $$")
            malicious=root/'malicious.dump'
            backup._run_pg_tool(['pg_dump','--format=custom','--schema','public','--no-owner','--no-privileges','--no-comments',
                '--file',str(malicious),'--dbname',backup._credentialless_cli_url(source,'odin-backup')],source,str(app_file),root)
            try:
                backup.validate_postgres_restore(malicious,source,maintenance,password_file=str(app_file),
                    maintenance_password_file=str(maintenance_file),base_dir=root)
            except BackupValidationError as exc:
                assert 'schema/executables differ' in str(exc)
            else:raise AssertionError('Altered same-name function archive admitted')
            assert marker()==original
            print('canonical-native-malicious-marker-zero-execution-pass')
            engine.dispose()
    finally:
        with admin.connect() as c:c.exec_driver_sql('DROP DATABASE canonical_source WITH (FORCE)')
        admin.dispose()


if __name__=='__main__':main()
