"""Additive catalogue schema, retained rows, checksummed ledger and dialect DDL."""
import hashlib
import importlib
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.exc import IntegrityError
from core.schema import bootstrap_database
from core.schema.migrator import MigrationError, apply_python_migration

M = importlib.import_module('core.schema.migrations.058_filament_catalogue')


def test_fresh_repeat_ledger(tmp_path):
    engine = create_engine('sqlite:///'+str(tmp_path/'fresh.db'))
    assert M.MIGRATION_ID in bootstrap_database(engine)['applied']
    from core.schema.bootstrap import schema_fingerprint
    with engine.connect() as c:
        before = schema_fingerprint(c)
    assert bootstrap_database(engine)['applied'] == []
    with engine.connect() as c:
        assert schema_fingerprint(c) == before
        M.validate(c)
        checksum = c.execute(text('SELECT checksum FROM odin_schema_migrations WHERE migration_id=:id'),{'id':M.MIGRATION_ID}).scalar_one()
        assert checksum == hashlib.sha256(Path(M.__file__).read_bytes()).hexdigest()


def test_upgrade_is_only_additive_and_preserves_rows(tmp_path):
    engine = create_engine('sqlite:///'+str(tmp_path/'legacy.db'))
    with engine.begin() as c:
        c.exec_driver_sql('CREATE TABLE filament_library(id INTEGER PRIMARY KEY, brand VARCHAR NOT NULL, name VARCHAR NOT NULL)')
        c.exec_driver_sql("INSERT INTO filament_library VALUES(1,'Synthetic','Legacy'),(2,'Synthetic','Legacy duplicate')")
        statements = []
        def record(conn,cursor,sql,*args): statements.append(sql)
        event.listen(c,'before_cursor_execute',record)
        M.apply(c); M.apply(c)
        event.remove(c,'before_cursor_execute',record)
        ddl = [s for s in statements if s.split()[0].upper() in ('ALTER','CREATE','DROP')]
        assert len([s for s in ddl if s.startswith('ALTER TABLE filament_library ADD COLUMN')]) == 3
        assert all(s.startswith(('ALTER TABLE filament_library ADD COLUMN','CREATE UNIQUE INDEX IF NOT EXISTS')) for s in ddl)
        assert c.exec_driver_sql('SELECT id,name,catalogue_type,catalogue_code,catalogue_colors FROM filament_library').all() == [(1,'Legacy',None,None,None),(2,'Legacy duplicate',None,None,None)]
        c.exec_driver_sql("UPDATE filament_library SET catalogue_code='ODIN-TEST-ONE' WHERE id=1")
        with pytest.raises(IntegrityError):
            c.exec_driver_sql("UPDATE filament_library SET catalogue_code='ODIN-TEST-ONE' WHERE id=2")


@pytest.mark.parametrize('wrong',['column','index'])
def test_wrong_shape_has_no_ledger(tmp_path,wrong):
    engine = create_engine('sqlite:///'+str(tmp_path/'bad.db'))
    with engine.begin() as c:
        c.exec_driver_sql('CREATE TABLE filament_library(id INTEGER PRIMARY KEY,brand VARCHAR,catalogue_code '+('INTEGER' if wrong=='column' else 'VARCHAR(100)')+')')
        if wrong == 'index':
            c.exec_driver_sql('CREATE INDEX uq_filament_catalogue_code ON filament_library(catalogue_code)')
        with pytest.raises(MigrationError):
            apply_python_migration(c,M.__name__)
        assert c.execute(text('SELECT count(*) FROM odin_schema_migrations WHERE migration_id=:id'),{'id':M.MIGRATION_ID}).scalar() == 0


def test_postgresql_json_and_partial_unique_ddl():
    from sqlalchemy.schema import CreateTable, CreateIndex
    from sqlalchemy.dialects import postgresql
    from modules.inventory.models import FilamentLibrary
    dialect = postgresql.dialect()
    ddl = str(CreateTable(FilamentLibrary.__table__).compile(dialect=dialect))
    assert 'catalogue_colors JSON' in ddl
    assert 'catalogue_type VARCHAR(100)' in ddl
    index = next(i for i in FilamentLibrary.__table__.indexes if i.name==M.INDEX)
    assert str(CreateIndex(index).compile(dialect=dialect)) == 'CREATE UNIQUE INDEX uq_filament_catalogue_code ON filament_library (brand, catalogue_code) WHERE catalogue_code IS NOT NULL'


def test_prior_057_database_upgrades_with_only_058_and_keeps_library(tmp_path):
    import subprocess
    import sys
    from core.schema import preserved056
    code = '''
import runpy,sys
runpy.run_path(sys.argv[1],run_name='frozen056')['prepare_sources']()
from sqlalchemy import create_engine
from core.schema import bootstrap_database
engine=create_engine('sqlite:///'+sys.argv[2])
bootstrap_database(engine)
with engine.begin() as c:
 c.exec_driver_sql("INSERT INTO filament_library(id,brand,name,material) VALUES(98765,'Synthetic','Retained','OTHER')")
'''
    result = subprocess.run([sys.executable,'-I','-c',code,
        str(Path(preserved056.__file__).with_name('runner.py')),str(tmp_path/'old.db')],
        cwd=tmp_path,capture_output=True,text=True,timeout=90)
    assert result.returncode == 0,result.stderr
    engine = create_engine('sqlite:///'+str(tmp_path/'old.db'))
    with engine.begin() as c:
        apply_python_migration(c,'core.schema.migrations.057_slot_feed_policy')
        evidence_before = c.exec_driver_sql("SELECT sql FROM sqlite_master WHERE name='ams_report_trays'").scalar_one()
    from core.schema.bootstrap import POST027_MIGRATIONS
    assert bootstrap_database(engine)['applied'] == [mid for mid, _ in POST027_MIGRATIONS if int(mid.split(':')[1][:3]) >= 58]
    assert bootstrap_database(engine)['applied'] == []
    with engine.connect() as c:
        assert c.exec_driver_sql('SELECT name,material,catalogue_code FROM filament_library WHERE id=98765').one() == ('Retained','OTHER',None)
        assert c.exec_driver_sql("SELECT sql FROM sqlite_master WHERE name='ams_report_trays'").scalar_one() == evidence_before
