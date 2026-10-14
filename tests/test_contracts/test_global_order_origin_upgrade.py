"""Actual025 retained source data precedes registered026 cutover, no adoption."""
import importlib
from concurrent.futures import ThreadPoolExecutor
import pytest
from sqlalchemy import create_engine,text,inspect,event
from sqlalchemy.orm import Session
from core.schema import bootstrap_database
from core.economic_identity import _add_created
from modules.orders.models import Order
from tests.test_contracts.test_global_order_origin_storage import fresh
M=importlib.import_module('core.schema.migrations.026_global_order_origins')


def pre026(monkeypatch,engine):
    import core.schema.bootstrap as bootstrap
    with monkeypatch.context() as patch:
        real=bootstrap.apply_python_migration
        patch.setattr(bootstrap,'apply_python_migration',lambda c,name:False if name.endswith('.026_global_order_origins') else real(c,name))
        patch.setattr(bootstrap,'RAW_REQUIRED_TABLES',bootstrap.RAW_REQUIRED_TABLES-set(M.schema.TABLES))
        columns=dict(bootstrap.RAW_REQUIRED_COLUMNS)
        for table in M.schema.TABLES:columns.pop(table)
        columns['orders']=columns['orders']-{M.schema.SOURCE}
        patch.setattr(bootstrap,'RAW_REQUIRED_COLUMNS',columns)
        bootstrap_database(engine)


def historical(engine):
    with Session(engine) as db:
        live=_add_created(db,Order(),'order',owner_org_id=None,global_authority=True)
        deleted=_add_created(db,Order(),'order',owner_org_id=None,global_authority=True)
        org=_add_created(db,Order(org_id=1),'order',owner_org_id=1)
        tuples=db.execute(text('SELECT id,economic_uid,org_id FROM orders ORDER BY id')).all()
        db.execute(text('DELETE FROM orders WHERE id=:id'),{'id':deleted.id});db.commit()
    return {uid for id,uid,org in tuples if org is None}


def test_real025_upgrade_excludes_live_deleted_preserves_org_then_repeat(monkeypatch,tmp_path):
    engine=create_engine('sqlite:///'+str(tmp_path/'upgrade.db'))
    try:
        pre026(monkeypatch,engine);expected=historical(engine)
        result=bootstrap_database(engine);assert M.MIGRATION_ID in result['applied']
        with engine.begin() as c:
            assert set(c.execute(text('SELECT order_uid FROM cost_global_origin_exclusions')).scalars())==expected
            assert c.execute(text('SELECT count(*) FROM orders WHERE economic_global_origin_nonce IS NOT NULL')).scalar_one()==0
            id,uid,nonce=fresh(c)
        assert bootstrap_database(engine)['applied']==[]
        with engine.connect() as c:
            assert set(c.execute(text('SELECT order_uid FROM cost_global_origin_exclusions')).scalars())==expected
            assert c.execute(text('SELECT origin_nonce FROM cost_global_order_origins WHERE order_uid=:u'),{'u':uid}).scalar_one()==nonce
    finally:engine.dispose()


def test_actual_history_insert_failure_rolls_back_all026_state(monkeypatch,tmp_path):
    engine=create_engine('sqlite:///'+str(tmp_path/'history-fault.db'))
    try:
        pre026(monkeypatch,engine);historical(engine)
        def fault(c,cursor,statement,parameters,context,executemany):
            if statement.startswith('INSERT INTO odin_schema_migrations') and M.MIGRATION_ID in parameters:raise RuntimeError('Owned026 history insert failure')
        event.listen(engine,'before_cursor_execute',fault)
        with pytest.raises(RuntimeError,match='history insert'):bootstrap_database(engine)
        event.remove(engine,'before_cursor_execute',fault)
        i=inspect(engine);assert not set(M.schema.TABLES)&set(i.get_table_names())
        assert M.schema.SOURCE not in {x['name'] for x in i.get_columns('orders')}
        with engine.connect() as c:assert c.execute(text('SELECT count(*) FROM odin_schema_migrations WHERE migration_id=:id'),{'id':M.MIGRATION_ID}).scalar_one()==0
    finally:engine.dispose()


def test_two_existing025_starters_one026_history(monkeypatch,tmp_path):
    engine=create_engine('sqlite:///'+str(tmp_path/'concurrent.db'),connect_args={'timeout':5})
    try:
        pre026(monkeypatch,engine);historical(engine)
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda _:bootstrap_database(engine),range(2)))
        assert sum(M.MIGRATION_ID in r['applied'] for r in results)==1
        with engine.connect() as c:assert c.execute(text('SELECT count(*) FROM odin_schema_migrations WHERE migration_id=:id'),{'id':M.MIGRATION_ID}).scalar_one()==1
    finally:engine.dispose()
