"""Native filament enum upgrade; no table rebuild or material relabelling."""
import hashlib
import importlib
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, text
from core.schema import bootstrap_database
from core.schema.migrator import MigrationError
from tests.test_contracts.test_post027_migration_040 import pg_engine

M = importlib.import_module('core.schema.migrations.059_six_materials')


@pytest.fixture
def engine(tmp_path):
    engine = create_engine('sqlite:///' + str(tmp_path/'six.db'))
    yield engine
    engine.dispose()


def test_fresh_repeat_checksum_and_enum_pin(engine):
    from core.base import FilamentType
    assert M.CURRENT == tuple(item.value for item in FilamentType)
    first = bootstrap_database(engine)
    second = bootstrap_database(engine)
    assert M.MIGRATION_ID in first['applied']
    assert second['applied'] == [] and first['schema_fingerprint'] == second['schema_fingerprint']
    with engine.begin() as c:
        statements = []
        def record(conn, cursor, sql, *args): statements.append(sql)
        event.listen(c, 'before_cursor_execute', record)
        M.apply(c); M.apply(c); M.validate(c)
        event.remove(c, 'before_cursor_execute', record)
        assert not any(s.split()[0].upper() in ('ALTER', 'CREATE', 'DROP', 'UPDATE', 'DELETE', 'INSERT') for s in statements)
        assert c.execute(text('SELECT checksum FROM odin_schema_migrations WHERE migration_id=:id'),
            {'id': M.MIGRATION_ID}).scalar_one() == hashlib.sha256(Path(M.__file__).read_bytes()).hexdigest()


def test_upgrade_preserves_real_family_rows_and_frozen_evidence(engine):
    bootstrap_database(engine)
    with engine.begin() as c:
        c.execute(text('DELETE FROM odin_schema_migrations WHERE migration_id=:id'), {'id': M.MIGRATION_ID})
        c.exec_driver_sql("INSERT INTO printers(id,name) VALUES(901,'Fixture')")
        for n, label in enumerate(('ASA','TPU','PETG_CF','NYLON_CF','PPS_CF'), 1):
            c.execute(text('INSERT INTO filament_slots(printer_id,slot_number,filament_type) VALUES(901,:n,:label)'), dict(n=n,label=label))
        before = c.exec_driver_sql('SELECT * FROM filament_slots ORDER BY id').all()
        evidence = c.exec_driver_sql("SELECT sql FROM sqlite_master WHERE name='ams_report_trays'").scalar_one()
    assert bootstrap_database(engine)['applied'] == [M.MIGRATION_ID]
    assert bootstrap_database(engine)['applied'] == []
    with engine.connect() as c:
        assert c.exec_driver_sql('SELECT * FROM filament_slots ORDER BY id').all() == before
        assert c.exec_driver_sql("SELECT sql FROM sqlite_master WHERE name='ams_report_trays'").scalar_one() == evidence


@pytest.mark.parametrize('table,column', M.COLUMNS)
def test_drift_refused_without_ledger(engine, table, column):
    bootstrap_database(engine)
    with engine.begin() as c:
        c.execute(text('DELETE FROM odin_schema_migrations WHERE migration_id=:id'), {'id':M.MIGRATION_ID})
        c.exec_driver_sql(f'ALTER TABLE {table} RENAME COLUMN {column} TO wrong_name')
    with pytest.raises(MigrationError): bootstrap_database(engine)
    with engine.connect() as c:
        assert c.execute(text('SELECT count(*) FROM odin_schema_migrations WHERE migration_id=:id'), {'id':M.MIGRATION_ID}).scalar_one() == 0


def test_published_1924_entrypoint_upgrade(tmp_path):
    from tests.test_contracts.test_post027_migration_032 import test_published_v1_9_24_database_upgrades_through_032_with_the_entrypoint_command
    test_published_v1_9_24_database_upgrades_through_032_with_the_entrypoint_command(tmp_path)
    engine = create_engine('sqlite:///' + str(tmp_path/'odin.db'))
    try:
        with engine.connect() as c:
            M.validate(c)
            assert c.execute(text('SELECT count(*) FROM odin_schema_migrations WHERE migration_id=:id'), {'id':M.MIGRATION_ID}).scalar_one() == 1
    finally: engine.dispose()


def test_native_postgres_upgrade_preserves_rows_and_accepts_new_values(pg_engine):
    bootstrap_database(pg_engine)
    with pg_engine.begin() as c:
        c.exec_driver_sql('ALTER TYPE public.filamenttype RENAME TO filamenttype_current_test')
        c.exec_driver_sql('CREATE TYPE public.filamenttype AS ENUM (' + ','.join("'"+v+"'" for v in M.LEGACY) + ')')
        for table, column in M.COLUMNS:
            c.exec_driver_sql(f'ALTER TABLE {table} ALTER COLUMN {column} TYPE public.filamenttype USING {column}::text::public.filamenttype')
        c.exec_driver_sql('DROP TYPE public.filamenttype_current_test')
        c.execute(text('DELETE FROM odin_schema_migrations WHERE migration_id=:id'), {'id':M.MIGRATION_ID})
        c.exec_driver_sql("INSERT INTO printers(id,name) VALUES(901,'Fixture')")
        c.exec_driver_sql("INSERT INTO filament_slots(printer_id,slot_number,filament_type) VALUES(901,1,'ASA')")
    assert bootstrap_database(pg_engine)['applied'] == [M.MIGRATION_ID]
    assert bootstrap_database(pg_engine)['applied'] == []
    with pg_engine.begin() as c:
        M.validate(c)
        assert c.exec_driver_sql('SELECT filament_type::text FROM filament_slots WHERE printer_id=901').scalar_one() == 'ASA'
        for n, label in enumerate(M.ADDED, 2):
            c.execute(text('INSERT INTO filament_slots(printer_id,slot_number,filament_type) VALUES(901,:n,:label)'), dict(n=n,label=label))
        assert c.exec_driver_sql('SELECT filament_type::text FROM filament_slots WHERE printer_id=901 ORDER BY slot_number').scalars().all() == ['ASA', *M.ADDED]


def test_native_postgres_enum_drift_refuses_without_ledger(pg_engine):
    bootstrap_database(pg_engine)
    with pg_engine.begin() as c:
        c.exec_driver_sql("ALTER TYPE public.filamenttype ADD VALUE 'unexpected'")
        c.execute(text('DELETE FROM odin_schema_migrations WHERE migration_id=:id'), {'id':M.MIGRATION_ID})
    with pytest.raises(MigrationError): bootstrap_database(pg_engine)
    with pg_engine.connect() as c:
        assert c.execute(text('SELECT count(*) FROM odin_schema_migrations WHERE migration_id=:id'), {'id':M.MIGRATION_ID}).scalar_one() == 0
