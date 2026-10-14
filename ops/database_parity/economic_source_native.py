"""Owned full bootstrap and prospective source factories on native PostgreSQL."""
import os,json,tempfile
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import sleep
from uuid import uuid4
import importlib
from sqlalchemy import create_engine,text,event
from sqlalchemy.orm import Session
from core.schema import bootstrap_database
from core.economic_identity import _add_created
from core.economic_source_creation import _create_spool,_create_attempt,_validate_existing_source


def main():
    engine=create_engine(os.environ['ECONOMIC_SOURCE_NATIVE_URL'],hide_parameters=True)
    bootstrap_database(engine)
    M=importlib.import_module('core.schema.migrations.022_economic_source_lifetimes')
    with engine.connect() as c:
        print('history-checks-json='+json.dumps(dict(c.execute(text("SELECT conname,pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid='cost_source_lifetimes'::regclass AND contype='c' ORDER BY conname")).all())))
        assert c.execute(text('SELECT count(*) FROM cost_source_lifetimes')).scalar_one()==0
    from modules.orders.models import Order,OrderItem,Product
    from modules.jobs.models import Job
    from modules.printers.models import Printer
    from modules.inventory.models import FilamentLibrary
    with Session(engine) as db:
        product=Product(name='Synthetic prospective source',org_id=7)
        printer=Printer(name='Synthetic',org_id=7,api_type='bambu')
        filament=FilamentLibrary(brand='Synthetic',name='Owned',material='PLA')
        db.add_all([product,printer,filament]);db.flush()
        printer_id,filament_id=printer.id,filament.id
        order=_add_created(db,Order(org_id=7),'order',owner_org_id=7)
        item=_add_created(db,OrderItem(order_id=order.id,product_id=product.id),'item',owner_org_id=7)
        job=_add_created(db,Job(order_item_id=item.id,charged_to_org_id=7,item_name='Synthetic source'),'job',owner_org_id=7)
        job_id=job.id;db.commit()
    values={'org_id':7,'filament_id':filament_id,'price':0,'initial_weight_g':1000,'status':'active'}
    attempt_values={'printer_id':printer_id,'scheduled_job_id':job_id,'status':'running','started_at':'2026-10-13T12:00:00Z'}
    with Session(engine) as db:
        db.begin();first=_create_spool(db,values,owner_org_id=7);known=_create_attempt(db,attempt_values);db.commit()
    with engine.begin() as c:
        assert _validate_existing_source(c,'spool',first.id)==first
        assert _validate_existing_source(c,'attempt',known.id)==known
        second=_create_spool(c,values,owner_org_id=7)
        unknown=_create_attempt(c,{k:v for k,v in attempt_values.items() if k!='scheduled_job_id'})
        assert unknown.identity_uid is None and unknown.state=='unresolved'
    # Existing PostgreSQL DBAPI adapter is exercised against the owned pool.
    with tempfile.TemporaryDirectory(prefix='source-native-') as folder:
        password=Path(folder)/'private-runtime-secret';password.write_text(uuid4().hex);password.chmod(0o600)
        os.environ['DATABASE_URL']='postgresql://postgres@pg/postgres'
        os.environ['DATABASE_PASSWORD_FILE']=str(password)
        from core.db_utils import _ConnectionAdapter
        raw=engine.raw_connection();adapter=_ConnectionAdapter(raw,True)
        try:
            adapter.execute('BEGIN')
            third=_create_spool(adapter,values,owner_org_id=7)
            created=_create_attempt(adapter,attempt_values)
            assert _validate_existing_source(adapter,'attempt',created.id)==created
            adapter.commit()
        finally:raw.close()
    with engine.connect() as c:
        c.begin();nonce=uuid4().hex
        c.execute(text('INSERT INTO spools(filament_id,org_id,economic_birth_nonce) VALUES(:f,7,:n)'),{'f':filament_id,'n':nonce})
        try:c.commit()
        except Exception:c.rollback()
        else:raise AssertionError('Deferred missing-history commit was admitted')
    with engine.begin() as c:
        c.execute(text("UPDATE print_jobs SET status='failed',spool_snapshot='original' WHERE id=:id"),{'id':unknown.id})
        c.execute(text('DELETE FROM print_jobs WHERE id=:id'),{'id':unknown.id})
        c.execute(text("SELECT setval(pg_get_serial_sequence('print_jobs','id'),:id,false)"),{'id':unknown.id})
        reused=_create_attempt(c,{k:v for k,v in attempt_values.items() if k!='scheduled_job_id'})
        assert reused.id==unknown.id and reused.birth_nonce!=unknown.birth_nonce
    with engine.begin() as c:
        unresolved_spool=_create_spool(c,values)
        c.execute(text('UPDATE spools SET org_id=9 WHERE id=:id'),{'id':unresolved_spool.id})
        c.execute(text('UPDATE print_jobs SET scheduled_job_id=NULL WHERE id=:id'),{'id':reused.id})
        assert c.execute(text('SELECT owner_scope,identity_uid FROM cost_source_lifetimes WHERE birth_nonce=:n'),{'n':unresolved_spool.birth_nonce}).one()==(None,None)
    with engine.connect() as c:
        for table,identifier in (('spools',unresolved_spool.id),('print_jobs',reused.id)):
            try:c.execute(text(f'UPDATE {table} SET id=101 WHERE id=:id'),{'id':identifier})
            except Exception:c.rollback()
            else:raise AssertionError('Unresolved source ID reassignment admitted')
        negative=[("UPDATE print_jobs SET economic_birth_nonce=:n WHERE id=:id",{'n':unknown.birth_nonce,'id':reused.id}),
                  ("INSERT INTO print_jobs(printer_id,status,started_at,economic_birth_nonce) VALUES(:p,'running',CURRENT_TIMESTAMP,:n)",{'p':printer_id,'n':unknown.birth_nonce}),
                  ("UPDATE cost_source_lifetimes SET state='known' WHERE birth_nonce=:n",{'n':reused.birth_nonce}),
                  ("DELETE FROM cost_source_lifetimes WHERE birth_nonce=:n",{'n':unknown.birth_nonce}),
                  ("UPDATE spools SET economic_uid=:uid WHERE id=:id",{'uid':known.identity_uid,'id':second.id})]
        for statement,parameters in negative:
            try:c.execute(text(statement),parameters)
            except Exception:c.rollback()
            else:raise AssertionError('Invalid source/history mutation admitted')
    for prefix in ('INSERT INTO spools','INSERT INTO cost_identities','INSERT INTO cost_source_lifetimes','UPDATE spools SET economic_uid'):
        with engine.connect() as c:before=c.execute(text('SELECT count(*) FROM cost_source_lifetimes')).scalar_one()
        def fail(conn,cursor,statement,parameters,context,many):
            if statement.startswith(prefix):raise RuntimeError('Synthetic creation step failure')
        event.listen(engine,'before_cursor_execute',fail)
        try:
            with engine.begin() as c:
                try:_create_spool(c,values,owner_org_id=7)
                except RuntimeError:pass
                else:raise AssertionError('Creation failure expected')
        finally:event.remove(engine,'before_cursor_execute',fail)
        with engine.connect() as c:assert c.execute(text('SELECT count(*) FROM cost_source_lifetimes')).scalar_one()==before
    entered,attempted,release=Event(),Event(),Event();pid=[]
    def held():
        with engine.begin() as c:
            result=_validate_existing_source(c,'spool',first.id);entered.set();assert release.wait(5);return result
    def waiting():
        assert entered.wait(5)
        with engine.begin() as c:
            pid.append(c.execute(text('SELECT pg_backend_pid()')).scalar_one());attempted.set()
            return _validate_existing_source(c,'spool',first.id)
    with ThreadPoolExecutor(max_workers=2) as pool:
        a=pool.submit(held);b=pool.submit(waiting)
        try:
            assert attempted.wait(5)
            for _ in range(40):
                with engine.connect() as c:wait=c.execute(text('SELECT wait_event_type FROM pg_stat_activity WHERE pid=:pid'),{'pid':pid[0]}).scalar_one()
                if wait=='Lock':break
                sleep(.05)
            assert wait=='Lock' and not b.done()
        finally:release.set()
        assert a.result()==b.result()==first
    with engine.begin() as c:M.validate(c)
    bootstrap_database(engine)
    # Physical enforcement/adoption mutations must fail before repair/writes.
    negative=[
        "ALTER TABLE cost_source_lifetimes DROP CONSTRAINT cost_source_lifetimes_state_check",
        "ALTER TABLE cost_source_lifetimes DROP CONSTRAINT cost_source_lifetimes_state_check; ALTER TABLE cost_source_lifetimes ADD CONSTRAINT cost_source_lifetimes_state_check CHECK (state IS NOT NULL)",
        "ALTER TABLE spools ALTER CONSTRAINT spools_economic_birth_nonce_fkey NOT DEFERRABLE",
        "ALTER TABLE cost_source_lifetimes ALTER CONSTRAINT cost_source_lifetimes_identity_uid_resource_kind_owner_scope__fkey DEFERRABLE INITIALLY DEFERRED",
        "ALTER TABLE spools DISABLE TRIGGER spools_economic_birth_guard",
    ]
    # Use the actual canonical auto-generated FK name, not a guessed truncation.
    with engine.connect() as c:
        name=c.execute(text("SELECT conname FROM pg_constraint WHERE conrelid='cost_source_lifetimes'::regclass AND contype='f' AND confrelid='cost_identities'::regclass")).scalar_one()
    negative[3]='ALTER TABLE cost_source_lifetimes ALTER CONSTRAINT '+name+' DEFERRABLE INITIALLY DEFERRED'
    for statement in negative:
        with engine.connect() as c:
            c.exec_driver_sql(statement)
            try:M.validate(c)
            except Exception:c.rollback()
            else:raise AssertionError('Weakened physical adoption shape admitted')
    with engine.begin() as c:M.validate(c)
    engine.dispose()
    print('economic-source-native-pass: actual bootstrap/source factories/session/connection/PGadapter/deferred FK/unknown retirement/reuse/step rollback/real Lock wait/replay')

if __name__=='__main__':main()
