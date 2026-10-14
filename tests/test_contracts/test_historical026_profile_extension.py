"""Closed source oracle extension; qualification remains separate from these assertions."""
import hashlib,json
from pathlib import Path
import pytest
from sqlalchemy import create_engine,text
from core.schema import pre026_restore_contract as C
from modules.system import postgres_canonical_executables as E
from modules.system import postgres_backup_service as B
from core.schema.migrator import MigrationError


def test_immutable025_oracle_and_closed026_extension():
    root=Path(__file__).resolve().parents[2]
    oracle=json.loads((root/'artifacts/edu-workflow-check/pre026-independent-history-oracle.json').read_text())
    assert oracle['sql_source_sha256']==C.SQL_SOURCE_PINS
    assert tuple(map(tuple,oracle['expected_postgres_ledger']))==C.EXPECTED_POSTGRES_LEDGER
    assert len(C.SQL_SOURCE_PINS)==24 and len(C.EXPECTED_POSTGRES_LEDGER)==50
    assert len(C.EXPECTED_POSTGRES_LEDGER026)==51
    assert C.EXPECTED_POSTGRES_LEDGER026[:-1]==C.EXPECTED_POSTGRES_LEDGER
    assert C.EXPECTED_POSTGRES_LEDGER026[-1]==('python:026-global-order-origins',hashlib.sha256((root/'backend/core/schema/migrations/026_global_order_origins.py').read_bytes()).hexdigest(),'postgresql')
    assert set(C.INVENTORY026['tables'])-set(C.INVENTORY['tables'])=={'cost_global_order_origins','cost_global_origin_exclusions'}
    assert len(C.INVENTORY026['tables'])==116
    assert C.INVENTORY026['columns']['orders']==C.INVENTORY['columns']['orders']+['economic_global_origin_nonce']
    assert len(C.INVENTORY026['functions'])==20 and len(C.INVENTORY026['triggers'])==29


def test_selection_precedes_new_helpers(monkeypatch):
    # Deliberately source-only inventory test; does NOT qualify historical restore.
    monkeypatch.setattr(C,'check_sources',lambda contract=C.PRE026_RESTORE025:None)
    real=E.importlib.import_module
    def forbid(name,*args,**kwargs):
        if name.endswith('027_power_target_operations'):raise AssertionError('new helper evaluated by historical selector')
        return real(name,*args,**kwargs)
    monkeypatch.setattr(E.importlib,'import_module',forbid)
    assert len(E.canonical_functions(_contract=C.PRE027_RESTORE026))==20
    assert len(E.canonical_triggers(_contract=C.PRE027_RESTORE026))==29
    def forbid26(name,*args,**kwargs):
        if name.endswith(('026_global_order_origins','027_power_target_operations')):raise AssertionError('new helper evaluated by025 selector')
        return real(name,*args,**kwargs)
    monkeypatch.setattr(E.importlib,'import_module',forbid26)
    assert len(E.canonical_functions(_contract=C.PRE026_RESTORE025))==17
    assert len(E.canonical_triggers(_contract=C.PRE026_RESTORE025))==24


def test_fixed51_history_refuses_missing_or_changed(monkeypatch):
    monkeypatch.setattr(C,'check_sources',lambda contract=C.PRE026_RESTORE025:None)
    engine=create_engine('sqlite://')
    with engine.begin() as c:
        c.exec_driver_sql('CREATE TABLE odin_schema_migrations(migration_id TEXT,checksum TEXT,dialect TEXT)')
        for id,sha,dialect in C.EXPECTED_POSTGRES_LEDGER026:c.execute(text('INSERT INTO odin_schema_migrations VALUES(:id,:sha,:dialect)'),dict(id=id,sha=sha,dialect=dialect))
        C.validate_postgres_history(c,C.PRE027_RESTORE026)
        c.exec_driver_sql("DELETE FROM odin_schema_migrations WHERE migration_id='python:026-global-order-origins'")
        with pytest.raises(MigrationError):C.validate_postgres_history(c,C.PRE027_RESTORE026)
    engine.dispose()


def test_current_trigger_union_uses_table_name_keys_and_native_power_shape():
    from core import power_target_operation_schema as power
    triggers=E.canonical_triggers()
    assert all(type(key) is tuple and len(key)==2 for key in triggers)
    for name,definition in power.pg_triggers().items():
        expected=definition.replace('INSERT OR UPDATE OR DELETE','INSERT OR DELETE OR UPDATE').replace('EXECUTE FUNCTION public.','EXECUTE FUNCTION ')
        assert triggers[(power.TABLE,name)]==(expected,power.FUNCTION,'public','O')
