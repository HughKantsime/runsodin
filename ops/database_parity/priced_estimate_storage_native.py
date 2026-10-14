"""Owned unregistered estimate storage catalog/concurrency; no HTTP authority."""
import os,stat,tempfile,secrets,json,traceback,re
from pathlib import Path
from uuid import uuid4
PHASE='bootstrap'


def main():
    global PHASE
    secret=Path(os.environ['PRICED_ESTIMATE_NATIVE_PASSWORD_FILE'])
    assert os.getuid()==10001 and secret.stat().st_uid==10001 and stat.S_IMODE(secret.stat().st_mode)==0o600 and not secret.is_symlink()
    with tempfile.TemporaryDirectory(prefix='owned-priced-estimate-') as folder:
        os.environ.update(DATABASE_URL='postgresql+psycopg://postgres@pg/postgres',DATABASE_PASSWORD_FILE=str(secret),ODIN_DATA_DIR=folder,JWT_SECRET_KEY=secrets.token_urlsafe(48))
        from sqlalchemy import text
        from sqlalchemy.orm import Session
        from sqlalchemy.exc import DBAPIError
        from core.db import engine
        from core.db_compat import IS_POSTGRES
        from core.schema import bootstrap_database
        from core.economic_identity import _add_created
        from modules.jobs.models import Job
        from core import priced_estimate_schema as s,priced_estimate_validation as v
        from core.priced_job_estimate import calculate_priced_job_estimate
        assert IS_POSTGRES and engine.dialect.name=='postgresql'
        bootstrap_database(engine,Path('/source/backend'))
        def insert(db,table,row):return db.execute(text('INSERT INTO public.'+table+'('+','.join(row)+') VALUES('+','.join(':'+n for n in row)+')'),row)
        def update(db,table,row,where,params):return db.execute(text('UPDATE public.'+table+' SET '+','.join(n+'=:'+n for n in row)+' WHERE '+where),{**params,**row})
        def expect(function,state):
            try:function()
            except DBAPIError as error:
                actual=getattr(error.orig,'sqlstate',None)
                if actual!=state:raise AssertionError('Unexpected closed SQLstate '+str(actual)) from None
            else:raise AssertionError('Expected closed SQLstate '+state)
        def rollback(db):
            db.connection.driver_connection.rollback();db.rollback()
        def bundle():
            values={k:'0' for k in s.ACTIVE_FIELDS};values.update(printer_lifespan='1',parts_per_month='1')
            return dict(currency='USD',assumptions=values,material_rates={'PLA':dict(currency='USD',cost_per_gram='2')})
        now='2026-10-13T12:00:00.000000Z'
        def source(db,scope='org',key=1,uid=None,revision=1):
            payload=bundle();row=dict(source_uid=uid or uuid4().hex,revision=revision,owner_scope=scope,owner_key=key,currency='USD',assumptions_json=v.canonical_json(payload),source_sha256=v.digest(payload),assumptions_kind='explicit_saved',actor_id=7,created_at=now)
            insert(db,s.SOURCE,row);return row
        def selection(db,src,scope='org',key=1,revision=1):
            row=dict(owner_scope=scope,owner_key=key,revision=revision,source_uid=src['source_uid'],source_revision=src['revision'],source_owner_scope=src['owner_scope'],source_owner_key=src['owner_key'],currency='USD',actor_binding_uid=uuid4().hex,actor_id=7,command_nonce=uuid4().hex,expected_revision=revision-1,created_at=now)
            row['request_sha256']=v.digest(v.selection_command(row));insert(db,s.SELECTION_HISTORY,row)
            if revision==1:insert(db,s.SELECTION,{k:row[k] for k in ('owner_scope','owner_key','revision')})
            else:update(db,s.SELECTION,dict(revision=revision),'owner_scope=:scope AND owner_key=:key',{'scope':scope,'key':key})
            return row
        def birth(db,scope='org',key=1):
            # Actual frozen ORM and sanctioned021 allocation, no fake registryUID.
            with Session(bind=db) as session:
                job=_add_created(session,Job(item_name='Owned prospective estimate',quantity=3,charged_to_org_id=key if scope=='org' else None),'job',owner_org_id=key if scope=='org' else None,global_authority=scope=='global')
                uid=session.execute(text('SELECT economic_uid FROM jobs WHERE id=:id'),{'id':job.id}).scalar_one()
                assert len(uid)==36 and str(__import__('uuid').UUID(uid))==uid
                return uid
        def receipt(db,src,h,job,revision=1,omit=(),classification='job_estimate'):
            inputs=dict(material_key='PLA',grams_per_run='1',hours_per_run='0',runs=3,payload_sha256=None,plate_index=None,piece_count=None,capacity=None,model_fingerprint=None,file_fingerprint=None)
            row=dict(receipt_uid=uuid4().hex,job_uid=job,job_kind='job',owner_scope=h['owner_scope'],owner_key=h['owner_key'],receipt_revision=revision,classification=classification,formula='per-run-cost-v1',rounding='HALF_EVEN',amount='6.00',runs=3,required_total_name='total',unknown_reason=None,selection_owner_scope=h['owner_scope'],selection_owner_key=h['owner_key'],selection_revision=h['revision'],input_sha256=v.digest(inputs),input_json=v.canonical_json(inputs),payload_sha256=None,plate_index=None,piece_count=None,capacity=None,created_at=now,**{n:h[n] for n in s.SOURCE_LINK})
            insert(db,s.RECEIPT,row)
            payload=bundle();payload['assumptions'].update(spool_cost='0',spool_weight='1')
            result=calculate_priced_job_estimate(payload,material_key='PLA',grams_per_run='1',hours_per_run='0',runs=3)
            for c in result.components:
                if c.name not in omit:insert(db,s.COMPONENT,dict(receipt_uid=row['receipt_uid'],component_name=c.name,currency=c.currency,basis=c.basis,predecessor_name=s.PREDECESSOR[c.name],numerator=str(c.numerator),denominator=str(c.denominator)))
            return row
        def reference_row(r,revision=1,clear=False):
            row=dict(job_uid=r['job_uid'],owner_scope=r['owner_scope'],owner_key=r['owner_key'],revision=revision,receipt_uid=None if clear else r['receipt_uid'],invalidation_reason='explicit_clear' if clear else None,current_input_sha256=None if clear else r['input_sha256'],actor_binding_uid=uuid4().hex,actor_id=7,command_nonce=uuid4().hex,expected_revision=revision-1,created_at=now)
            row['request_sha256']=v.digest(v.reference_command(row));return row
        def reference(db,r,revision=1,clear=False):
            row=reference_row(r,revision,clear);insert(db,s.REFERENCE_HISTORY,row)
            names=('job_uid','owner_scope','owner_key','revision','receipt_uid','invalidation_reason','current_input_sha256')
            if revision==1:insert(db,s.CURRENT,{n:row[n] for n in names})
            else:update(db,s.CURRENT,{n:row[n] for n in names if n!='job_uid'},'job_uid=:uid',{'uid':r['job_uid']})
            return row
        with engine.begin() as db:
            db.execute(text("INSERT INTO groups(id,name,is_org) VALUES(1,'Owned SQL fixture',true),(2,'Owned foreign SQL fixture',true)"))
            s.apply(db)
            if os.environ.get('PRICED_ESTIMATE_DIAGNOSTIC')=='1':
                from core.schema.migrator import MigrationError
                try:v.validate(db)
                except MigrationError as error:
                    frames=traceback.extract_tb(error.__traceback__)
                    failed=next((f for f in reversed(frames) if f.name=='validate_schema'),None)
                    actual=db.execute(text("SELECT t.tgname,t.tgtype,pg_get_triggerdef(t.oid) FROM pg_catalog.pg_trigger t JOIN pg_catalog.pg_class c ON c.oid=t.tgrelid WHERE c.relnamespace='public'::regnamespace AND c.relname=ANY(:tables) AND NOT t.tgisinternal ORDER BY t.tgname"),{'tables':list(s.TABLES)}).all()
                    assert len(actual)==14 and {row[0] for row in actual}==set(s.pg_triggers())
                    metadata=[]
                    for name,bits,definition in actual:
                        expected=s.pg_triggers()[name]
                        events=lambda sql:re.search(r' BEFORE (.*?) ON ',sql)[1].split(' OR ')
                        metadata.append(dict(name=name,event_bits=bits,expected_bits=34 if name.endswith('_truncate_guard') else 31,actual_events=events(definition),expected_events=events(expected),exact_comparison_matches=re.sub(r'\s+','',definition).replace('public.','')==re.sub(r'\s+','',expected).replace('public.','')))
                    print('priced-estimate-catalog-diagnostic '+json.dumps(dict(comparison='trigger_definition',function=failed.name if failed else None,line=failed.lineno if failed else None,triggers=metadata)),flush=True)
                    print('priced-estimate-storage-catalog-diagnostic-complete',flush=True)
                    return
                else:raise AssertionError('Expected preserved strict catalog refusal not reproduced')
            v.validate(db)
        print('priced-estimate-actualPG-catalog-CHECK-key-index-OID-function-trigger-pass',flush=True)
        PHASE='complete_graph'
        with engine.begin() as db:
            src=source(db);h=selection(db,src);job=birth(db);r=receipt(db,src,h,job);ref=reference(db,r);v.validate(db)
            global_src=source(db,'global',0);global_h=selection(db,global_src,'global',0);global_job=birth(db,'global',0);global_r=receipt(db,global_src,global_h,global_job);reference(db,global_r);v.validate(db)
        with engine.connect() as db:assert db.execute(text('SELECT count(*) FROM public.'+s.COMPONENT)).scalar_one()==22
        print('priced-estimate-genuine021-org-global-UUID36-fullSameTX-receipt11-current-pass',flush=True)
        PHASE='component_commit'
        for name in s.NAMES:
            with engine.connect() as db:
                db.begin();before=db.execute(text('SELECT count(*) FROM jobs')).scalar_one();job=birth(db);receipt(db,src,h,job,omit=(name,));expect(db.commit,'23503');rollback(db)
                assert db.execute(text('SELECT count(*) FROM jobs')).scalar_one()==before
        print('priced-estimate-all11-realDeferredCOMMIT-missingComponent-jobGraphRollback-pass',flush=True)
        PHASE='guards'
        with engine.begin() as db:
            for table in s.TABLES:
                for command in ('TRUNCATE public.'+table+' CASCADE','DELETE FROM public.'+table):
                    try:
                        with db.begin_nested():db.execute(text(command))
                    except DBAPIError as error:assert getattr(error.orig,'sqlstate',None)=='23514'
                    else:raise AssertionError('Immutable command accepted')
            foreign=source(db,'org',2)
            try:
                with db.begin_nested():selection(db,foreign,revision=2)
            except DBAPIError as error:assert getattr(error.orig,'sqlstate',None)=='23514'
            else:raise AssertionError('Foreign selection accepted')
            scenario_job=birth(db);scenario=receipt(db,src,h,scenario_job,classification='scenario')
            try:
                with db.begin_nested():reference(db,scenario)
            except DBAPIError as error:assert getattr(error.orig,'sqlstate',None)=='23514'
            else:raise AssertionError('Scenario current accepted')
            reference(db,r,2,True)
            try:
                with db.begin_nested():update(db,s.CURRENT,dict(revision=1),'job_uid=:uid',{'uid':r['job_uid']})
            except DBAPIError as error:assert getattr(error.orig,'sqlstate',None)=='23514'
            else:raise AssertionError('Current rewind accepted')
            v.validate(db)
        print('priced-estimate-immutableTRUNCATE-delete-foreignOwner-scenario-currentCAS-pass',flush=True)
        PHASE='direct_sequences'
        with engine.begin() as db:
            seq_source=source(db);seq_job=birth(db)
        # Direct SQL contenders, no caller membership/table fences. Both physical
        # connections independently start transactions. First retains advisory
        # key until rollback; second actual55P03, then release allows revision1/2.
        for domain in ('source','selection','receipt','reference'):
            for order in (0,1):
                with engine.connect() as first,engine.connect() as second:
                    first.begin();second.begin()
                    leader,follower=(first,second) if order==0 else (second,first)
                    if domain=='source':
                        action=lambda db:source(db,uid=seq_source['source_uid'],revision=2)
                    elif domain=='selection':
                        # New selecting owner uses a globally qualified source.
                        action=lambda db:selection(db,global_src,'org',2,1)
                    elif domain=='receipt':
                        action=lambda db:receipt(db,src,h,seq_job)
                    else:
                        action=lambda db:insert(db,s.REFERENCE_HISTORY,reference_row(r,3,True))
                    action(leader);expect(lambda:action(follower),'55P03')
                    follower.rollback();leader.rollback()
                    follower.begin();action(follower);follower.rollback()
            print('priced-estimate-directSequence-'+domain+'-bothOrders55P03-releaseRollback-pass',flush=True)
        PHASE='catalog_negatives'
        with engine.connect() as db:
            db.begin();db.execute(text('ALTER TABLE public.'+s.SOURCE+' DISABLE TRIGGER '+s.SOURCE+'_row_guard'))
            try:v.validate(db)
            except Exception as error:
                from core.schema.migrator import MigrationError
                assert isinstance(error,MigrationError)
            else:raise AssertionError('Disabled guard accepted')
            db.rollback()
        with engine.begin() as db:
            db.execute(text('ALTER TABLE public.'+s.COMPONENT+' DISABLE TRIGGER '+s.COMPONENT+'_row_guard'))
            db.execute(text("UPDATE public."+s.COMPONENT+" SET numerator='7' WHERE receipt_uid=:uid AND component_name='total'"),{'uid':r['receipt_uid']})
            try:v.validate_data(db)
            except Exception as error:
                from core.schema.migrator import MigrationError
                assert isinstance(error,MigrationError)
            else:raise AssertionError('Arithmetic tamper accepted')
            # Whole owned corruption experiment must roll back, not persist.
            db.rollback()
        print('priced-estimate-disabledGuard-guardlessArithmeticRefusal-pass',flush=True)
        PHASE='lost_ack'
        with engine.connect() as db:
            db.begin();saved=source(db)
            db.commit()
            try:raise RuntimeError('Owned definiteCOMMIT lost acknowledgement fixture')
            except RuntimeError:pass
        with engine.connect() as observer:
            row=observer.execute(text('SELECT source_sha256 FROM public.'+s.SOURCE+' WHERE source_uid=:uid AND revision=1'),{'uid':saved['source_uid']}).scalar_one();assert row==saved['source_sha256'];v.validate(observer)
        print('priced-estimate-realCOMMIT-lostACK-exactFreshObserver-noRecalculation-pass',flush=True)
        print('priced-estimate-storage-native-pass',flush=True)

if __name__=='__main__':
    try:main()
    except BaseException as error:
        print('priced-estimate-storage-native-failure '+json.dumps({'phase':PHASE,'class':type(error).__name__,'sqlstate':getattr(getattr(error,'orig',None),'sqlstate',None),'frames':[{'file':Path(f.filename).name,'function':f.name,'line':f.lineno} for f in traceback.extract_tb(error.__traceback__)[-5:]]}),flush=True)
        raise RuntimeError('Owned priced storage probe failed; sanitized classification above') from None
