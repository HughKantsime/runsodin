"""AMS percentage storage and upgrade contracts on the two supported databases."""
import importlib

import pytest
from sqlalchemy import create_engine, text

from core.schema import bootstrap_database
from core.schema.migrator import MigrationError, apply_python_migration
from tests.test_contracts.test_education_classes_migration import pg_engine

M = importlib.import_module('core.schema.migrations.062_ams_humidity_percent')


def check_storage_and_upgrade(engine):
    first = bootstrap_database(engine)
    assert M.MIGRATION_ID in first['applied']
    with engine.begin() as c:
        # Recreate a 061 database holding one historical AMS sample.
        c.exec_driver_sql('ALTER TABLE ams_telemetry DROP COLUMN humidity_percent')
        c.execute(text('DELETE FROM odin_schema_migrations WHERE migration_id=:id'), {'id': M.MIGRATION_ID})
        c.exec_driver_sql('INSERT INTO ams_telemetry(printer_id,ams_unit,humidity) VALUES(1,0,2)')
    upgrade = bootstrap_database(engine)
    assert upgrade['applied'] == [M.MIGRATION_ID]
    with engine.begin() as c:
        M.validate(c)
        c.exec_driver_sql('INSERT INTO ams_telemetry(printer_id,ams_unit,humidity,humidity_percent) VALUES(1,0,2,37.75)')
        assert c.exec_driver_sql('SELECT humidity,humidity_percent FROM ams_telemetry ORDER BY id').all() == [
            (2, None), (2, 37.75)]
    second = bootstrap_database(engine)
    assert second['applied'] == []
    assert first['schema_fingerprint'] == upgrade['schema_fingerprint'] == second['schema_fingerprint']


def test_sqlite_stores_percentage_and_keeps_pre062_rows_unknown():
    engine = create_engine('sqlite://')
    try:
        check_storage_and_upgrade(engine)
    finally:
        engine.dispose()


@pytest.mark.postgresql
def test_postgresql_stores_percentage_and_keeps_pre062_rows_unknown(pg_engine):
    check_storage_and_upgrade(pg_engine)


@pytest.mark.parametrize('definition', ['INTEGER', 'REAL NOT NULL', 'REAL DEFAULT 0'])
def test_incompatible_column_refused_without_recording_062(definition):
    engine = create_engine('sqlite://')
    try:
        with engine.begin() as c:
            c.exec_driver_sql('CREATE TABLE ams_telemetry(humidity_percent ' + definition + ')')
            with pytest.raises(MigrationError, match='column shape differs'):
                apply_python_migration(c, M.__name__)
            assert c.execute(text('SELECT 1 FROM odin_schema_migrations WHERE migration_id=:id'),
                             {'id': M.MIGRATION_ID}).first() is None
    finally:
        engine.dispose()
