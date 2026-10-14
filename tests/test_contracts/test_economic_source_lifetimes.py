"""Prospective source births across four caller-owned transaction adapters."""
import importlib
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import sleep
from uuid import uuid4
import pytest
from sqlalchemy import create_engine,text,event
from sqlalchemy.orm import Session
from core.economic_source_creation import _create_spool,_create_attempt,_validate_existing_source,SourceCreationError

M=importlib.import_module('core.schema.migrations.022_economic_source_lifetimes')
F=importlib.import_module('core.schema.migrations.009_cost_foundation')
I=importlib.import_module('core.schema.migrations.021_economic_identity')

@pytest.fixture
def engine(tmp_path):
    e=create_engine('sqlite:///'+str(tmp_path/'birth.db'),connect_args={'timeout':5})
    @event.listens_for(e,'connect')
    def foreign_keys(connection,record):connection.execute('PRAGMA foreign_keys=ON')
    with e.begin() as c:
        for ddl in ('CREATE TABLE orders(id INTEGER PRIMARY KEY,org_id INTEGER)',
                    'CREATE TABLE order_items(id INTEGER PRIMARY KEY,order_id INTEGER)',
                    'CREATE TABLE jobs(id INTEGER PRIMARY KEY,order_item_id INTEGER,charged_to_org_id INTEGER)',
                    'CREATE TABLE spools(id INTEGER PRIMARY KEY,org_id INTEGER,price REAL,initial_weight_g REAL)',
                    'CREATE TABLE print_jobs(id INTEGER PRIMARY KEY,printer_id INTEGER,scheduled_job_id INTEGER,status TEXT,spool_snapshot TEXT)'):
            c.exec_driver_sql(ddl)
        F.apply(c);I.apply(c);M.apply(c)
    yield e
    e.dispose()


def graph(c):
    from core.economic_identity import _add_created
    from tests.test_contracts.test_prospective_economic_identity import Order,Item,Job
    with Session(bind=c) as s:
        o=_add_created(s,Order(org_id=7),'order',owner_org_id=7)
        i=_add_created(s,Item(order_id=o.id),'item',owner_org_id=7)
        j=_add_created(s,Job(order_item_id=i.id,charged_to_org_id=7),'job',owner_org_id=7)
        return j.id


def counts(c):
    return tuple(c.exec_driver_sql('SELECT count(*) FROM '+table).scalar_one() for table in ('spools','print_jobs','cost_source_lifetimes','cost_identities','cost_attempt_bindings'))


@pytest.mark.parametrize('style',['connection','session','sqlite_dbapi'])
def test_actual_factory_known_unknown_and_replay(engine,style):
    with engine.begin() as c:job=graph(c)
    if style=='sqlite_dbapi':
        executor=sqlite3.connect(engine.url.database);executor.execute('PRAGMA foreign_keys=ON');executor.execute('BEGIN IMMEDIATE')
    elif style=='session':
        executor=Session(engine);executor.begin()
    else:executor=engine.connect();executor.begin()
    spool=_create_spool(executor,{'org_id':7,'price':0,'initial_weight_g':1000},owner_org_id=7)
    known=_create_attempt(executor,{'printer_id':1,'scheduled_job_id':job,'status':'running'})
    unknown=_create_attempt(executor,{'printer_id':1,'status':'running'})
    assert spool.state==known.state=='known' and unknown.state=='unresolved'
    assert unknown.identity_uid is None
    assert _validate_existing_source(executor,'attempt',known.id)==known
    executor.commit();executor.close()
    with engine.begin() as c:
        M.validate(c)
        assert c.exec_driver_sql('SELECT owner_scope,owner_key,identity_uid,job_uid FROM cost_source_lifetimes WHERE state=\'unresolved\'').one()==(None,None,None,None)


