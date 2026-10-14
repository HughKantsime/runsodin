"""Owned actual PG COPY/replay; no registration/account or production effects."""
import os,stat,tempfile,secrets,json,traceback,subprocess,re,sys,select,signal
from pathlib import Path
from uuid import uuid4
from time import monotonic
PHASE='bootstrap'


def child():
    from sqlalchemy import text
    from core.database_config import create_database_engine
    from core import priced_estimate_restore as r
    url='postgresql+psycopg://postgres@pg/'+os.environ['PRICED_RESTORE_CHILD_DATABASE']
    engine=create_database_engine(url,role='restore',password_file=os.environ['PRICED_ESTIMATE_NATIVE_PASSWORD_FILE'])
    original=r._insert;reported=False
    def insert(stage,table,row):
        nonlocal reported
        original(stage,table,row)
        if not reported:
            reported=True;print('owned-priced-child-replay-staged',flush=True)
            # Parent kills this OWNED process group after observing real inserted
            # data in an uncommitted caller transaction, not a synthetic error.
            signal.pause()
    r._insert=insert
    try:
        with engine.begin() as db:
            db.execute(text('SELECT 1'))
            file=Path(os.environ['PRICED_RESTORE_CHILD_STREAM'])
            with file.open('rb') as stream:r.restore_priced_tables(db,stream,table_data=tuple(r.TableDataEntry(i+1,t) for i,t in enumerate(r.schema.TABLES)),budget=r.RestoreBudget(file.stat().st_size,monotonic()+120))
    finally:r._insert=original;engine.dispose()


