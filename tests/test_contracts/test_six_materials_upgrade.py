"""Released SQLite writes and catalogue backfill after historical/current restore."""
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest
from sqlalchemy import create_engine, inspect, text

from core.print_payload import material_match
from core.schema import bootstrap_database
from tests.test_contracts.test_six_materials import SIX
from tests.test_contracts.test_six_materials_catalogue import NAMES


def test_published_1924_accepts_all_six_in_every_enum_column(tmp_path):
    from tests.test_contracts.test_released_database_upgrade import _load, _bootstrap
    path = tmp_path / 'published.db'
    _load('odin-v1.9.24-sqlite.sql', path)
    result = _bootstrap(path)
    assert result.returncode == 0, result.stderr[-3000:]
    engine = create_engine('sqlite:///' + str(path))
    try:
        with engine.begin() as c:
            c.exec_driver_sql("INSERT INTO printers(id,name) VALUES(901,'Fixture')")
            columns = (('filament_slots', 'filament_type'), ('jobs', 'filament_type'),
                       ('models', 'default_filament_type'), ('print_presets', 'filament_type'))
            for table, column in columns:
                assert all(column not in check['sqltext'] for check in inspect(c).get_check_constraints(table))
            for n, material in enumerate(SIX, 1):
                values = dict(n=n, material=material.replace('-', '_'), name=f'Fixture-{n}')
                c.execute(text('INSERT INTO filament_slots(printer_id,slot_number,filament_type) VALUES(901,:n,:material)'), values)
                c.execute(text("INSERT INTO jobs(id,item_name,filament_type) VALUES(:n,'Fixture',:material)"), values)
                c.execute(text("INSERT INTO models(id,name,default_filament_type) VALUES(:n,:name,:material)"), values)
                c.execute(text("INSERT INTO print_presets(id,name,filament_type) VALUES(:n,:name,:material)"), values)
            for table, column in columns:
                assert c.exec_driver_sql(f'SELECT {column} FROM {table} ORDER BY id').scalars().all() == [m.replace('-', '_') for m in SIX]
        assert bootstrap_database(engine)['applied'] == []
    finally:
        engine.dispose()


@pytest.mark.parametrize('origin', ['preserved057', 'current'])
def test_restored_catalogue_spools_match_after_normal_restart(tmp_path, origin):
    from core.schema import preserved057
    from core.schema.migrator import apply_python_migration
    path = tmp_path / 'source.db'
    if origin == 'preserved057':
        code = '''
import runpy,sys
runpy.run_path(sys.argv[1],run_name='frozen057')['prepare_sources']()
from sqlalchemy import create_engine
from core.schema import bootstrap_database
engine=create_engine('sqlite:///'+sys.argv[2])
bootstrap_database(engine)
'''
        result = subprocess.run([sys.executable, '-I', '-c', code,
            str(Path(preserved057.__file__).with_name('runner.py')), str(path)],
            cwd=tmp_path, capture_output=True, text=True, timeout=90)
        assert result.returncode == 0, result.stderr
    engine = create_engine('sqlite:///' + str(path))
    try:
        if origin == 'current':
            bootstrap_database(engine)
        with engine.begin() as c:
            # Part A is the first contract with catalogue rows. A preserved057
            # restore gains these columns only when upgraded through 058.
            if origin == 'preserved057':
                apply_python_migration(c, 'core.schema.migrations.058_filament_catalogue')
            for n, material in enumerate(NAMES, 1):
                c.execute(text("INSERT INTO filament_library(id,brand,name,material,is_custom,catalogue_code,catalogue_type) "
                    "VALUES(:n,'Fixture','Fixture','OTHER',FALSE,:code,:material)"),
                    dict(n=n, code=f'FIXTURE-{n}', material=material))
                c.execute(text('INSERT INTO spools(id,filament_id) VALUES(:n,:n)'), dict(n=n))
        # Copy only the owned fixture, as an offline SQLite restore does.
        restored = tmp_path / 'restored.db'
        with sqlite3.connect(path) as source, sqlite3.connect(restored) as target:
            source.backup(target)
    finally:
        engine.dispose()
    engine = create_engine('sqlite:///' + str(restored))
    try:
        bootstrap_database(engine)
        assert bootstrap_database(engine)['applied'] == []
        with engine.connect() as c:
            rows = c.exec_driver_sql('SELECT l.material FROM spools s JOIN filament_library l ON l.id=s.filament_id ORDER BY s.id').scalars().all()
            assert rows == list(SIX)
            assert all(material_match(stored, reported) is True for stored, reported in zip(rows, SIX))
            assert c.exec_driver_sql("SELECT count(*) FROM audit_logs WHERE action='filament.catalogue_material_recognized'").scalar_one() == 6
    finally:
        engine.dispose()
