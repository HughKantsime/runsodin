"""Carry SQLite job identities across an offline restore's rollback-safe swap."""
import importlib

from sqlalchemy import create_engine, inspect
from sqlalchemy.pool import NullPool


def job_identity_floor(database):
    """Read only, after writers stop: include IDs issued since staging the backup."""
    history = importlib.import_module('core.schema.migrations.055_controlled_history')
    engine = create_engine('sqlite:///' + str(database), poolclass=NullPool)
    try:
        with engine.connect() as connection:
            floor = history.largest_referenced_job_id(connection)
            if 'job_id_high_water' in inspect(connection).get_table_names():
                floor = max(floor, connection.exec_driver_sql(
                    'SELECT COALESCE(max(high_water),0) FROM job_id_high_water').scalar_one())
            return floor
    finally:
        engine.dispose()


def preserve_job_identity(database, floor):
    """Also supports pre-055 backups; startup will apply the remaining migration."""
    history = importlib.import_module('core.schema.migrations.055_controlled_history')
    engine = create_engine('sqlite:///' + str(database), poolclass=NullPool)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql('CREATE TABLE IF NOT EXISTS job_id_high_water '
                '(singleton INTEGER NOT NULL PRIMARY KEY, high_water INTEGER NOT NULL, '
                'CHECK (singleton=1 AND high_water>=0))')
            connection.exec_driver_sql('INSERT INTO job_id_high_water(singleton,high_water) VALUES(1,?) '
                'ON CONFLICT(singleton) DO UPDATE SET high_water=max(high_water,excluded.high_water)', (floor,))
            history.initialize_job_ids(connection)
    finally:
        engine.dispose()
