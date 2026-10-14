"""Unregistered026 raw storage contracts; no production creator authorization claim."""
import importlib
from uuid import uuid4
import pytest
from sqlalchemy import text
from core import global_order_origin_schema as G
from core.schema.migrator import MigrationError
from core.economic_identity import _add_created
from modules.orders.models import Order
from tests.test_contracts.test_education_review_workflow import review_db

@pytest.fixture(autouse=True)
def pre026_standalone_bootstrap(monkeypatch):
    import core.schema.bootstrap as bootstrap
    real=bootstrap.apply_python_migration
    monkeypatch.setattr(bootstrap,'apply_python_migration',lambda c,name:False if name.endswith('.026_global_order_origins') else real(c,name))
    monkeypatch.setattr(bootstrap,'RAW_REQUIRED_TABLES',bootstrap.RAW_REQUIRED_TABLES-set(G.TABLES))
    columns=dict(bootstrap.RAW_REQUIRED_COLUMNS)
    for table in G.TABLES:columns.pop(table)
    columns['orders']=columns['orders']-{G.SOURCE}
    monkeypatch.setattr(bootstrap,'RAW_REQUIRED_COLUMNS',columns)

@pytest.fixture
def origin_db(review_db):
    db=review_db
    live=_add_created(db,Order(),'order',owner_org_id=None,global_authority=True)
    deleted=_add_created(db,Order(),'order',owner_org_id=None,global_authority=True)
    org=_add_created(db,Order(org_id=1),'order',owner_org_id=1)
    rows=db.execute(text('SELECT id,economic_uid,org_id FROM orders ORDER BY id')).all()
    db.execute(text('DELETE FROM orders WHERE id=:id'),{'id':deleted.id});db.commit()
    with db.get_bind().begin() as c: G.apply(c)
    return db,rows

def fresh(c,nonce=None,id=None,org=None):
    """Owned storage fixture, explicit source→registry→UID→origin statements."""
    nonce=nonce or uuid4().hex
    sql='INSERT INTO orders(org_id,economic_global_origin_nonce'+(',id' if id else '')+') VALUES(:org,:n'+(',:id' if id else '')+') RETURNING id'
    identifier=c.execute(text(sql),{'org':org,'n':nonce,'id':id}).scalar_one()
    uid=str(uuid4())
    c.execute(text("INSERT INTO cost_identities(identity_uid,resource_kind,resource_id,owner_scope,owner_key) VALUES(:u,'order',:id,'global',0)"),{'u':uid,'id':identifier})
    c.execute(text('UPDATE orders SET economic_uid=:u WHERE id=:id'),{'u':uid,'id':identifier})
    c.execute(text("INSERT INTO cost_global_order_origins(order_uid,resource_kind,owner_scope,owner_key,origin_nonce,proof_kind) VALUES(:u,'order','global',0,:n,'verified_platform_admin_global_creation')"),{'u':uid,'n':nonce})
    return identifier,uid,nonce

def test_cutover_deleted_exclusion_and_unused_adoption(origin_db):
    db,old=origin_db
    assert set(db.execute(text('SELECT order_uid FROM cost_global_origin_exclusions')).scalars())=={u for _,u,org in old if org is None}
    assert db.execute(text('SELECT count(*) FROM cost_global_order_origins')).scalar_one()==0
    assert db.execute(text('SELECT count(*) FROM orders WHERE economic_global_origin_nonce IS NOT NULL')).scalar_one()==0
    db.commit()
    with db.get_bind().begin() as c: G.apply(c);G.validate(c)

def test_prospective_positive_delete_numeric_reuse_and_recorded_only(origin_db):
    db,_=origin_db;db.commit()
    with db.get_bind().begin() as c:
        id,uid,nonce=fresh(c);G.validate(c)
        with pytest.raises(MigrationError):G.apply(c)
    with db.get_bind().begin() as c:c.execute(text('DELETE FROM orders WHERE id=:id'),{'id':id});G.validate(c)
    with db.get_bind().begin() as c:
        second,uid2,_=fresh(c,id=id);assert second==id and uid2!=uid;G.validate(c)
    with pytest.raises(Exception):
        with db.get_bind().begin() as c:fresh(c,nonce=nonce)

@pytest.mark.parametrize('recursive',[False,True])
def test_replace_and_nonce_update_guards(origin_db,recursive):
    db,old=origin_db;db.commit()
    with db.get_bind().begin() as c:
        c.exec_driver_sql('PRAGMA recursive_triggers='+('ON' if recursive else 'OFF'));id,uid,n=fresh(c)
    statements=["INSERT OR REPLACE INTO orders(id) VALUES(:id)","UPDATE orders SET economic_global_origin_nonce=NULL WHERE id=:id", "UPDATE orders SET economic_global_origin_nonce=:n WHERE economic_global_origin_nonce IS NULL",'INSERT OR REPLACE INTO cost_global_order_origins SELECT * FROM cost_global_order_origins','INSERT OR REPLACE INTO cost_global_origin_exclusions SELECT * FROM cost_global_origin_exclusions','DELETE FROM cost_global_order_origins','UPDATE cost_global_origin_exclusions SET owner_key=0']
    for sql in statements:
        with pytest.raises(Exception):
            with db.get_bind().begin() as c:c.execute(text(sql),{'id':id,'n':'a'*32})
    # Even an unidentified source cannot be replaced.
    with db.get_bind().begin() as c:legacy=c.execute(text('INSERT INTO orders DEFAULT VALUES RETURNING id')).scalar_one()
    with pytest.raises(Exception):
        with db.get_bind().begin() as c:c.execute(text('INSERT OR REPLACE INTO orders(id) VALUES(:id)'),{'id':legacy})