def test_deferred_fk_missing_history_cannot_commit(engine):
    with engine.connect() as c:
        c.begin()
        c.exec_driver_sql('INSERT INTO spools(org_id,economic_birth_nonce) VALUES(7,?)',(uuid4().hex,))
        assert c.exec_driver_sql('SELECT count(*) FROM spools').scalar_one()==1
        with pytest.raises(Exception):c.commit()
        c.rollback()
    with engine.connect() as c:assert counts(c)==(0,0,0,0,0)


def test_raw_lifetime_wrong_kind_id_owner_and_unknown_metadata_refused(engine):
    with engine.connect() as c:
        for change in ({'resource_kind':'attempt'},{'resource_id':99},{'state':'known','identity_uid':str(uuid4()),'owner_scope':'org','owner_key':9},{'owner_key':7}):
            nonce=uuid4().hex;c.exec_driver_sql('INSERT INTO spools(org_id,economic_birth_nonce) VALUES(7,?)',(nonce,))
            identifier=c.exec_driver_sql('SELECT id FROM spools').scalar_one()
            record={'birth_nonce':nonce,'resource_kind':'spool','resource_id':identifier,'state':'unresolved','identity_uid':None,'owner_scope':None,'owner_key':None,**change}
            with pytest.raises(Exception):c.execute(text('INSERT INTO cost_source_lifetimes('+','.join(record)+') VALUES('+','.join(':'+k for k in record)+')'),record)
            c.rollback()


def test_legacy_null_and_unknown_first_start_cannot_promote(engine):
    with engine.begin() as c:
        c.exec_driver_sql('INSERT INTO spools(org_id) VALUES(7)')
        unknown=_create_attempt(c,{'printer_id':1,'status':'running'})
        c.exec_driver_sql('UPDATE print_jobs SET spool_snapshot=\'original\' WHERE id=?',(unknown.id,))
    with engine.connect() as c:
        for sql,args in [('UPDATE spools SET economic_birth_nonce=?',(uuid4().hex,)),('UPDATE cost_source_lifetimes SET state=\'known\'',()),('UPDATE print_jobs SET economic_uid=?',(str(uuid4()),))]:
            with pytest.raises(Exception):c.exec_driver_sql(sql,args)
            c.rollback()
        assert c.exec_driver_sql('SELECT economic_uid,economic_birth_nonce FROM spools').one()==(None,None)
        assert c.exec_driver_sql('SELECT spool_snapshot,economic_uid FROM print_jobs').one()==('original',None)


@pytest.mark.parametrize('known',[False,True])
def test_retire_numeric_reuse_and_nonce_uid_resurrection_refused(engine,known):
    with engine.begin() as c:
        first=_create_spool(c,{'org_id':7},**({'owner_org_id':7} if known else {}))
    with engine.begin() as c:c.exec_driver_sql('DELETE FROM spools WHERE id=?',(first.id,))
    with engine.begin() as c:
        fresh=_create_spool(c,{'org_id':7},**({'owner_org_id':7} if known else {}))
        assert fresh.id==first.id and fresh.birth_nonce!=first.birth_nonce
        assert c.exec_driver_sql('SELECT count(*) FROM cost_source_lifetimes').scalar_one()==2
    with engine.connect() as c:
        for sql,args in [('INSERT INTO spools(id,org_id,economic_birth_nonce) VALUES(99,7,?)',(first.birth_nonce,)),('UPDATE spools SET economic_birth_nonce=?',(first.birth_nonce,)),('INSERT OR REPLACE INTO spools(id,org_id) VALUES(?,7)',(fresh.id,)),('INSERT OR REPLACE INTO cost_source_lifetimes SELECT * FROM cost_source_lifetimes LIMIT 1',())]:
            c.exec_driver_sql('PRAGMA recursive_triggers=OFF')
            with pytest.raises(Exception):c.exec_driver_sql(sql,args)
            c.rollback()
        if first.identity_uid:
            with pytest.raises(Exception):c.exec_driver_sql('UPDATE spools SET economic_uid=?',(first.identity_uid,))
            c.rollback()


