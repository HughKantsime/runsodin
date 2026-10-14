"""Prospective source creation and retained-history contract on real transactions."""
import importlib
import os
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from time import monotonic, sleep
import pytest
from sqlalchemy import Column, Integer, String, create_engine, text, inspect
from sqlalchemy.orm import DeclarativeBase, Session, attributes
from core.economic_identity import _capture_fresh, _allocate_created, _add_created, EconomicIdentityError

M = importlib.import_module('core.schema.migrations.021_economic_identity')
F = importlib.import_module('core.schema.migrations.009_cost_foundation')

class Base(DeclarativeBase): pass
class Order(Base):
    __tablename__='orders'
    id=Column(Integer,primary_key=True)
    org_id=Column(Integer)
class Item(Base):
    __tablename__='order_items'
    id=Column(Integer,primary_key=True)
    order_id=Column(Integer,nullable=False)
class Job(Base):
    __tablename__='jobs'
    id=Column(Integer,primary_key=True)
    order_item_id=Column(Integer)
    charged_to_org_id=Column(Integer)

@pytest.fixture
def engine(tmp_path):
    url=os.environ.get('ODIN_OWNED_IDENTITY_DB')
    if url:
        schema='identity_'+uuid4().hex
        admin=create_engine(url)
        with admin.begin() as c:c.exec_driver_sql('CREATE SCHEMA '+schema)
        e=create_engine(url,connect_args={'options':'-csearch_path='+schema})
    else:
        e=create_engine('sqlite:///'+str(tmp_path/'identity.db'),connect_args={'timeout':5})
    with e.begin() as c:
        Base.metadata.create_all(c)
        F.apply(c)
        M.apply(c)
    yield e
    e.dispose()
    if url:
        with admin.begin() as c:c.exec_driver_sql("DROP SCHEMA "+schema+" CASCADE")
        admin.dispose()

def graph(s, org=7):
    o=_add_created(s,Order(org_id=org),'order',owner_org_id=org,global_authority=org is None)
    i=_add_created(s,Item(order_id=o.id),'item',owner_org_id=org,global_authority=org is None)
    j=_add_created(s,Job(order_item_id=i.id,charged_to_org_id=org),'job',owner_org_id=org,global_authority=org is None)
    return o,i,j

def test_fresh_graph_replay_rollback_and_retained_numeric_reuse(engine):
    with Session(engine) as s:
        o=Order(org_id=7)
        cap=_capture_fresh(o,'order',owner_org_id=7)
        s.add(o)
        uid=_allocate_created(s,cap)
        assert _allocate_created(s,cap)==uid
        s.commit()
        identifier=o.id
    with Session(engine) as s:
        s.execute(text('DELETE FROM orders WHERE id=:id'),{'id':identifier})
        s.commit()
    with Session(engine) as s:
        o=Order(org_id=7)
        cap=_capture_fresh(o,'order',owner_org_id=7)
        s.add(o)
        s.flush()
        if engine.dialect.name=='postgresql':
            s.execute(text('UPDATE orders SET id=:old WHERE id=:new'),{'old':identifier,'new':o.id})
            attributes.set_committed_value(o,'id',identifier)
        fresh=_allocate_created(s,cap)
        assert o.id==identifier and fresh!=uid
        s.commit()
        assert s.execute(text('SELECT count(*) FROM cost_identities')).scalar_one()==2
    with Session(engine) as s:
        graph(s)
        s.rollback()
    with engine.connect() as c:
        assert c.execute(text('SELECT count(*) FROM cost_identities')).scalar_one()==2
        assert c.execute(text('SELECT count(*) FROM order_items')).scalar_one()==0

@pytest.mark.parametrize('org',[None,7])
def test_actual_graph_and_populated_adoption(engine,org):
    with Session(engine) as s:
        graph(s,org)
        s.commit()
    with engine.begin() as c:
        M.apply(c)
        assert c.execute(text('SELECT count(*) FROM cost_identities')).scalar_one()==3
        assert c.execute(text('SELECT count(*) FROM cost_order_memberships')).scalar_one()==2
        assert c.execute(text('SELECT count(*) FROM cost_job_bindings')).scalar_one()==1

