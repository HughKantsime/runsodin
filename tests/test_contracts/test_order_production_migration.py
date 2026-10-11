"""Frozen production schema upgrades preserve existing data and roll back safely."""
import importlib
import pytest
from sqlalchemy import create_engine, inspect, text

migration = importlib.import_module('core.schema.migrations.005_order_production_snapshots')


def engine_with_legacy():
    engine = create_engine('sqlite://', hide_parameters=True)
    with engine.begin() as connection:
        connection.exec_driver_sql('CREATE TABLE orders(id INTEGER PRIMARY KEY)')
        connection.exec_driver_sql('CREATE TABLE order_items(id INTEGER PRIMARY KEY)')
        connection.exec_driver_sql('INSERT INTO orders VALUES(7)')
        connection.exec_driver_sql('INSERT INTO order_items VALUES(9)')
    return engine


def test_additive_upgrade_and_repeat_preserve_legacy_without_backfill():
    engine = engine_with_legacy()
    try:
        for _ in range(2):
            with engine.begin() as connection:
                migration.apply(connection)
                migration.validate(connection)
                assert connection.execute(text('SELECT id FROM orders')).scalar_one() == 7
                for name in migration.TABLES:
                    assert connection.execute(text(f'SELECT COUNT(*) FROM {name}')).scalar_one() == 0
    finally:
        engine.dispose()


@pytest.mark.parametrize('bad_table', migration.TABLES)
def test_malformed_existing_schema_rejects_before_creating_missing_tables(bad_table):
    engine = engine_with_legacy()
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(f'CREATE TABLE {bad_table}(incorrect INTEGER)')
        with pytest.raises(RuntimeError, match='column shape mismatch'):
            with engine.begin() as connection:
                migration.apply(connection)
        with engine.connect() as connection:
            assert set(inspect(connection).get_table_names()) == {'orders', 'order_items', bad_table}
    finally:
        engine.dispose()


def test_upgrade_creation_rolls_back_with_enclosing_transaction():
    engine = engine_with_legacy()
    try:
        with pytest.raises(RuntimeError, match='synthetic enclosing failure'):
            with engine.begin() as connection:
                migration.apply(connection)
                migration.validate(connection)
                raise RuntimeError('synthetic enclosing failure')
        with engine.connect() as connection:
            assert set(inspect(connection).get_table_names()) == {'orders', 'order_items'}
            assert connection.execute(text('SELECT id FROM orders')).scalar_one() == 7
    finally:
        engine.dispose()


def test_print_measurements_upgrade_repeat_and_rollback():
    measurements = importlib.import_module('core.schema.migrations.006_print_job_measurements')
    engine = create_engine('sqlite://', hide_parameters=True)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql('CREATE TABLE print_jobs(id INTEGER PRIMARY KEY, status TEXT)')
            connection.exec_driver_sql("INSERT INTO print_jobs VALUES(8,'completed')")
        with pytest.raises(RuntimeError, match='synthetic rollback'):
            with engine.begin() as connection:
                measurements.apply(connection)
                raise RuntimeError('synthetic rollback')
        with engine.connect() as connection:
            assert {c['name'] for c in inspect(connection).get_columns('print_jobs')} == {'id', 'status'}
        for _ in range(2):
            with engine.begin() as connection:
                measurements.apply(connection)
                measurements.validate(connection)
                assert connection.execute(text('SELECT id,status,print_duration_seconds,filament_used_g FROM print_jobs')).one() == (8, 'completed', None, None)
    finally:
        engine.dispose()
