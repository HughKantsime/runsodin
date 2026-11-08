"""2.0.47: production TEXT printer IDs, released checksums and partial 054 recovery."""
import importlib
from pathlib import Path
import subprocess
import sys

import pytest
from sqlalchemy import create_engine, inspect, text

from core.job_identity import allocate_sqlite_job_id
from core.schema import bootstrap_database, preserved053
from core.schema.migrator import MigrationError, apply_python_migration
from tests.test_contracts.test_controlled_postgres import native_pg

MODULE = 'core.schema.migrations.055_controlled_history'
HISTORY = importlib.import_module(MODULE)
PARTS = importlib.import_module('core.schema.migrations.054_controlled_parts')
OLD_CHECKSUM = '5c11304c296f9ebc4a414a776a6438fd81cfdba2c0a119e20f93f95b114ff6e2'


@pytest.fixture
def minimal_engine(tmp_path):
    engine = create_engine('sqlite:///' + str(tmp_path / 'references.db'))
    with engine.begin() as c:
        c.exec_driver_sql('CREATE TABLE jobs(id INTEGER PRIMARY KEY)')
        c.exec_driver_sql('CREATE TABLE users(id INTEGER PRIMARY KEY)')
        c.exec_driver_sql('CREATE TABLE print_files(id INTEGER PRIMARY KEY)')
        c.exec_driver_sql('CREATE TABLE print_jobs(job_id TEXT, scheduled_job_id INTEGER)')
        c.exec_driver_sql('CREATE TABLE audit_logs(entity_type TEXT, entity_id TEXT)')
        PARTS.apply(c)
    yield engine
    engine.dispose()


def seed_references(c):
    c.exec_driver_sql('INSERT INTO print_jobs(job_id,scheduled_job_id) VALUES(?,?)',
                      [('0', 221), ('abc', 7), ('999999', 8)])
    c.exec_driver_sql('INSERT INTO audit_logs(entity_type,entity_id) VALUES(?,?)',
                      [('job', '9'), ('job', '0300'), ('job', 'abc'), ('printer', '999998')])


def test_prod_text_printer_ids_do_not_enter_high_water(minimal_engine):
    with minimal_engine.begin() as c:
        seed_references(c)
        assert apply_python_migration(c, MODULE)
        high_water = c.exec_driver_sql('SELECT high_water FROM job_id_high_water').scalar_one()
        assert type(high_water) is int and high_water == 300
        assert allocate_sqlite_job_id(c.exec_driver_sql) == 301
        HISTORY.validate(c)


@pytest.mark.parametrize('value,expected', [
    (321, 321), ('0321', 321), ('9', 9), ('0', 0), (-7, 0),
    ('abc', 0), ('999junk', 0), ('', 0), (' 999', 0), ('999 ', 0),
    ('+999', 0), ('-999', 0), ('999.0', 0), ('9e9', 0), ('９９９', 0),
    ('٩٩٩', 0), ('999\n', 0), ('999\x00junk', 0), (999.5, 0),
    (999.0, 0), (b'999', 0), (None, 0),
])
def test_reference_scan_accepts_only_integers_and_ascii_digit_text(minimal_engine, value, expected):
    with minimal_engine.begin() as c:
        # No affinity: preserve each SQLite storage type exactly as supplied.
        c.exec_driver_sql('CREATE TABLE legacy_reference(job_id)')
        c.exec_driver_sql('INSERT INTO legacy_reference VALUES(?)', (value,))
        result = HISTORY.largest_referenced_job_id(c)
        assert type(result) is int and result == expected