@pytest.mark.parametrize('sql',[
    "UPDATE orders SET economic_uid=NULL", "UPDATE orders SET economic_uid='foreign'",
    'UPDATE orders SET org_id=9', 'UPDATE order_items SET order_id=100',
    'UPDATE jobs SET order_item_id=NULL','UPDATE jobs SET charged_to_org_id=9',
    "INSERT INTO orders(id,org_id,economic_uid) SELECT 100,org_id,economic_uid FROM orders",
    "INSERT OR REPLACE INTO orders(id,org_id) SELECT id,org_id FROM orders",
    "INSERT OR REPLACE INTO order_items(id,order_id) SELECT id,order_id FROM order_items",
    "INSERT OR REPLACE INTO jobs(id,order_item_id,charged_to_org_id) SELECT id,order_item_id,charged_to_org_id FROM jobs",
])
def test_sql_source_guards_including_replace_recursive_off(engine,sql):
    with Session(engine) as s: graph(s);s.commit()
    if engine.dialect.name!='sqlite' and 'REPLACE' in sql:
        pytest.skip('SQLite replacement syntax is SQLite-specific')
    with engine.connect() as c:
        if engine.dialect.name=='sqlite':c.exec_driver_sql('PRAGMA recursive_triggers=OFF')
        with pytest.raises(Exception,match='economic source'):
            c.execute(text(sql))
        c.rollback()
    with engine.begin() as c:M.validate(c)

def test_no_historical_root_allocation_and_no_implicit_global(engine):
    with Session(engine) as s:
        old=Order(org_id=7);s.add(old);s.flush()
        _add_created(s,Item(order_id=old.id),'item',owner_org_id=7)
        assert s.execute(text('SELECT count(*) FROM cost_identities')).scalar_one()==0
        with pytest.raises(EconomicIdentityError,match='global'):
            _capture_fresh(Job(),'job',owner_org_id=None)
        s.rollback()

def test_partial_footprint_refuses_before_mutation(tmp_path):
    e=create_engine('sqlite:///'+str(tmp_path/'partial.db'))
    with e.begin() as c:
        Base.metadata.create_all(c);F.apply(c)
        c.execute(text('ALTER TABLE orders ADD COLUMN economic_uid TEXT'))
    with pytest.raises(Exception,match='shape'):
        with e.begin() as c:M.apply(c)
    with e.connect() as c:
        assert 'economic_uid' not in {x['name'] for x in inspect(c).get_columns('jobs')}
    e.dispose()

def test_guard_drift_refuses(engine):
    with engine.begin() as c:c.execute(text('DROP TRIGGER jobs_economic_insert' if engine.dialect.name=='sqlite' else 'DROP TRIGGER jobs_economic_guard ON jobs'))
    with pytest.raises(Exception,match='guard'):
        with engine.begin() as c:M.apply(c)

def test_migration_rolls_back_source_columns(tmp_path):
    e=create_engine('sqlite:///'+str(tmp_path/'rollback.db'))
    with e.begin() as c:Base.metadata.create_all(c);F.apply(c)
    with pytest.raises(RuntimeError):
        with e.begin() as c:M.apply(c);raise RuntimeError('rollback')
    with e.connect() as c:
        assert all('economic_uid' not in {x['name'] for x in inspect(c).get_columns(t)} for t in M.TABLES)
    e.dispose()

def test_owner_mismatch_rolls_back_both_halves(engine):
    with Session(engine) as s:
        o=Order(org_id=8);cap=_capture_fresh(o,'order',owner_org_id=7);s.add(o)
        with pytest.raises(EconomicIdentityError,match='owner'): _allocate_created(s,cap)
        s.rollback()
    with engine.connect() as c:
        assert c.execute(text('SELECT count(*) FROM orders')).scalar_one()==0
        assert c.execute(text('SELECT count(*) FROM cost_identities')).scalar_one()==0

def test_concurrent_replay_waits_on_real_parent_lock_and_returns_one_uid(engine):
    from core.economic_identity import _validate_existing
    with Session(engine) as s:
        o,i,j=graph(s);s.commit();identifier=j.id
    entered=Event()
    release=Event()
    def held():
        with Session(engine) as s:
            uid=_validate_existing(s,'job',identifier,owner_org_id=7)
            entered.set()
            assert release.wait(4)
            s.commit()
            return uid
    def waiting():
        assert entered.wait(4)
        with Session(engine) as s:
            start=monotonic()
            uid=_validate_existing(s,'job',identifier,owner_org_id=7)
            elapsed=monotonic()-start
            s.commit()
            return uid,elapsed
    with ThreadPoolExecutor(max_workers=2) as pool:
        a=pool.submit(held);b=pool.submit(waiting)
        assert entered.wait(4)
        sleep(.2)
        assert not b.done()
        release.set()
        uid=a.result();other,elapsed=b.result()
    assert uid==other and elapsed>=.15
    with engine.connect() as c:assert c.execute(text('SELECT count(*) FROM cost_identities')).scalar_one()==3


