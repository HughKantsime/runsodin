"""Additive color-name migration, retained rows and repeat boot."""
import importlib
import pytest
from sqlalchemy import create_engine, inspect, text
from core.schema import bootstrap_database
from core.schema.migrator import MigrationError
M = importlib.import_module('core.schema.migrations.041_inventory_color')


def test_fresh_and_repeat_boot(tmp_path):
    engine = create_engine('sqlite:///' + str(tmp_path/'color.db'))
    try:
        first = bootstrap_database(engine)
        start = first['applied'].index('python:040-job-foundation')
        assert first['applied'][start:start + 3] == ['python:040-job-foundation', M.MIGRATION_ID, 'python:042-dispatch-capability']
        assert inspect(engine).get_columns('printers')[-1]['name'] == 'installed_nozzles'
        assert bootstrap_database(engine)['applied'] == []
        with engine.begin() as conn:
            M.validate(conn)
            assert conn.execute(text('SELECT COUNT(*) FROM odin_schema_migrations WHERE migration_id=:id'), {'id':M.MIGRATION_ID}).scalar_one() == 1
    finally: engine.dispose()


def test_pre041_rows_preserved_and_column_last(tmp_path):
    engine = create_engine('sqlite:///' + str(tmp_path/'upgrade.db'))
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql('CREATE TABLE spools(id INTEGER PRIMARY KEY, color_hex VARCHAR(6))')
            conn.exec_driver_sql("INSERT INTO spools VALUES(1,'123ABC')")
            M.apply(conn); M.apply(conn)
            assert conn.execute(text('SELECT * FROM spools')).one() == (1,'123ABC',None)
            assert inspect(conn).get_columns('spools')[-1]['name'] == 'color_name'
    finally: engine.dispose()


def test_wrong_shape_is_refused_without_ledger(tmp_path):
    engine = create_engine('sqlite:///' + str(tmp_path/'drift.db'))
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql('CREATE TABLE spools(id INTEGER PRIMARY KEY, color_name INTEGER)')
            with pytest.raises(MigrationError): M.apply(conn)
            assert not inspect(conn).has_table('odin_schema_migrations')
    finally: engine.dispose()


def test_published_1924_entrypoint_upgrade(tmp_path):
    from tests.test_contracts.test_post027_migration_032 import test_published_v1_9_24_database_upgrades_through_032_with_the_entrypoint_command
    test_published_v1_9_24_database_upgrades_through_032_with_the_entrypoint_command(tmp_path)
    engine = create_engine('sqlite:///' + str(tmp_path/'odin.db'))
    try:
        with engine.connect() as conn: M.validate(conn)
    finally: engine.dispose()
