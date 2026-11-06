"""Traceability migration adoption, upgrade preservation, cleanup and native shape."""
import importlib
import pytest
from sqlalchemy import create_engine, text, inspect
from core.schema import bootstrap_database
from core.schema.migrator import MigrationError
from tests.test_contracts.test_post027_migration_040 import pg_engine

M = importlib.import_module('core.schema.migrations.052_traceability')


@pytest.fixture
def engine(tmp_path):
    engine=create_engine('sqlite:///'+str(tmp_path/'migration.db'))
    bootstrap_database(engine)
    yield engine
    engine.dispose()


def test_inventory_columns_and_repeat_fingerprint(engine):
    from core.schema.bootstrap import RAW_REQUIRED_COLUMNS
    from modules.system.postgres_backup_service import canonical_tables
    first=bootstrap_database(engine)
    second=bootstrap_database(engine)
    assert first['applied']==second['applied']==[]
    assert first['schema_fingerprint']==second['schema_fingerprint']
    for table in M.DDL:
        assert table in canonical_tables()
        assert {c['name'] for c in inspect(engine).get_columns(table)}==set(RAW_REQUIRED_COLUMNS[table])


def test_pre052_upgrade_preserves_job(engine):
    with engine.begin() as c:
        c.execute(text("INSERT INTO jobs(id,item_name,status) VALUES(700,'retained','completed')"))
        for table in M.DDL:c.exec_driver_sql('DROP TABLE '+table)
        c.execute(text('DELETE FROM odin_schema_migrations WHERE migration_id=:id'),{'id':M.MIGRATION_ID})
    assert bootstrap_database(engine)['applied']==[M.MIGRATION_ID]
    with engine.connect() as c:assert c.execute(text('SELECT item_name FROM jobs WHERE id=700')).scalar()=='retained'


@pytest.mark.parametrize('ddl',[
    'CREATE TABLE traceability_records(job_id INTEGER)',
    M.DDL['traceability_records'].replace('inspection_notes TEXT','inspection_notes INTEGER'),
    M.DDL['traceability_records'].replace('ON DELETE RESTRICT','ON DELETE CASCADE'),
])
def test_wrong_shape_refused_without_ledger(engine,ddl):
    with engine.begin() as c:
        c.exec_driver_sql('DROP TABLE traceability_records')
        c.exec_driver_sql(ddl if ddl.startswith('CREATE') else 'CREATE TABLE traceability_records '+ddl)
        c.execute(text('DELETE FROM odin_schema_migrations WHERE migration_id=:id'),{'id':M.MIGRATION_ID})
    with pytest.raises(MigrationError):bootstrap_database(engine)
    with engine.connect() as c:assert c.execute(text('SELECT count(*) FROM odin_schema_migrations WHERE migration_id=:id'),{'id':M.MIGRATION_ID}).scalar()==0


def test_job_and_org_cleanup(engine):
    with engine.begin() as c:
        c.exec_driver_sql('PRAGMA foreign_keys=ON')
        c.execute(text("INSERT INTO groups(id,name) VALUES(700,'Traceability')"))
        c.execute(text("INSERT INTO jobs(id,item_name,status) VALUES(700,'retained','completed')"))
        c.execute(text("INSERT INTO traceability_records(job_id,attempt,snapshot,snapshot_sha256,captured_at) VALUES(700,0,'{}','digest','now')"))
        c.execute(text("INSERT INTO traceability_policies(owner_id,org_id,target_kind,target_key,required) VALUES(700,700,'printer_tag','QA',1)"))
        from sqlalchemy.exc import IntegrityError
        with pytest.raises(IntegrityError):
            with c.begin_nested():
                c.execute(text('DELETE FROM jobs WHERE id=700'))
        c.execute(text('DELETE FROM traceability_records WHERE job_id=700'))
        c.execute(text('DELETE FROM jobs WHERE id=700'))
        c.execute(text('DELETE FROM groups WHERE id=700'))
        assert c.execute(text('SELECT count(*) FROM traceability_records')).scalar()==0
        assert c.execute(text('SELECT count(*) FROM traceability_policies')).scalar()==0


def test_published_1924_upgrade(tmp_path):
    from tests.test_contracts.test_post027_migration_032 import test_published_v1_9_24_database_upgrades_through_032_with_the_entrypoint_command
    test_published_v1_9_24_database_upgrades_through_032_with_the_entrypoint_command(tmp_path)
    engine=create_engine('sqlite:///'+str(tmp_path/'odin.db'))
    try:
        with engine.begin() as c:
            M.validate(c)
            assert c.execute(text('SELECT count(*) FROM odin_schema_migrations WHERE migration_id=:id'),{'id':M.MIGRATION_ID}).scalar()==1
    finally:engine.dispose()


def test_native_postgres_fresh_repeat(pg_engine):
    bootstrap_database(pg_engine)
    assert bootstrap_database(pg_engine)['applied']==[]
    with pg_engine.begin() as c:M.apply(c);M.validate(c)