def test_parent_link_recheck_refuses_change_before_locks(engine,monkeypatch):
    import core.economic_identity as helper
    with Session(engine) as s:
        o=_add_created(s,Order(org_id=7),'order',owner_org_id=7)
        other=_add_created(s,Order(org_id=7),'order',owner_org_id=7)
        i=Item(order_id=o.id)
        cap=_capture_fresh(i,'item',owner_org_id=7);s.add(i);s.flush()
        lock=helper._lock
        def changing(db,table,identifier):
            if table=='orders':
                db.execute(text('UPDATE order_items SET order_id=:other WHERE id=:id'),{'other':other.id,'id':i.id})
            return lock(db,table,identifier)
        monkeypatch.setattr(helper,'_lock',changing)
        with pytest.raises(EconomicIdentityError,match='Parent changed'):_allocate_created(s,cap)
        s.rollback()
    with engine.connect() as c:assert c.execute(text('SELECT count(*) FROM cost_identities')).scalar_one()==0


def test_bad_registry_cannot_be_repaired_and_cas_failure_rolls_back(engine,monkeypatch):
    import core.economic_identity as helper
    from core.economic_identity import _validate_existing
    with Session(engine) as s:
        o=_add_created(s,Order(org_id=7),'order',owner_org_id=7);s.commit();identifier=o.id
    with Session(engine) as s:
        with pytest.raises(EconomicIdentityError,match='owner'):_validate_existing(s,'order',identifier,owner_org_id=8)
        s.rollback()
    with Session(engine) as s:
        obj=Order(org_id=7);cap=_capture_fresh(obj,'order',owner_org_id=7);s.add(obj)
        original=s.execute
        class Lost:
            rowcount=0
        def fail_cas(statement,params=None,**kwargs):
            if str(statement).startswith('UPDATE orders SET economic_uid='):return Lost()
            return original(statement,params,**kwargs)
        monkeypatch.setattr(s,'execute',fail_cas)
        with pytest.raises(EconomicIdentityError,match='lost race'):_allocate_created(s,cap)
        s.rollback()
    with engine.connect() as c:
        assert c.execute(text('SELECT count(*) FROM cost_identities')).scalar_one()==1
        assert c.execute(text('SELECT count(*) FROM orders')).scalar_one()==1

@pytest.mark.parametrize('declaration',[
    'TEXT DEFAULT NULL REFERENCES cost_identities(identity_uid) ON DELETE RESTRICT',
    'TEXT REFERENCES cost_identities(identity_uid) ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED',
    'TEXT REFERENCES cost_identities(identity_uid) ON DELETE CASCADE',
    'TEXT COLLATE NOCASE REFERENCES cost_identities(identity_uid) ON DELETE RESTRICT',
])
def test_exact_complete_adoption_rejects_raw_column_drift_before_mutation(tmp_path,declaration):
    e=create_engine('sqlite:///'+str(tmp_path/'drift.db'))
    with e.begin() as c:
        Base.metadata.create_all(c);F.apply(c)
        for table in M.TABLES:
            definition=declaration if table=='orders' else 'TEXT REFERENCES cost_identities(identity_uid) ON DELETE RESTRICT'
            c.exec_driver_sql(f'ALTER TABLE {table} ADD COLUMN economic_uid '+definition)
            c.exec_driver_sql(f'CREATE UNIQUE INDEX {table}_economic_uid_unique ON {table}(economic_uid)')
            for ddl in M._sqlite_triggers(table).values():c.exec_driver_sql(ddl)
    with e.connect() as c:before=c.execute(text('SELECT name,sql FROM sqlite_master ORDER BY name')).all()
    with pytest.raises(Exception,match='Economic'):
        with e.begin() as c:M.apply(c)
    with e.connect() as c:assert c.execute(text('SELECT name,sql FROM sqlite_master ORDER BY name')).all()==before
    e.dispose()

def test_creation_capability_refuses_persistent_or_cross_kind_sources(engine):
    with pytest.raises(EconomicIdentityError,match='Fresh'):_capture_fresh(Item(order_id=1),'order',owner_org_id=7)
    with Session(engine) as s:
        legacy=Order(org_id=7);s.add(legacy);s.flush()
        with pytest.raises(EconomicIdentityError,match='Fresh'):_capture_fresh(legacy,'order',owner_org_id=7)
        s.rollback()
