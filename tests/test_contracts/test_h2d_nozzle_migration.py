"""Owned SQLite adoption and rerun proof for nullable nozzle observations."""
import importlib
import pytest
from sqlalchemy import create_engine, text

@pytest.mark.parametrize('existing', [False, True])
def test_nozzle_migration_preserves_unknown_legacy_rows_and_reruns(existing):
    migration=importlib.import_module('core.schema.migrations.010_h2d_nozzle_observations')
    engine=create_engine('sqlite:///:memory:')
    with engine.begin() as conn:
        conn.execute(text('CREATE TABLE printers(id INTEGER PRIMARY KEY)'))
        conn.execute(text('INSERT INTO printers VALUES(1)'))
        if existing:
            for name,definition in migration.COLUMNS.items():conn.execute(text(f'ALTER TABLE printers ADD COLUMN {name} {definition}'))
        migration.apply(conn);migration.apply(conn)
        assert tuple(conn.execute(text('SELECT nozzle_1_temp,nozzle_1_target_temp,nozzle_1_temp_observed_at,nozzle_1_target_observed_at FROM printers')).one())==(None,None,None,None)
    engine.dispose()

@pytest.mark.parametrize('definition', ['TEXT', 'FLOAT NOT NULL', 'FLOAT DEFAULT 0'])
def test_nozzle_migration_refuses_wrong_adoption_before_adding_columns(definition):
    migration=importlib.import_module('core.schema.migrations.010_h2d_nozzle_observations')
    engine=create_engine('sqlite:///:memory:')
    with engine.begin() as conn:
        conn.execute(text(f'CREATE TABLE printers(id INTEGER PRIMARY KEY, nozzle_1_temp {definition})'))
        with pytest.raises(Exception):migration.apply(conn)
        assert [row[1] for row in conn.execute(text('PRAGMA table_info(printers)'))]==['id','nozzle_1_temp']
    engine.dispose()
