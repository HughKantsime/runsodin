"""Additive receipt upgrade, immutable historical pins and exact schema adoption."""
import hashlib
import importlib
from pathlib import Path
import pytest
from sqlalchemy import create_engine, inspect, text
from core.schema import bootstrap_database
from core.schema.migrator import MigrationError

M = importlib.import_module('core.schema.migrations.043_recipe_configuration')


@pytest.fixture
def engine(tmp_path):
    engine=create_engine('sqlite:///'+str(tmp_path/'recipe.db'))
    yield engine
    engine.dispose()


def test_fresh_repeat_and_backup_inventory(engine):
    from core.schema.bootstrap import _orm_manifest
    from modules.system.postgres_backup_service import canonical_tables
    first=bootstrap_database(engine)
    second=bootstrap_database(engine)
    assert M.MIGRATION_ID in first['applied']
    assert second['applied']==[] and first['schema_fingerprint']==second['schema_fingerprint']
    assert M.TABLE in canonical_tables()
    assert set(_orm_manifest()[M.TABLE])=={c['name'] for c in inspect(engine).get_columns(M.TABLE)}
    assert _orm_manifest()[M.TABLE][-2:]==list(M.ADDITIONS)
    with engine.begin() as c:M.apply(c);M.validate(c)


def _pre043(engine):
    bootstrap_database(engine)
    with engine.begin() as c:
        for column in M.ADDITIONS:c.exec_driver_sql('ALTER TABLE '+M.TABLE+' DROP COLUMN '+column)
        c.execute(text('DELETE FROM odin_schema_migrations WHERE migration_id=:id'),{'id':M.MIGRATION_ID})
        c.execute(text('''INSERT INTO dispatch_attempt_receipts(claim_token,printer_id,job_id,
            configuration_key,prepared_power_key,observed_filename_key) VALUES(:token,1,85,:key,:key,:key)'''),
            {'token':'a'*32,'key':'b'*64})


def test_pre043_upgrade_keeps_rows_and_leaves_identity_unknown(engine):
    _pre043(engine)
    assert bootstrap_database(engine)['applied']==[M.MIGRATION_ID]
    with engine.connect() as c:
        assert c.execute(text('SELECT job_id,configuration_key,stable_configuration_key,stable_key_source FROM dispatch_attempt_receipts')).one()==(85,'b'*64,None,None)
    assert bootstrap_database(engine)['applied']==[]


@pytest.mark.parametrize('ddl', [
    'stable_configuration_key TEXT', 'stable_configuration_key VARCHAR(64) NOT NULL DEFAULT \'bad\'',
    "stable_configuration_key VARCHAR(64) DEFAULT 'bad'", 'stable_key_source VARCHAR(31)',
])
def test_wrong_or_partial_existing_shape_refused_without_ledger(engine,ddl):
    _pre043(engine)
    with engine.begin() as c:c.exec_driver_sql('ALTER TABLE '+M.TABLE+' ADD COLUMN '+ddl)
    with pytest.raises(MigrationError):bootstrap_database(engine)
    with engine.connect() as c:
        assert c.execute(text('SELECT count(*) FROM odin_schema_migrations WHERE migration_id=:id'),{'id':M.MIGRATION_ID}).scalar_one()==0
        assert c.execute(text('SELECT job_id FROM dispatch_attempt_receipts')).scalar_one()==85


def test_original_receipt_schema_pin_unchanged():
    original=importlib.import_module('core.schema.migrations.012_dispatch_receipts')
    assert hashlib.sha256(Path('backend/core/dispatch_receipt_schema.py').read_bytes()).hexdigest()==original.SCHEMA_SOURCE_SHA256


def test_published_1924_entrypoint_upgrade(tmp_path):
    from tests.test_contracts.test_post027_migration_032 import test_published_v1_9_24_database_upgrades_through_032_with_the_entrypoint_command
    test_published_v1_9_24_database_upgrades_through_032_with_the_entrypoint_command(tmp_path)
    engine=create_engine('sqlite:///'+str(tmp_path/'odin.db'))
    try:
        with engine.connect() as c:
            assert c.execute(text('SELECT count(*) FROM odin_schema_migrations WHERE migration_id=:id'),{'id':M.MIGRATION_ID}).scalar_one()==1
            M.validate(c)
    finally:engine.dispose()


from tests.test_contracts.test_post027_migration_040 import pg_engine


def test_postgres_fresh_repeat_and_exact_shape(pg_engine):
    bootstrap_database(pg_engine)
    assert bootstrap_database(pg_engine)['applied']==[]
    with pg_engine.begin() as c:M.apply(c);M.validate(c)