@pytest.mark.parametrize('definition,extra', [
    ('legacy(job_id TEXT)', ''),
    ('legacy(scheduled_job_id TEXT)', ''),
    ('legacy(parent TEXT REFERENCES jobs(id))', ''),
    ('timelapses(print_job_id TEXT)', ''),
    ('legacy(entity_id TEXT,entity_type TEXT)', ", 'job'"),
    ('legacy(resource_id TEXT,resource_type TEXT)', ", 'jobs'"),
    ('legacy(resource_id TEXT,resource_kind TEXT)', ", 'job'"),
])
def test_all_legacy_sources_keep_numeric_text_references(minimal_engine, definition, extra):
    with minimal_engine.begin() as c:
        c.exec_driver_sql('CREATE TABLE ' + definition)
        table = definition.split('(', 1)[0]
        c.exec_driver_sql(f"INSERT INTO {table} VALUES('0901'{extra})")
        assert HISTORY.largest_referenced_job_id(c) == 901


@pytest.mark.parametrize('invalid', ['abc', b'999', 12.5, -1])
def test_validation_rejects_invalid_high_water_with_migration_error(minimal_engine, invalid):
    with minimal_engine.begin() as c:
        HISTORY.apply(c)
        c.exec_driver_sql('DROP TRIGGER jobs_high_water_prevent_lower')
        c.exec_driver_sql('PRAGMA ignore_check_constraints=ON')
        c.exec_driver_sql('UPDATE job_id_high_water SET high_water=?', (invalid,))
        with pytest.raises(MigrationError, match='nonnegative integer high_water'):
            HISTORY.validate(c)


def test_restore_reference_floor_and_merge_use_the_fixed_scanner(minimal_engine):
    from modules.system.sqlite_restore_identity import job_identity_floor, preserve_job_identity
    with minimal_engine.begin() as c:
        seed_references(c)
    source = minimal_engine.url.database
    assert job_identity_floor(source) == 300  # pre-055 backup
    preserve_job_identity(source, 400)
    assert job_identity_floor(source) == 400  # retain issued/deleted IDs, too
    with minimal_engine.begin() as c:
        HISTORY.apply(c)
        assert allocate_sqlite_job_id(c.exec_driver_sql) == 401


@pytest.fixture
def current_engine(tmp_path):
    engine = create_engine('sqlite:///' + str(tmp_path / 'current.db'))
    bootstrap_database(engine)
    yield engine
    engine.dispose()


def set_ledger(c, migration_id, checksum, dialect):
    c.execute(text('UPDATE odin_schema_migrations SET checksum=:checksum,dialect=:dialect '
                   'WHERE migration_id=:migration_id'),
              dict(checksum=checksum, dialect=dialect, migration_id=migration_id))


def assert_superseded_accepted(engine):
    with engine.begin() as c:
        set_ledger(c, HISTORY.MIGRATION_ID, OLD_CHECKSUM, engine.dialect.name)
        before = c.exec_driver_sql('SELECT * FROM odin_schema_migrations '
                                   "WHERE migration_id='python:055-controlled-history'").one()
        assert not apply_python_migration(c, MODULE)
    assert bootstrap_database(engine)['applied'] == []
    with engine.connect() as c:
        assert c.exec_driver_sql('SELECT * FROM odin_schema_migrations '
                                 "WHERE migration_id='python:055-controlled-history'").one() == before
        HISTORY.validate(c)


def test_superseded_055_checksum_is_already_applied_without_rewriting_ledger(current_engine):
    assert_superseded_accepted(current_engine)


@pytest.mark.parametrize('module,checksum,dialect', [
    (MODULE, '0' * 64, 'sqlite'),
    (MODULE, OLD_CHECKSUM, 'postgresql'),
    ('core.schema.migrations.054_controlled_parts', OLD_CHECKSUM, 'sqlite'),
])
def test_other_checksums_migrations_and_dialects_still_refused(current_engine, module, checksum, dialect):
    migration = importlib.import_module(module)
    with current_engine.begin() as c:
        set_ledger(c, migration.MIGRATION_ID, checksum, dialect)
        with pytest.raises(MigrationError, match='Migration history mismatch'):
            apply_python_migration(c, module)


def test_superseded_checksum_does_not_bypass_schema_validation(current_engine):
    with current_engine.begin() as c:
        set_ledger(c, HISTORY.MIGRATION_ID, OLD_CHECKSUM, 'sqlite')
        c.exec_driver_sql('ALTER TABLE controlled_retention_clocks ADD COLUMN unexpected TEXT')
        with pytest.raises(MigrationError, match='Controlled history column shape differs'):
            apply_python_migration(c, MODULE)


