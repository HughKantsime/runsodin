"""Registered026 inventory, exact canonical union and caller transaction failures."""
import importlib
import pytest
from sqlalchemy import create_engine,inspect,text
from core.schema import bootstrap_database
from core.schema.bootstrap import RAW_REQUIRED_COLUMNS
from core.schema.migrator import MigrationError,apply_python_migration
from modules.system.postgres_backup_service import canonical_tables
from modules.system.postgres_canonical_executables import canonical_functions,canonical_triggers
M=importlib.import_module('core.schema.migrations.026_global_order_origins')


def test_registered_fresh_repeat_exact_inventory_and_nullable_source(tmp_path):
    engine=create_engine('sqlite:///'+str(tmp_path/'registered.db'))
    try:
        result=bootstrap_database(engine);assert M.MIGRATION_ID in result['applied']
        assert bootstrap_database(engine)['applied']==[]
        i=inspect(engine);assert set(i.get_table_names())==set(canonical_tables())
        for table in M.schema.TABLES:assert {x['name'] for x in i.get_columns(table)}==RAW_REQUIRED_COLUMNS[table]
        source={x['name']:x for x in i.get_columns('orders')}[M.schema.SOURCE]
        assert source['nullable'] and source['default'] is None
        with engine.connect() as c:
            assert c.execute(text('SELECT count(*) FROM odin_schema_migrations WHERE migration_id=:id'),{'id':M.MIGRATION_ID}).scalar_one()==1
            assert c.execute(text('SELECT count(*) FROM cost_global_order_origins')).scalar_one()==0
    finally:engine.dispose()


def test_source_pin_refusal_leaves_no026_footprint(tmp_path,monkeypatch):
    engine=create_engine('sqlite:///'+str(tmp_path/'refusal.db'));monkeypatch.setattr(M,'SOURCE_SHA256','0'*64)
    try:
        with pytest.raises(MigrationError,match='source differs'):bootstrap_database(engine)
        i=inspect(engine);assert not set(M.schema.TABLES)&set(i.get_table_names())
        assert M.schema.SOURCE not in {x['name'] for x in i.get_columns('orders')}
        with engine.connect() as c:assert c.execute(text('SELECT count(*) FROM odin_schema_migrations WHERE migration_id=:id'),{'id':M.MIGRATION_ID}).scalar_one()==0
    finally:engine.dispose()


def test_exact_current_canonical29_40_shared_truncate_map():
    functions=canonical_functions();triggers=canonical_triggers()
    assert (len(functions),len(triggers))==(29,40)
    assert set(M.schema.pg_functions())<=set(functions)
    assert set(M.schema.pg_triggers())<=set(triggers)
    for (table,name),definition in M.schema.pg_triggers().items():
        function='orders_global_origin_guard' if table=='orders' else table+'_guard'
        assert triggers[(table,name)]==(definition,function,'public','O')
        assert functions[function][1]==['search_path=pg_catalog, public']
    assert triggers[('spool_purchase_evidence','spool_purchase_evidence_no_truncate')][1]=='spool_purchase_evidence_guard'
