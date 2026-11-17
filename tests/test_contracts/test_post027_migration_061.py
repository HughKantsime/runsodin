"""Physical-print evidence prerequisites and migration contracts."""
from pathlib import Path
import sqlite3
import importlib

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError

from sqlalchemy import create_engine, text

from core.schema import bootstrap_database
from core.schema.migrator import MigrationError

M = importlib.import_module('core.schema.migrations.061_print_filament_evidence')


def test_published_1924_upgrade_preserves_print_jobs_autoincrement():
    """Legacy prints have no nonce: their physical ids must never be reused."""
    root = Path(__file__).resolve().parents[2]
    connection = sqlite3.connect(':memory:')
    connection.executescript((root / 'tests/fixtures/released_databases/odin-v1.9.24-sqlite.sql').read_text())
    engine = create_engine('sqlite://', creator=lambda: connection)
    try:
        bootstrap_database(engine)
        with engine.connect() as c:
            ddl = c.execute(text("SELECT sql FROM sqlite_master WHERE type='table' AND name='print_jobs'")).scalar_one()
            assert 'AUTOINCREMENT' in ddl.upper(), ddl
    finally:
        engine.dispose()


def test_inventory_and_postgres_guards():
    from core.schema.bootstrap import RAW_REQUIRED_TABLES, RAW_REQUIRED_COLUMNS
    from modules.system.postgres_backup_service import canonical_tables
    from modules.system.postgres_canonical_executables import canonical_functions, canonical_triggers
    assert M.history.name in RAW_REQUIRED_TABLES & canonical_tables()
    assert RAW_REQUIRED_COLUMNS[M.history.name] == set(M.history.c.keys())
    for name, body in M.pg_functions().items():
        assert canonical_functions()[name][0] == body
    for key, ddl in M.pg_triggers().items():
        assert canonical_triggers()[key] == (ddl, 'odin_print_filament_evidence_immutable', 'public', 'O')
    assert any('BEFORE TRUNCATE' in ddl for ddl in M.pg_triggers().values())


def test_fresh_boot_twice_and_immutability():
    engine = create_engine('sqlite://')
    try:
        first = bootstrap_database(engine)
        with engine.begin() as c:
            c.execute(M.history.insert().values(print_job_id=1, kind='closed', claim=1, source='settlement'))
        second = bootstrap_database(engine)
        assert second['applied'] == [] and second['schema_fingerprint'] == first['schema_fingerprint']
        for sql in ('UPDATE print_filament_evidence SET grams=4', 'DELETE FROM print_filament_evidence'):
            with engine.begin() as c, pytest.raises(IntegrityError, match='immutable print filament evidence'):
                c.exec_driver_sql(sql)
        assert inspect(engine).get_foreign_keys(M.history.name) == []
    finally:
        engine.dispose()


@pytest.mark.parametrize('weakening', ['shape', 'check', 'nullable', 'trigger', 'index'])
def test_wrong_shape_refused_without_ledger(weakening):
    engine = create_engine('sqlite://')
    try:
        with engine.begin() as c:
            if weakening == 'shape':
                c.exec_driver_sql('CREATE TABLE print_filament_evidence(id INTEGER PRIMARY KEY)')
            else:
                M.apply(c)
                if weakening == 'trigger':
                    c.exec_driver_sql('DROP TRIGGER print_filament_evidence_no_update')
                elif weakening == 'index':
                    c.exec_driver_sql('DROP INDEX uq_print_filament_evidence_claim')
                else:
                    ddl = c.exec_driver_sql("SELECT sql FROM sqlite_master WHERE name='print_filament_evidence'").scalar_one()
                    c.exec_driver_sql('DROP TABLE print_filament_evidence')
                    ddl = ddl.replace('claim IS NULL OR claim = 1', '1=1') if weakening == 'check' else ddl.replace('kind VARCHAR(12) NOT NULL', 'kind VARCHAR(12)')
                    c.exec_driver_sql(ddl)
                    for index in M.history.indexes:
                        index.create(c)
                    for trigger in M.sqlite_triggers().values():
                        c.exec_driver_sql(trigger)
        with pytest.raises(MigrationError):
            bootstrap_database(engine)
        with engine.connect() as c:
            assert not c.execute(text('SELECT 1 FROM odin_schema_migrations WHERE migration_id=:id'), {'id': M.MIGRATION_ID}).first()
    finally:
        engine.dispose()


def test_pre061_upgrade_preserves_rows():
    engine = create_engine('sqlite://')
    try:
        bootstrap_database(engine)
        with engine.begin() as c:
            c.exec_driver_sql('DROP TABLE print_filament_evidence')
            c.execute(text('DELETE FROM odin_schema_migrations WHERE migration_id=:id'), {'id': M.MIGRATION_ID})
            c.exec_driver_sql("INSERT INTO system_config(key,value) VALUES('kept','123')")
            c.exec_driver_sql("INSERT INTO printers(id,name) VALUES(1,'Fixture')")
            c.exec_driver_sql("INSERT INTO filament_library(id,brand,name) VALUES(1,'Fixture','PLA')")
            c.exec_driver_sql('INSERT INTO spools(id,filament_id,remaining_weight_g) VALUES(1,1,960)')
            c.exec_driver_sql("INSERT INTO print_jobs(id,printer_id,started_at) VALUES(1,1,'2026-01-01')")
            c.exec_driver_sql("INSERT INTO spool_usage(spool_id,print_job_id,weight_used_g,used_at,notes) VALUES(1,1,40,'2026-01-01','auto:archive')")
        assert bootstrap_database(engine)['applied'] == [M.MIGRATION_ID]
        with engine.connect() as c:
            assert c.exec_driver_sql("SELECT value FROM system_config WHERE key='kept'").scalar_one() == 123
            assert c.exec_driver_sql('SELECT print_job_id,kind,claim,spool_id,grams FROM print_filament_evidence').one() == (1, 'usage', 1, 1, 40)
            assert c.exec_driver_sql('SELECT remaining_weight_g FROM spools WHERE id=1').scalar_one() == 960
            assert c.exec_driver_sql('SELECT COUNT(*) FROM spool_usage').scalar_one() == 1
    finally:
        engine.dispose()


def test_published_1924_fixture_upgrades_via_entrypoint(tmp_path):
    from tests.test_contracts.test_post027_migration_032 import test_published_v1_9_24_database_upgrades_through_032_with_the_entrypoint_command
    test_published_v1_9_24_database_upgrades_through_032_with_the_entrypoint_command(tmp_path)
    engine = create_engine('sqlite:///' + str(tmp_path / 'odin.db'))
    try:
        with engine.connect() as c:
            M.validate(c)
            assert 'AUTOINCREMENT' in c.exec_driver_sql("SELECT sql FROM sqlite_master WHERE name='print_jobs'").scalar_one().upper()
    finally:
        engine.dispose()
