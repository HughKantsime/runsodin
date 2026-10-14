"""Actual application bootstrap registration, shared by SQLite and owned native PG."""
import hashlib
import importlib
from pathlib import Path

from sqlalchemy import create_engine, inspect, text

from core.base import Base
from core.schema.bootstrap import (
    RAW_REQUIRED_COLUMNS, RAW_REQUIRED_TABLES, bootstrap_database, schema_fingerprint,
)
from core.schema.migrator import apply_python_migration
from core.timelapse_publication_schema import TABLES
from modules.system.postgres_backup_service import canonical_tables

MODULE = 'core.schema.migrations.019_timelapse_publications'


def verify_registration(engine):
    first = bootstrap_database(engine)
    module = importlib.import_module(MODULE)
    checksum = hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
    names = {table.name for table in TABLES}
    assert names <= RAW_REQUIRED_TABLES
    assert names.isdisjoint(Base.metadata.tables)
    assert names <= canonical_tables()
    with engine.connect() as db:
        inventory = set(inspect(db).get_table_names())
        assert names <= inventory
        assert set(canonical_tables()) <= inventory
        canonical_extras = sorted(inventory - set(canonical_tables()))
        for table in TABLES:
            expected = {column.name for column in table.columns}
            assert RAW_REQUIRED_COLUMNS[table.name] == expected
            assert {column['name'] for column in inspect(db).get_columns(table.name)} == expected
            assert db.execute(text('SELECT count(*) FROM '+table.name)).scalar_one() == 0
        history = db.execute(text('SELECT migration_id,checksum,dialect,applied_at FROM odin_schema_migrations ORDER BY migration_id')).all()
        record = next(row for row in history if row[0] == module.MIGRATION_ID)
        assert record[1:3] == (checksum, engine.dialect.name)
        fingerprint = schema_fingerprint(db)
    assert 'python:019-timelapse-publications' in first['applied']
    assert first['applied'].index('python:018-timelapse-unlinks') < first['applied'].index('python:019-timelapse-publications') < first['applied'].index('python:020-ams-evidence')
    second = bootstrap_database(engine)
    assert second['applied'] == []
    assert second['schema_fingerprint'] == first['schema_fingerprint'] == fingerprint
    with engine.connect() as db:
        transaction = db.begin()
        assert apply_python_migration(db, MODULE) is False
        assert db.in_transaction() and transaction.is_active
        transaction.rollback()
        assert db.execute(text('SELECT migration_id,checksum,dialect,applied_at FROM odin_schema_migrations ORDER BY migration_id')).all() == history
        assert schema_fingerprint(db) == fingerprint
    # Exercise the registered first-application path inside a caller-owned
    # transaction. Rollback restores the original full application inventory.
    with engine.connect() as db:
        transaction = db.begin()
        if engine.dialect.name == 'sqlite':
            db.exec_driver_sql('BEGIN IMMEDIATE')
        for table in reversed(TABLES):
            table.drop(db)
        db.execute(text('DELETE FROM odin_schema_migrations WHERE migration_id=:id'), {'id': module.MIGRATION_ID})
        assert apply_python_migration(db, MODULE) is True
        assert transaction.is_active and db.in_transaction()
        transaction.rollback()
        assert db.execute(text('SELECT migration_id,checksum,dialect,applied_at FROM odin_schema_migrations ORDER BY migration_id')).all() == history
        assert schema_fingerprint(db) == fingerprint
    return {'passed': True, 'dialect': engine.dialect.name, 'inventory_count': len(inventory),
            'schema_fingerprint': fingerprint, 'migration_checksum': checksum,
            'repeat_bootstrap_applied': second['applied'], 'caller_transaction_preserved': True,
            'history_unchanged': True, 'canonical_019_coverage': True, 'existing_inventory_extras': canonical_extras}


def test_actual_sqlite_registration(tmp_path):
    engine = create_engine('sqlite:///'+str(tmp_path/'registration.sqlite'))
    try:
        assert verify_registration(engine)['passed']
    finally:
        engine.dispose()
