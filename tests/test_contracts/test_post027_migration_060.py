"""Shared file-path index: additive upgrade, exact shape and lookup plan."""
import hashlib
import importlib
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text
from core.schema import bootstrap_database
from core.schema.migrator import MigrationError, apply_python_migration

M = importlib.import_module('core.schema.migrations.060_print_files_stored_path_index')


@pytest.fixture
def engine(tmp_path):
    engine = create_engine('sqlite:///' + str(tmp_path / 'paths.db'))
    yield engine
    engine.dispose()


def test_creates_index_without_rewriting_rows_and_repeat_is_noop(engine):
    with engine.begin() as c:
        c.exec_driver_sql('CREATE TABLE print_files(id INTEGER PRIMARY KEY, stored_path TEXT)')
        c.exec_driver_sql("INSERT INTO print_files VALUES(1,'/shared'),(2,'/shared'),(3,NULL)")
        before = c.exec_driver_sql('SELECT * FROM print_files ORDER BY id').all()
        changes = c.connection.driver_connection.total_changes
        M.apply(c)
        index = next(i for i in inspect(c).get_indexes('print_files') if i['name'] == M.INDEX)
        assert index['column_names'] == ['stored_path'] and not index['unique']
        schema = c.exec_driver_sql('SELECT type,name,sql FROM sqlite_master ORDER BY name').all()
        M.apply(c)
        M.validate(c)
        assert c.exec_driver_sql('SELECT type,name,sql FROM sqlite_master ORDER BY name').all() == schema
        assert c.exec_driver_sql('SELECT * FROM print_files ORDER BY id').all() == before
        assert c.connection.driver_connection.total_changes == changes


def test_validate_refuses_missing_index(engine):
    with engine.begin() as c:
        c.exec_driver_sql('CREATE TABLE print_files(id INTEGER PRIMARY KEY, stored_path TEXT)')
        with pytest.raises(MigrationError):
            M.validate(c)


@pytest.mark.parametrize('definition', [
    'INDEX ix_print_files_stored_path ON print_files(id)',
    'INDEX ix_print_files_stored_path ON print_files(stored_path,id)',
    'UNIQUE INDEX ix_print_files_stored_path ON print_files(stored_path)',
    'INDEX ix_print_files_stored_path ON print_files(stored_path) WHERE stored_path IS NOT NULL',
    'INDEX ix_print_files_stored_path ON other_files(stored_path)',
])
def test_wrong_shape_refused_without_ledger(engine, definition):
    with engine.begin() as c:
        c.exec_driver_sql('CREATE TABLE print_files(id INTEGER PRIMARY KEY, stored_path TEXT)')
        c.exec_driver_sql('CREATE TABLE other_files(stored_path TEXT)')
        c.exec_driver_sql('CREATE ' + definition)
        with pytest.raises(MigrationError):
            M.validate(c)
        with pytest.raises(MigrationError):
            apply_python_migration(c, M.__name__)
        assert c.execute(text('SELECT count(*) FROM odin_schema_migrations WHERE migration_id=:id'),
                         {'id': M.MIGRATION_ID}).scalar_one() == 0


def test_stored_path_lookup_uses_index(engine):
    with engine.begin() as c:
        c.exec_driver_sql('CREATE TABLE print_files(id INTEGER PRIMARY KEY, stored_path TEXT)')
        M.apply(c)
        plan = c.execute(text('EXPLAIN QUERY PLAN SELECT id FROM print_files WHERE stored_path = :p'),
                         {'p': '/shared'}).all()
        assert any('SEARCH' in row[3] and M.INDEX in row[3] for row in plan)


def test_fresh_repeat_checksum_and_upgrade(engine):
    first = bootstrap_database(engine)
    assert M.MIGRATION_ID in first['applied']
    second = bootstrap_database(engine)
    assert second['applied'] == [] and second['schema_fingerprint'] == first['schema_fingerprint']
    with engine.begin() as c:
        M.validate(c)
        assert c.execute(text('SELECT checksum FROM odin_schema_migrations WHERE migration_id=:id'),
                         {'id': M.MIGRATION_ID}).scalar_one() == hashlib.sha256(Path(M.__file__).read_bytes()).hexdigest()
        c.exec_driver_sql('DROP INDEX ix_print_files_stored_path')
        c.execute(text('DELETE FROM odin_schema_migrations WHERE migration_id=:id'), {'id': M.MIGRATION_ID})
        c.exec_driver_sql("INSERT INTO print_files(id,filename,stored_path) VALUES(990060,'Shared','/shared'),(990061,'Alias','/shared')")
        before = c.exec_driver_sql('SELECT * FROM print_files ORDER BY id').all()
    assert bootstrap_database(engine)['applied'] == [M.MIGRATION_ID]
    assert bootstrap_database(engine)['applied'] == []
    with engine.connect() as c:
        M.validate(c)
        assert c.exec_driver_sql('SELECT * FROM print_files ORDER BY id').all() == before


def test_published_1924_entrypoint_upgrade(tmp_path):
    from tests.test_contracts.test_post027_migration_032 import test_published_v1_9_24_database_upgrades_through_032_with_the_entrypoint_command
    test_published_v1_9_24_database_upgrades_through_032_with_the_entrypoint_command(tmp_path)
    engine = create_engine('sqlite:///' + str(tmp_path / 'odin.db'))
    try:
        with engine.connect() as c:
            M.validate(c)
            assert c.execute(text('SELECT count(*) FROM odin_schema_migrations WHERE migration_id=:id'),
                             {'id': M.MIGRATION_ID}).scalar_one() == 1
    finally:
        engine.dispose()
