"""Additive spool identity migration and immutable prior ORM manifests."""
import importlib
import json
from pathlib import Path
import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from core.schema import bootstrap_database
from core.schema.migrator import MigrationError
from tests.test_contracts.test_spool_rfid_identity import identity_engine, postgres_server

M = importlib.import_module('core.schema.migrations.044_spool_tray_uuid')


def test_fresh_repeat_and_unique_nullable(identity_engine):
    from core.schema.bootstrap import _orm_manifest
    assert bootstrap_database(identity_engine)['applied'] == []
    with identity_engine.begin() as c:
        M.apply(c)
        M.validate(c)
        assert set(_orm_manifest()['spools']) <= {col['name'] for col in inspect(c).get_columns('spools')}
        assert {'tray_uuid', 'remaining_measured_at', 'remaining_measured_g', 'remaining_measured_printer_id'} <= set(_orm_manifest()['spools'])
        assert c.execute(text('SELECT count(*) FROM odin_schema_migrations WHERE migration_id=:id'), {'id':M.MIGRATION_ID}).scalar_one() == 1
        c.execute(text("INSERT INTO filament_library(id,brand,name,material) VALUES(1,'Bambu Lab','PLA Matte','PLA')"))
        c.execute(text('INSERT INTO spools(id,filament_id,tray_uuid) VALUES(1,1,NULL),(2,1,NULL),(3,1,:uuid)'), {'uuid':'a'*32})
        with pytest.raises(IntegrityError):
            with c.begin_nested():
                c.execute(text('INSERT INTO spools(id,filament_id,tray_uuid) VALUES(4,1,:uuid)'), {'uuid':'a'*32})


def test_pre044_upgrade_retains_rows_and_no_backfill(identity_engine):
    with identity_engine.begin() as c:
        c.exec_driver_sql('DROP INDEX ix_spools_tray_uuid')
        c.exec_driver_sql('ALTER TABLE spools DROP COLUMN tray_uuid')
        c.exec_driver_sql('ALTER TABLE spools DROP COLUMN remaining_measured_at')
        c.exec_driver_sql('ALTER TABLE spools DROP COLUMN remaining_measured_g')
        c.exec_driver_sql('ALTER TABLE spools DROP COLUMN remaining_measured_printer_id')
        c.execute(text('DELETE FROM odin_schema_migrations WHERE migration_id=:id'), {'id':M.MIGRATION_ID})
        c.execute(text("INSERT INTO filament_library(id,brand,name) VALUES(1,'Bambu Lab','PLA Matte')"))
        c.execute(text("INSERT INTO spools(id,filament_id,rfid_tag,remaining_weight_g) VALUES(56,1,'fixture-tag',412)"))
    assert bootstrap_database(identity_engine)['applied'] == [M.MIGRATION_ID]
    with identity_engine.connect() as c:
        assert c.execute(text('SELECT id,rfid_tag,remaining_weight_g,tray_uuid,remaining_measured_at,remaining_measured_g,remaining_measured_printer_id FROM spools')).one() == (56,'fixture-tag',412,None,None,None,None)


@pytest.mark.parametrize('drift', [
    'DROP INDEX ix_spools_tray_uuid',
    'CREATE INDEX ix_spools_tray_uuid ON spools(tray_uuid)',
    'CREATE UNIQUE INDEX ix_spools_tray_uuid ON spools(tray_uuid) WHERE tray_uuid IS NOT NULL',
    'CREATE UNIQUE INDEX ix_spools_tray_uuid ON spools(rfid_tag)',
])
def test_bad_index_shape_not_adopted(identity_engine, drift):
    with identity_engine.begin() as c:
        c.exec_driver_sql('DROP INDEX ix_spools_tray_uuid')
        c.execute(text('DELETE FROM odin_schema_migrations WHERE migration_id=:id'), {'id':M.MIGRATION_ID})
        if drift != 'DROP INDEX ix_spools_tray_uuid':
            c.exec_driver_sql(drift)
    with pytest.raises(MigrationError):
        bootstrap_database(identity_engine)
    with identity_engine.connect() as c:
        assert c.execute(text('SELECT count(*) FROM odin_schema_migrations WHERE migration_id=:id'), {'id':M.MIGRATION_ID}).scalar_one() == 0


@pytest.mark.parametrize('ddl', [
    'CREATE TABLE spools(id INTEGER PRIMARY KEY, tray_uuid TEXT)',
    'CREATE TABLE spools(id INTEGER PRIMARY KEY, tray_uuid VARCHAR(32) NOT NULL)',
    "CREATE TABLE spools(id INTEGER PRIMARY KEY, tray_uuid VARCHAR(32) DEFAULT 'bad')",
])
def test_bad_column_shape(tmp_path, ddl):
    engine = create_engine('sqlite:///' + str(tmp_path/'bad.db'))
    try:
        with engine.begin() as c:
            c.exec_driver_sql(ddl)
            c.exec_driver_sql('CREATE UNIQUE INDEX ix_spools_tray_uuid ON spools(tray_uuid)')
            with pytest.raises(MigrationError):
                M.apply(c)
    finally:
        engine.dispose()


def test_prior_manifests_unchanged_and_new_baseline_pinned():
    import hashlib
    from core.schema.bootstrap import CURRENT_ORM_BASELINE, _orm_manifest
    catalog = json.loads(Path('backend/core/schema/orm_baselines.json').read_text())
    assert 'tray_uuid' not in catalog['release_2_0_24_merged_043_20261027']['manifest']['spools']
    expected = hashlib.sha256(json.dumps(_orm_manifest(), sort_keys=True, separators=(',',':')).encode()).hexdigest()
    assert catalog[CURRENT_ORM_BASELINE]['checksum'] == expected


@pytest.mark.parametrize('ddl', [
    'CREATE TABLE spools(id INTEGER PRIMARY KEY, tray_uuid VARCHAR(32), remaining_measured_at TEXT)',
    'CREATE TABLE spools(id INTEGER PRIMARY KEY, tray_uuid VARCHAR(32), remaining_measured_g FLOAT NOT NULL)',
    'CREATE TABLE spools(id INTEGER PRIMARY KEY, tray_uuid VARCHAR(32), remaining_measured_printer_id INTEGER DEFAULT 1)',
])
def test_bad_measurement_basis_shape(tmp_path, ddl):
    engine = create_engine('sqlite:///' + str(tmp_path/'basis.db'))
    try:
        with engine.begin() as c:
            c.exec_driver_sql(ddl)
            c.exec_driver_sql('CREATE UNIQUE INDEX ix_spools_tray_uuid ON spools(tray_uuid)')
            with pytest.raises(MigrationError, match='measurement basis'):
                M.apply(c)
    finally:
        engine.dispose()
