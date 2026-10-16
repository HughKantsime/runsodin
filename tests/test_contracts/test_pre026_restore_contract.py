"""Closed historical stage surfaces; actual data path has separate native proof."""
from pathlib import Path
import pytest
from sqlalchemy import create_engine
from core.schema.pre026_restore_contract import (PRE026_RESTORE025,INVENTORY,check_contract,check_sources,owned_stage_database,bootstrap_stage)
from core.schema.migrator import MigrationError
from modules.system.postgres_canonical_executables import canonical_functions,canonical_triggers
from modules.system import postgres_backup_service as backup
from ._preserved029_target import bundle_only

pytestmark = bundle_only


def test_positive_closed25_inventory_and_current26_default():
    check_sources()
    assert len(INVENTORY['tables'])==114
    assert len(canonical_functions(_contract=PRE026_RESTORE025))==17
    assert len(canonical_triggers(_contract=PRE026_RESTORE025))==24
    assert (len(canonical_functions()),len(canonical_triggers()))==(21,31)
    assert set(backup.canonical_tables())-set(backup.canonical_tables(_contract=PRE026_RESTORE025))=={'cost_global_order_origins','cost_global_origin_exclusions','power_target_operations'}
    assert 'economic_global_origin_nonce' not in INVENTORY['columns']['orders']

@pytest.mark.parametrize('contract',[25,26,{},'025',object()])
def test_no_arbitrary_version_or_contract(contract):
    with pytest.raises(MigrationError):check_contract(contract)
    with pytest.raises(MigrationError):backup.canonical_tables(_contract=contract)


def test_no_unbound_non_pg_or_live_target_stage_capability(tmp_path):
    with pytest.raises(MigrationError):bootstrap_stage(object())
    engine=create_engine('sqlite:///'+str(tmp_path/'owned.db'))
    try:
        with pytest.raises(MigrationError):owned_stage_database(engine,'odin_validate_'+'a'*20)
    finally:engine.dispose()


def test_source_mismatch_refuses_before_helpers(monkeypatch):
    import core.schema.pre026_restore_contract as contract
    monkeypatch.setitem(contract.SOURCE_PINS,'core/spool_purchase_schema.py','0'*64)
    with pytest.raises(MigrationError,match='source differs'):check_sources()
    with pytest.raises(MigrationError):canonical_functions(_contract=PRE026_RESTORE025)
    # Ordinary current26 operation has no historical fallback or source switch.
    assert len(canonical_functions())==21


def test_contract_whole_source_pin_refuses(monkeypatch):
    import core.schema.pre026_restore_integrity as integrity
    monkeypatch.setattr(integrity,'SOURCE_SHA256','0'*64)
    with pytest.raises(MigrationError,match='contract source differs'):check_sources()


def test_historical_toc_positive25_and_current26_defaults():
    toc='\n'.join(f'{i+1}; 1 1 TABLE public {table} postgres' for i,table in enumerate(INVENTORY['tables']))
    assert backup.validate_archive_toc(toc,_contract=PRE026_RESTORE025)['table_count']==114
    with pytest.raises(backup.BackupValidationError):backup.validate_archive_toc(toc)


def test_exact_cutover_uid_comparison_detects_missing_deleted_registry_root():
    from sqlalchemy import text
    from modules.system.pre026_restore_stage import missing_cutover_exclusions
    engine=create_engine('sqlite://')
    try:
        with engine.begin() as c:
            c.exec_driver_sql('CREATE TABLE cost_identities(identity_uid TEXT,resource_kind TEXT,owner_scope TEXT,owner_key INTEGER)')
            c.exec_driver_sql('CREATE TABLE cost_global_origin_exclusions(order_uid TEXT)')
            c.exec_driver_sql("INSERT INTO cost_identities VALUES('retained-deleted','order','global',0),('org-root','order','org',1)")
            assert tuple(missing_cutover_exclusions(c))==('retained-deleted',)
            c.exec_driver_sql("INSERT INTO cost_global_origin_exclusions VALUES('retained-deleted')")
            assert missing_cutover_exclusions(c) is None
    finally:engine.dispose()


@pytest.mark.parametrize('change',['changed','new_core','new_module','missing'])
def test_fixed_sql_inventory_refuses_changes_before_execution(tmp_path,change):
    import shutil
    from core.schema.pre026_restore_contract import SQL_SOURCE_PINS,validate_sql_inventory
    root=Path(__file__).resolve().parents[2]/'backend'
    for name in SQL_SOURCE_PINS:
        target=tmp_path/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(root/name,target)
    validate_sql_inventory(tmp_path)
    one=tmp_path/next(iter(SQL_SOURCE_PINS))
    if change=='changed':one.write_text(one.read_text()+'\n-- unqualified mutation\n')
    elif change=='missing':one.unlink()
    else:
        addition=tmp_path/('core/migrations/999_unqualified.sql' if change=='new_core' else 'modules/unqualified/migrations/999_new.sql')
        addition.parent.mkdir(parents=True,exist_ok=True);addition.write_text('SELECT 1;')
    with pytest.raises(MigrationError,match='SQL'):validate_sql_inventory(tmp_path)


def test_fixed_ledger_independent_source_oracle_and_mismatch_refusal():
    import json
    from sqlalchemy import text
    from core.schema.pre026_restore_contract import EXPECTED_POSTGRES_LEDGER,SQL_SOURCE_PINS,validate_postgres_history
    oracle=json.loads((Path(__file__).resolve().parents[2]/'tests/fixtures/certification/pre026-independent-history-oracle.json').read_text())
    assert tuple(tuple(row) for row in oracle['expected_postgres_ledger'])==EXPECTED_POSTGRES_LEDGER
    assert oracle['sql_source_sha256']==SQL_SOURCE_PINS and len(SQL_SOURCE_PINS)==24 and len(EXPECTED_POSTGRES_LEDGER)==50
    engine=create_engine('sqlite://')
    try:
        with engine.begin() as c:
            c.exec_driver_sql('CREATE TABLE odin_schema_migrations(migration_id TEXT,checksum TEXT,dialect TEXT)')
            for id,sha,dialect in EXPECTED_POSTGRES_LEDGER:c.execute(text('INSERT INTO odin_schema_migrations VALUES(:id,:sha,:dialect)'),{'id':id,'sha':sha,'dialect':dialect})
            validate_postgres_history(c)
            c.exec_driver_sql("UPDATE odin_schema_migrations SET checksum='changed' WHERE migration_id='core/migrations/001_initial.sql'")
            with pytest.raises(MigrationError,match='fixed packaged contract'):validate_postgres_history(c)
    finally:engine.dispose()
