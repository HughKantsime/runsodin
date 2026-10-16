"""Registered025 inventory/source refusal and exact canonical additions."""
import importlib
import pytest
from sqlalchemy import create_engine,inspect,text
from core.schema import bootstrap_database
from core.schema.bootstrap import RAW_REQUIRED_COLUMNS
from core.schema.migrator import MigrationError
from modules.system.postgres_backup_service import canonical_tables
from modules.system.postgres_canonical_executables import canonical_functions,canonical_triggers
from ._preserved029_target import bundle_only
M=importlib.import_module('core.schema.migrations.025_spool_purchase_evidence')

def test_registered_bootstrap_repeat_inventory_and_nullable_history(tmp_path):
    engine=create_engine('sqlite:///'+str(tmp_path/'registered.db'))
    try:
        result=bootstrap_database(engine)
        assert M.MIGRATION_ID in result['applied']
        physical=inspect(engine)
        assert set(physical.get_table_names())==set(canonical_tables())
        assert {c['name'] for c in physical.get_columns('spool_purchase_evidence')}==RAW_REQUIRED_COLUMNS['spool_purchase_evidence']
        columns={c['name']:c for c in physical.get_columns('spools')}
        for name in ('purchase_evidence_nonce','purchase_revision'):
            assert columns[name]['nullable'] and columns[name]['default'] is None
        assert bootstrap_database(engine)['applied']==[]
        with engine.connect() as c:
            assert c.execute(text('SELECT count(*) FROM odin_schema_migrations WHERE migration_id=:id'),{'id':M.MIGRATION_ID}).scalar_one()==1
    finally:engine.dispose()

@pytest.mark.parametrize('name',list(M.SOURCE_PINS))
def test_registered_source_mismatch_refuses_without_purchase_footprint(tmp_path,monkeypatch,name):
    engine=create_engine('sqlite:///'+str(tmp_path/'refusal.db'))
    monkeypatch.setitem(M.SOURCE_PINS,name,'0'*64)
    try:
        with pytest.raises(MigrationError,match='Spool purchase source differs'):bootstrap_database(engine)
        physical=inspect(engine)
        assert 'spool_purchase_evidence' not in physical.get_table_names()
        assert not {'purchase_revision','purchase_evidence_nonce'} & {c['name'] for c in physical.get_columns('spools')}
        with engine.connect() as c:assert c.execute(text('SELECT count(*) FROM odin_schema_migrations WHERE migration_id=:id'),{'id':M.MIGRATION_ID}).scalar_one()==0
    finally:engine.dispose()

@bundle_only
def test_canonical_purchase_exact_union_and_shared_truncate_target():
    functions=canonical_functions();triggers=canonical_triggers()
    from core.schema.pre026_restore_contract import PRE027_RESTORE026
    legacy_functions=canonical_functions(_contract=PRE027_RESTORE026)
    legacy_triggers=canonical_triggers(_contract=PRE027_RESTORE026)
    assert (len(legacy_functions),len(legacy_triggers))==(20,29)
    assert (len(functions),len(triggers))==(21,31)
    assert all(functions[k]==v for k,v in legacy_functions.items())
    assert all(triggers[k]==v for k,v in legacy_triggers.items())
    assert functions['spool_purchase_evidence_guard'][1]==['search_path=pg_catalog, public']
    assert triggers[('spool_purchase_evidence','spool_purchase_evidence_no_truncate')][1]=='spool_purchase_evidence_guard'
    assert triggers[('spools','spools_purchase_guard')][1]=='spools_purchase_guard'