@pytest.mark.parametrize('nonce',['a'*31,'A'*32,'g'*32,'a'*33,''])
def test_invalid_source_nonce(origin_db,nonce):
    db,_=origin_db;db.commit()
    with pytest.raises(Exception):
        with db.get_bind().begin() as c:c.execute(text('INSERT INTO orders(economic_global_origin_nonce) VALUES(:n)'),{'n':nonce})

def test_foreign_org_and_exclusion_admission(origin_db):
    db,old=origin_db;db.commit()
    with pytest.raises(Exception):
        with db.get_bind().begin() as c:fresh(c,org=1)
    excluded=next(uid for _,uid,org in old if org is None)
    with pytest.raises(Exception):
        with db.get_bind().begin() as c:c.execute(text("INSERT INTO cost_global_order_origins(order_uid,resource_kind,owner_scope,owner_key,origin_nonce,proof_kind) VALUES(:u,'order','global',0,:n,'verified_platform_admin_global_creation')"),{'u':excluded,'n':'b'*32})
    with db.get_bind().begin() as c:_,uid,_=fresh(c)
    with pytest.raises(Exception):
        with db.get_bind().begin() as c:c.execute(text("INSERT INTO cost_global_origin_exclusions VALUES(:u,'order','global',0)"),{'u':uid})

def test_missing_origin_and_reverse_nonce_drift_rejected(origin_db):
    db,_=origin_db;db.commit()
    with db.get_bind().begin() as c:
        c.execute(text("INSERT INTO orders(economic_global_origin_nonce) VALUES(:n)"),{'n':'c'*32})
        with pytest.raises(MigrationError,match='live source'):G.validate(c)
        c.rollback()
    with db.get_bind().begin() as c:id,uid,n=fresh(c)
    with db.get_bind().begin() as c:
        c.exec_driver_sql('DROP TRIGGER orders_global_origin_update')
        c.execute(text('UPDATE orders SET economic_global_origin_nonce=NULL WHERE id=:id'),{'id':id})
        c.exec_driver_sql(G.sqlite_triggers()['orders_global_origin_update'])
        with pytest.raises(MigrationError,match='live source'):G.validate(c)
        c.rollback()

@pytest.mark.parametrize('sql',["DROP TRIGGER orders_global_origin_insert","CREATE UNIQUE INDEX orders_unreviewed_unique ON orders(order_number)","DROP INDEX orders_global_origin_nonce_unique"])
def test_drift_refuses_without_repair(origin_db,sql):
    db,_=origin_db;db.commit()
    with db.get_bind().begin() as c:
        c.exec_driver_sql(sql)
        with pytest.raises(MigrationError):G.validate(c)
        c.rollback()

def test_cutover_failure_rolls_back_ddl_exclusions(review_db,monkeypatch):
    db=review_db;_add_created(db,Order(),'order',owner_org_id=None,global_authority=True);db.commit()
    real=G.validate
    monkeypatch.setattr(G,'validate',lambda c:(_ for _ in ()).throw(RuntimeError('Owned final validation failure')))
    with pytest.raises(RuntimeError):
        with db.get_bind().begin() as c:G.apply(c)
    with db.get_bind().connect() as c:
        from sqlalchemy import inspect
        assert not inspect(c).has_table(G.ORIGINS)
        assert G.SOURCE not in {x['name'] for x in inspect(c).get_columns('orders')}
    monkeypatch.setattr(G,'validate',real)
    with db.get_bind().begin() as c:G.apply(c)

def test_unrecorded_adoption_requires_exact_cutover_exclusions(origin_db):
    db,_=origin_db;db.commit()
    with db.get_bind().begin() as c:
        c.exec_driver_sql('DROP TRIGGER cost_global_origin_exclusions_no_delete')
        c.exec_driver_sql('DELETE FROM cost_global_origin_exclusions')
        c.exec_driver_sql(G.sqlite_triggers()['cost_global_origin_exclusions_no_delete'])
        with pytest.raises(MigrationError,match='cutover exclusions'):G.apply(c)
        c.rollback()

def test_partial_footprint_refuses_without_repair(review_db):
    db=review_db;db.commit()
    with db.get_bind().begin() as c:
        c.exec_driver_sql('ALTER TABLE orders ADD COLUMN economic_global_origin_nonce TEXT')
        with pytest.raises(MigrationError):G.apply(c)
        from sqlalchemy import inspect
        assert not inspect(c).has_table(G.ORIGINS)
        c.rollback()

def test_disabled_sqlite_check_enforcement_refuses(origin_db):
    db,_=origin_db;db.commit()
    with db.get_bind().begin() as c:
        c.exec_driver_sql('PRAGMA ignore_check_constraints=ON')
        with pytest.raises(MigrationError,match='CHECK enforcement'):G.validate(c)
        c.exec_driver_sql('PRAGMA ignore_check_constraints=OFF')

def test_wrapper_explicit_apply_validate_source_pin(origin_db):
    db,_=origin_db;db.commit()
    module=importlib.import_module('core.schema.migrations.026_global_order_origins')
    with db.get_bind().begin() as c:module.apply(c);module.validate(c)