@pytest.mark.parametrize('prefix',['INSERT INTO spools','INSERT INTO cost_identities','INSERT INTO cost_source_lifetimes','UPDATE spools SET economic_uid'])
def test_factory_failure_each_step_savepoint_rolls_back_even_caller_commit(engine,prefix):
    def fail(conn,cursor,statement,parameters,context,many):
        if statement.startswith(prefix):raise RuntimeError('Synthetic source-write failure')
    event.listen(engine,'before_cursor_execute',fail)
    try:
        with engine.begin() as c:
            with pytest.raises(RuntimeError):_create_spool(c,{'org_id':7},owner_org_id=7)
    finally:event.remove(engine,'before_cursor_execute',fail)
    with engine.connect() as c:assert counts(c)==(0,0,0,0,0)


def test_caller_rollback_and_invalid_authority(engine):
    with engine.connect() as c:
        c.begin();_create_spool(c,{'org_id':7},owner_org_id=7);c.rollback()
        c.begin()
        for args in ({'owner_org_id':8},{'owner_org_id':None},{'owner_org_id':False}):
            with pytest.raises(SourceCreationError):_create_spool(c,{'org_id':7},**args)
        c.rollback()
    with engine.connect() as c:assert counts(c)==(0,0,0,0,0)


def test_migration_adoption_drift_and_partial_no_mutation(engine):
    with engine.begin() as c:M.apply(c)
    with engine.begin() as c:c.exec_driver_sql('DROP TRIGGER spools_economic_birth_update')
    with pytest.raises(Exception):
        with engine.begin() as c:M.apply(c)


def test_unstarted_executor_refuses(engine):
    with engine.connect() as c:
        with pytest.raises(SourceCreationError,match='transaction'):_create_spool(c,{'org_id':7},owner_org_id=7)


def test_concurrent_replay_held_source_lock(engine):
    with engine.begin() as c:born=_create_spool(c,{'org_id':7},owner_org_id=7)
    entered,release=Event(),Event()
    def held():
        with engine.begin() as c:
            result=_validate_existing_source(c,'spool',born.id);entered.set();assert release.wait(5);return result
    def waiting():
        assert entered.wait(5)
        with engine.begin() as c:return _validate_existing_source(c,'spool',born.id)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first=pool.submit(held);second=pool.submit(waiting)
        assert entered.wait(5);sleep(.2);assert not second.done();release.set()
        assert first.result()==second.result()==born


@pytest.mark.parametrize('prefix',['INSERT INTO print_jobs','INSERT INTO cost_identities','INSERT INTO cost_attempt_bindings','INSERT INTO cost_source_lifetimes','UPDATE print_jobs SET economic_uid'])
def test_attempt_rollback_each_step_including_binding(engine,prefix):
    with engine.begin() as c:job=graph(c)
    with engine.connect() as c:before=counts(c)
    def fail(conn,cursor,statement,parameters,context,many):
        if statement.startswith(prefix):raise RuntimeError('Synthetic attempt step failure')
    event.listen(engine,'before_cursor_execute',fail)
    try:
        with engine.begin() as c:
            with pytest.raises(RuntimeError):_create_attempt(c,{'printer_id':1,'scheduled_job_id':job,'status':'running'})
    finally:event.remove(engine,'before_cursor_execute',fail)
    with engine.connect() as c:assert counts(c)==before


def test_known_attempt_owner_binding_and_reassignment_guards(engine):
    with engine.begin() as c:
        job=graph(c);other=graph(c)
        born=_create_attempt(c,{'printer_id':1,'scheduled_job_id':job,'status':'running'})
        c.exec_driver_sql('UPDATE print_jobs SET spool_snapshot=\'original\' WHERE id=?',(born.id,))
    with engine.connect() as c:
        for statement,args in [('UPDATE print_jobs SET scheduled_job_id=? WHERE id=?',(other,born.id)),('UPDATE print_jobs SET economic_uid=NULL WHERE id=?',(born.id,)),('UPDATE spools SET economic_birth_nonce=?',(uuid4().hex,))]:
            if statement.startswith('UPDATE spools'):continue
            with pytest.raises(Exception):c.exec_driver_sql(statement,args)
            c.rollback()
    with engine.begin() as c:
        c.exec_driver_sql('UPDATE jobs SET charged_to_org_id=9 WHERE economic_uid IS NULL')
        M.validate(c)