def test_restart_repairs_from_numeric_text_without_using_printer_ids(current_engine):
    with current_engine.begin() as c:
        c.exec_driver_sql("INSERT INTO printers(id,name) VALUES(1,'Printer')")
        c.exec_driver_sql("INSERT INTO print_jobs(printer_id,job_id,started_at) "
                          "VALUES(1,'999999',CURRENT_TIMESTAMP)")
        c.exec_driver_sql('CREATE TABLE legacy_reference(job_id TEXT)')
        c.exec_driver_sql("INSERT INTO legacy_reference VALUES('0300')")
        c.exec_driver_sql('DROP TRIGGER jobs_high_water_insert')
    assert bootstrap_database(current_engine)['applied'] == []
    with current_engine.connect() as c:
        HISTORY.validate(c)
        assert c.exec_driver_sql('SELECT high_water FROM job_id_high_water').scalar_one() == 300
        assert c.exec_driver_sql("SELECT count(*) FROM audit_logs WHERE action='job_identity.repaired'").scalar_one() == 1


def test_full_bootstrap_from_empty_054_tables_without_ledger(tmp_path):
    path = tmp_path / 'partial054.db'
    code = '''
import runpy,sys
runpy.run_path(sys.argv[1],run_name='frozen053')['prepare_sources']()
from sqlalchemy import create_engine
from core.schema import bootstrap_database
engine=create_engine('sqlite:///'+sys.argv[2])
bootstrap_database(engine)
engine.dispose()
'''
    run = subprocess.run([sys.executable, '-I', '-c', code,
                          str(Path(preserved053.__file__).with_name('runner.py')), str(path)],
                         cwd=tmp_path, capture_output=True, text=True, timeout=90)
    assert run.returncode == 0, run.stderr
    engine = create_engine('sqlite:///' + str(path))
    try:
        with engine.begin() as c:
            PARTS.apply(c)  # DDL persisted, but the failed release left no ledger row.
            tables = {'controlled_audiences', 'controlled_audience_members',
                      'controlled_files', 'controlled_jobs'}
            assert {t for t in inspect(c).get_table_names() if t.startswith('controlled_')} == tables
            for table in tables:
                assert c.exec_driver_sql(f'SELECT count(*) FROM {table}').scalar_one() == 0
            ids = set(c.exec_driver_sql('SELECT migration_id FROM odin_schema_migrations').scalars())
            assert ids == preserved053.ledger_ids()
            assert not (set(HISTORY.TABLES) & set(inspect(c).get_table_names()))
            c.exec_driver_sql("INSERT INTO printers(id,name) VALUES(1,'Production shape')")
            c.exec_driver_sql("INSERT INTO jobs(id,item_name) VALUES(221,'ODIN job')")
            c.exec_driver_sql('INSERT INTO print_jobs(printer_id,job_id,scheduled_job_id,started_at) '
                              'VALUES(1,?,221,CURRENT_TIMESTAMP)', [('0',), ('abc',), ('999999',)])
            assert str(next(col['type'] for col in inspect(c).get_columns('print_jobs')
                            if col['name'] == 'job_id')) == 'TEXT'
        assert bootstrap_database(engine)['applied'] == [PARTS.MIGRATION_ID, HISTORY.MIGRATION_ID]
        assert bootstrap_database(engine)['applied'] == []
        with engine.connect() as c:
            PARTS.validate(c)
            HISTORY.validate(c)
            assert c.exec_driver_sql('SELECT high_water FROM job_id_high_water').scalar_one() == 221
    finally:
        engine.dispose()


def test_postgres_superseded_055_checksum_keeps_native_identity(native_pg):
    bootstrap_database(native_pg)
    assert_superseded_accepted(native_pg)
    with native_pg.connect() as c:
        assert c.exec_driver_sql('SELECT count(*) FROM job_id_high_water').scalar_one() == 0