def main():
    global PHASE
    secret=Path(os.environ['PRICED_ESTIMATE_NATIVE_PASSWORD_FILE'])
    assert os.getuid()==10001 and secret.stat().st_uid==10001 and stat.S_IMODE(secret.stat().st_mode)==0o600 and not secret.is_symlink()
    with tempfile.TemporaryDirectory(prefix='owned-priced-restore-') as folder:
        os.environ.update(DATABASE_URL='postgresql+psycopg://postgres@pg/postgres',DATABASE_PASSWORD_FILE=str(secret),ODIN_DATA_DIR=folder,JWT_SECRET_KEY=secrets.token_urlsafe(48))
        from sqlalchemy import text
        from sqlalchemy.orm import Session
        from sqlalchemy.exc import DBAPIError
        from core.db import engine
        from core.db_compat import IS_POSTGRES
        from core.database_config import create_database_engine
        from core.schema import bootstrap_database
        from core.economic_identity import _add_created
        from modules.jobs.models import Job
        from core import priced_estimate_schema as s,priced_estimate_validation as v,priced_estimate_restore as r
        from core.priced_job_estimate import calculate_priced_job_estimate
        assert IS_POSTGRES and engine.dialect.name=='postgresql'
        bootstrap_database(engine,Path('/source/backend'))
        def insert(db,table,row):return db.execute(text('INSERT INTO public.'+table+'('+','.join(row)+') VALUES('+','.join(':'+n for n in row)+')'),row)
        def snapshot(db):return {table:[dict(row) for row in db.execute(text('SELECT * FROM public.'+table+' ORDER BY '+','.join(r.columns(table)))).mappings()] for table in s.TABLES}
        def raw_counts(db):return tuple(db.execute(text('SELECT count(*) FROM public.'+table)).scalar_one() for table in s.TABLES)
        now='2026-10-13T12:00:00.000000Z'
        payload=dict(currency='USD',assumptions={name:'0' for name in s.ACTIVE_FIELDS},material_rates={'PLA':dict(currency='USD',cost_per_gram='2')});payload['assumptions'].update(printer_lifespan='1',parts_per_month='1')
        with engine.begin() as db:
            db.execute(text("INSERT INTO groups(id,name,is_org) VALUES(1,'Owned SQL fixture',true)"));s.apply(db)
            with Session(bind=db) as session:
                job=_add_created(session,Job(item_name='Owned prospective restore',quantity=3,charged_to_org_id=1),'job',owner_org_id=1)
                job_uid=session.execute(text('SELECT economic_uid FROM jobs WHERE id=:id'),{'id':job.id}).scalar_one()
            identity=dict(db.execute(text('SELECT * FROM cost_identities WHERE identity_uid=:uid'),{'uid':job_uid}).mappings().one())
            uid=uuid4().hex
            for revision in (1,2):insert(db,s.SOURCE,dict(source_uid=uid,revision=revision,owner_scope='org',owner_key=1,currency='USD',assumptions_json=v.canonical_json(payload),source_sha256=v.digest(payload),assumptions_kind='explicit_saved',actor_id=7,created_at=now))
            histories=[]
            for revision in (1,2,3):
                row=dict(owner_scope='org',owner_key=1,revision=revision,source_uid=uid if revision<3 else None,source_revision=revision if revision<3 else None,source_owner_scope='org' if revision<3 else None,source_owner_key=1 if revision<3 else None,currency='USD' if revision<3 else None,actor_binding_uid=uuid4().hex,actor_id=7,command_nonce=uuid4().hex,expected_revision=revision-1,created_at=now);row['request_sha256']=v.digest(v.selection_command(row));insert(db,s.SELECTION_HISTORY,row);histories.append(row)
                if revision==1:insert(db,s.SELECTION,dict(owner_scope='org',owner_key=1,revision=1))
                else:db.execute(text('UPDATE public.'+s.SELECTION+' SET revision=:revision WHERE owner_key=1'),{'revision':revision})
            receipts=[];inputs=dict(material_key='PLA',grams_per_run='1',hours_per_run='0',runs=3,payload_sha256=None,plate_index=None,piece_count=None,capacity=None,model_fingerprint=None,file_fingerprint=None)
            priced={**payload,'assumptions':{**payload['assumptions'],'spool_cost':'0','spool_weight':'1'}};calculated=calculate_priced_job_estimate(priced,material_key='PLA',grams_per_run='1',hours_per_run='0',runs=3)
            for revision in (1,2):
                h=histories[revision-1];row=dict(receipt_uid=uuid4().hex,job_uid=job_uid,job_kind='job',owner_scope='org',owner_key=1,receipt_revision=revision,classification='job_estimate',formula='per-run-cost-v1',rounding='HALF_EVEN',amount='6.00',runs=3,required_total_name='total',unknown_reason=None,selection_owner_scope='org',selection_owner_key=1,selection_revision=revision,input_sha256=v.digest(inputs),input_json=v.canonical_json(inputs),payload_sha256=None,plate_index=None,piece_count=None,capacity=None,created_at=now,**{n:h[n] for n in s.SOURCE_LINK});insert(db,s.RECEIPT,row);receipts.append(row)
                for c in calculated.components:insert(db,s.COMPONENT,dict(receipt_uid=row['receipt_uid'],component_name=c.name,currency=c.currency,basis=c.basis,predecessor_name=s.PREDECESSOR[c.name],numerator=str(c.numerator),denominator=str(c.denominator)))
            for revision in (1,2,3):
                row=dict(job_uid=job_uid,owner_scope='org',owner_key=1,revision=revision,receipt_uid=receipts[revision-1]['receipt_uid'] if revision<3 else None,invalidation_reason=None if revision<3 else 'explicit_clear',current_input_sha256=receipts[0]['input_sha256'] if revision<3 else None,actor_binding_uid=uuid4().hex,actor_id=7,command_nonce=uuid4().hex,expected_revision=revision-1,created_at=now);row['request_sha256']=v.digest(v.reference_command(row));insert(db,s.REFERENCE_HISTORY,row)
                current={name:row[name] for name in r.columns(s.CURRENT)}
                if revision==1:insert(db,s.CURRENT,current)
                else:db.execute(text('UPDATE public.'+s.CURRENT+' SET revision=:revision,receipt_uid=:receipt_uid,invalidation_reason=:invalidation_reason,current_input_sha256=:current_input_sha256 WHERE job_uid=:job_uid'),current)
            v.validate(db);expected=snapshot(db)
        PHASE='archive_extraction'
        pgpass=Path(folder)/'pgpass';pgpass.write_text('pg:5432:*:postgres:'+secret.read_text().strip()+'\n');pgpass.chmod(0o600);env={**os.environ,'PGPASSFILE':str(pgpass)}
        archive=Path(folder)/'owned.dump';extracted=Path(folder)/'owned.sql';listing=Path(folder)/'owned.list'
        def tool(args,output=None):
            with output.open('wb') if output else __import__('contextlib').nullcontext(subprocess.PIPE) as sink:
                result=subprocess.run(args,env=env,stdout=sink,stderr=subprocess.PIPE,timeout=60)
            if result.returncode:raise RuntimeError('Owned dump tool failed')
            return result.stdout
        tool(['pg_dump','--format=custom','--no-owner','--no-privileges','--dbname','postgresql://postgres@pg/postgres','--file',str(archive),*[arg for table in s.TABLES for arg in ('--table','public.'+table)]])
        toc=tool(['pg_restore','--list',str(archive)]).decode();selected=[];entries=[]
        for line in toc.splitlines():
            match=re.fullmatch(r'(\d+);\s+\d+\s+\d+\s+TABLE DATA public ([a-z_]+) postgres',line)
            if match:
                assert match[2] in s.TABLES;entries.append(r.TableDataEntry(int(match[1]),match[2]));selected.append(line)
        assert len(entries)==7 and {e.table for e in entries}==set(s.TABLES)
        listing.write_text('\n'.join(selected)+'\n');listing.chmod(0o600)
        tool(['pg_restore','--data-only','--no-owner','--no-privileges','--use-list',str(listing),'--file','-',str(archive)],extracted);extracted.chmod(0o600)
        # This metadata is not printed; fixed package producer-build confirmation.
        metadata=extracted.read_bytes().splitlines()[:12]
        producer=next(line for line in metadata if line.startswith(b'-- Dumped by pg_dump version '))
        assert producer in (b'-- Dumped by pg_dump version 16.15',b'-- Dumped by pg_dump version 16.15 (Debian 16.15-1.pgdg13+2)')
        print('priced-restore-actual-owned-customArchive-sevenTOC-16.15-producer-qualified',flush=True)
        if os.environ.get('PRICED_RESTORE_DIAGNOSTIC')=='1':
            print('priced-restore-producer-variant-'+('plain' if producer.endswith(b'version 16.15') else 'debian16.15-1.pgdg13+2'),flush=True)
            print('priced-estimate-restore-envelope-diagnostic-complete',flush=True)
            return
        admin=create_database_engine('postgresql+psycopg://postgres@pg/postgres',role='restore',password_file=str(secret));targets=[]
        def target(label):
            name='odin_priced_restore_'+uuid4().hex[:12]+'_'+label
            assert re.fullmatch('[a-z0-9_]{1,63}',name)
            with admin.connect().execution_options(isolation_level='AUTOCOMMIT') as db:db.exec_driver_sql('CREATE DATABASE "'+name+'"')
            e=create_database_engine('postgresql+psycopg://postgres@pg/'+name,role='restore',password_file=str(secret));targets.append((name,e));bootstrap_database(e,Path('/source/backend'))
            with e.begin() as db:s.apply(db);insert(db,'cost_identities',identity)
            return name,e
        def load(db):
            db.execute(text('SELECT 1'));amount=extracted.stat().st_size;original=v.validate_schema;observations=[]
            def observe(connection):
                setting=int(connection.exec_driver_sql("SELECT setting FROM pg_settings WHERE name='statement_timeout'").scalar_one());assert 0<setting<=120000;observations.append(setting);return original(connection)
            v.validate_schema=observe
            try:
                with extracted.open('rb') as stream:r.restore_priced_tables(db,stream,table_data=tuple(entries),budget=r.RestoreBudget(amount,monotonic()+120))
            finally:v.validate_schema=original
            assert len(observations)==2
        try:
            PHASE='roundtrip';_,e=target('roundtrip')
            with e.begin() as db:load(db);assert snapshot(db)==expected;assert db.execute(text("SELECT count(*) FROM pg_catalog.pg_class WHERE relpersistence='t' AND relname LIKE 'pe_stage_%'")).scalar_one()==0
            with e.connect() as db:assert snapshot(db)==expected
            print('priced-restore-actualPGCOPY-nonempty7-histories2and3-nullCurrent-exactAllRows-COMMIT-pass',flush=True)
            PHASE='deferred_commit';_,e=target('commit')
            with e.begin() as db:
                db.exec_driver_sql('CREATE TABLE owned_restore_business(value INTEGER NOT NULL)');db.exec_driver_sql("CREATE FUNCTION owned_restore_commit_fail() RETURNS trigger LANGUAGE plpgsql AS $$BEGIN RAISE EXCEPTION 'owned deferred fault' USING ERRCODE='23514'; END;$$");db.exec_driver_sql('CREATE CONSTRAINT TRIGGER owned_restore_commit_fault AFTER INSERT ON owned_restore_business DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION owned_restore_commit_fail()')
            with e.connect() as db:
                db.begin();db.execute(text('INSERT INTO owned_restore_business VALUES(1)'));load(db)
                try:db.commit()
                except DBAPIError as error:assert getattr(error.orig,'sqlstate',None)=='23514'
                else:raise AssertionError('Actual deferred COMMIT fault did not fire')
                db.connection.driver_connection.rollback();db.rollback()
            with e.connect() as db:assert raw_counts(db)==(0,)*7 and db.execute(text('SELECT count(*) FROM owned_restore_business')).scalar_one()==0 and db.execute(text('SELECT count(*) FROM cost_identities')).scalar_one()==1
            print('priced-restore-realDeferredCOMMIT23514-full7businessRollback-prerequisitePreserved-pass',flush=True)
            PHASE='process_interruption';name,e=target('kill')
            childenv={**env,'PRICED_RESTORE_CHILD_DATABASE':name,'PRICED_RESTORE_CHILD_STREAM':str(extracted),'PRICED_RESTORE_CHILD':'1'}
            process=subprocess.Popen([sys.executable,str(Path(__file__).resolve())],env=childenv,stdout=subprocess.PIPE,stderr=subprocess.PIPE,start_new_session=True,text=True)
            try:
                readable,_,_=select.select([process.stdout],[],[],45)
                if not readable or process.stdout.readline().strip()!='owned-priced-child-replay-staged':raise RuntimeError('Owned child did not reach post-write marker')
                os.killpg(process.pid,signal.SIGKILL);assert process.wait(timeout=10)==-signal.SIGKILL
            finally:
                if process.poll() is None:os.killpg(process.pid,signal.SIGKILL);process.wait(timeout=10)
                process.stdout.close();process.stderr.close()
            with e.connect() as db:assert raw_counts(db)==(0,)*7 and db.execute(text('SELECT count(*) FROM cost_identities')).scalar_one()==1
            with e.begin() as db:load(db);assert snapshot(db)==expected
            print('priced-restore-ownedSIGKILL-afterRealReplayWrite-fullRollback-freshLeaseRecovery-pass',flush=True)
            PHASE='lost_ack';_,e=target('ack')
            with e.connect() as db:
                db.begin();load(db);db.commit()
                try:raise RuntimeError('Owned definiteCOMMIT acknowledgement loss fixture')
                except RuntimeError:pass
            with e.connect() as db:assert snapshot(db)==expected;v.validate(db)
            print('priced-restore-realCOMMIT-lostACK-exactFreshObserver-noRepeatReplay-pass',flush=True)
            print('priced-estimate-restore-native-pass',flush=True)
        finally:
            for name,e in reversed(targets):
                e.dispose()
                with admin.connect().execution_options(isolation_level='AUTOCOMMIT') as db:db.exec_driver_sql('DROP DATABASE "'+name+'" WITH (FORCE)')
            admin.dispose()

if __name__=='__main__':
    try:
        if os.environ.get('PRICED_RESTORE_CHILD')=='1':child()
        else:main()
    except BaseException as error:
        print('priced-estimate-restore-native-failure '+json.dumps({'phase':PHASE,'class':type(error).__name__,'sqlstate':getattr(error,'sqlstate',getattr(getattr(error,'orig',None),'sqlstate',None)),'frames':[{'file':Path(f.filename).name,'function':f.name,'line':f.lineno} for f in traceback.extract_tb(error.__traceback__)[-5:]]}),flush=True)
        raise RuntimeError('Owned priced restore probe failed; sanitized classification above') from None