def test_legacy_job_root_produces_retained_unknown_without_invented_owner(engine):
    with engine.begin() as c:
        c.exec_driver_sql('INSERT INTO orders(org_id) VALUES(7)')
        c.exec_driver_sql('INSERT INTO order_items(order_id) VALUES(1)')
        c.exec_driver_sql('INSERT INTO jobs(order_item_id,charged_to_org_id) VALUES(1,7)')
        born=_create_attempt(c,{'printer_id':1,'scheduled_job_id':1,'status':'running'})
        assert born.identity_uid is None
        assert c.exec_driver_sql('SELECT owner_scope,owner_key,identity_uid FROM cost_source_lifetimes').one()==(None,None,None)
        assert c.exec_driver_sql('SELECT count(*) FROM cost_identities').scalar_one()==0


def test_unenforced_sqlite_caller_refused_before_mutation(engine):
    raw=sqlite3.connect(engine.url.database)
    try:
        raw.execute('BEGIN IMMEDIATE')
        with pytest.raises(SourceCreationError,match='foreign-key'):_create_spool(raw,{'org_id':7},owner_org_id=7)
        raw.rollback()
    finally:raw.close()
    with engine.connect() as c:assert counts(c)==(0,0,0,0,0)


def test_partial_source_column_footprint_refuses_without_history_mutation(tmp_path):
    e=create_engine('sqlite:///'+str(tmp_path/'partial.db'))
    with e.begin() as c:
        c.exec_driver_sql('CREATE TABLE spools(id INTEGER PRIMARY KEY,org_id INTEGER,economic_uid TEXT)')
        c.exec_driver_sql('CREATE TABLE print_jobs(id INTEGER PRIMARY KEY,printer_id INTEGER,scheduled_job_id INTEGER,status TEXT,spool_snapshot TEXT)')
    with pytest.raises(Exception):
        with e.begin() as c:M.apply(c)
    with e.connect() as c:
        assert c.exec_driver_sql("SELECT count(*) FROM sqlite_master WHERE name='cost_source_lifetimes'").scalar_one()==0
        assert 'economic_birth_nonce' not in [r[1] for r in c.exec_driver_sql('PRAGMA table_info(print_jobs)').all()]
    e.dispose()


@pytest.mark.parametrize('kind,table',[('spool','spools'),('attempt','print_jobs')])
def test_unresolved_birth_id_is_immutable_without_freezing_unknown_fields(engine,kind,table):
    with engine.begin() as c:
        born=(_create_spool(c,{'org_id':7}) if kind=='spool' else _create_attempt(c,{'printer_id':1,'status':'running'}))
        assert born.state=='unresolved'
    with engine.connect() as c:
        with pytest.raises(Exception):c.exec_driver_sql(f'UPDATE {table} SET id=101 WHERE id=?',(born.id,))
        c.rollback()
    with engine.begin() as c:
        if kind=='spool':c.exec_driver_sql('UPDATE spools SET org_id=9 WHERE id=?',(born.id,))
        else:c.exec_driver_sql('UPDATE print_jobs SET printer_id=2,scheduled_job_id=NULL WHERE id=?',(born.id,))
        assert c.exec_driver_sql(f'SELECT id FROM {table} WHERE economic_birth_nonce=?',(born.birth_nonce,)).scalar_one()==born.id
        assert c.exec_driver_sql('SELECT resource_id,owner_scope,identity_uid FROM cost_source_lifetimes WHERE birth_nonce=?',(born.birth_nonce,)).one()==(born.id,None,None)
        M.validate(c)
