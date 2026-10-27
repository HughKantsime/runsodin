"""H2D bed corrections retain atomic history for conservative recipe backfill."""
import importlib
import json

import pytest
from sqlalchemy import create_engine, text

from core.recipe_configuration import unchanged_since
from core.schema import bootstrap_database
from tests.test_contracts.test_post027_migration_040 import pg_engine

M = importlib.import_module('core.schema.migrations.042_dispatch_capability')


def _seed(engine):
    bootstrap_database(engine)
    with engine.begin() as conn:
        for pid, machine, model, x, y, protocol in [
            (4201, 'H2D', None, 320, 320, 'bambu'),
            (4202, ' O1E ', None, 320, 320, 'bambu'),
            (4203, None, 'Bambu Lab H2D Pro', 320, 320, 'bambu'),
            (4204, 'H2D', None, 321, 320, 'bambu'),
            (4205, 'H2D', None, 320, 319, 'bambu'),
            (4206, 'P1S', None, 320, 320, 'bambu'),
            (4207, 'H2D', None, 320, 320, 'moonraker'),
        ]:
            conn.execute(text('''INSERT INTO printers(id,name,api_type,machine_type,model,bed_x_mm,bed_y_mm)
                VALUES(:id,:name,:api,:machine,:model,:x,:y)'''),
                dict(id=pid, name=f'Migration fixture {pid}', api=protocol, machine=machine, model=model, x=x, y=y))
        conn.execute(text('DELETE FROM odin_schema_migrations WHERE migration_id=:id'), {'id': M.MIGRATION_ID})


def _check_upgrade(engine):
    _seed(engine)
    assert bootstrap_database(engine)['applied'] == [M.MIGRATION_ID]
    assert bootstrap_database(engine)['applied'] == []
    with engine.begin() as conn:
        M.apply(conn)  # Direct reruns must not produce duplicate audit rows either.
        entries = conn.execute(text("SELECT entity_id,action,details FROM audit_logs WHERE entity_type='printer' ORDER BY entity_id")).all()
        assert [row[0] for row in entries] == [4201, 4202, 4203]
        for pid, action, details in entries:
            assert action == 'printer.observed_config_changed'
            detail = json.loads(details) if isinstance(details, str) else details
            assert detail == {'field': 'bed_x_mm', 'old': 320, 'new': 350, 'migration': M.MIGRATION_ID}
            assert not unchanged_since(conn, pid, '2020-01-01')
        for pid in range(4204, 4208):
            assert unchanged_since(conn, pid, '2020-01-01')
        assert conn.execute(text('SELECT bed_x_mm FROM printers WHERE id>=4201 ORDER BY id')).scalars().all() == [350,350,350,321,320,320,320]


def test_sqlite_corrections_are_audited_once(tmp_path):
    engine = create_engine('sqlite:///' + str(tmp_path / 'bed.db'))
    try:
        _check_upgrade(engine)
    finally:
        engine.dispose()


def test_postgres_corrections_are_audited_once(pg_engine):
    _check_upgrade(pg_engine)


def test_audit_failure_rolls_back_correction_and_ledger(tmp_path):
    engine = create_engine('sqlite:///' + str(tmp_path / 'audit-failure.db'))
    try:
        _seed(engine)
        with engine.begin() as conn:
            conn.exec_driver_sql("""CREATE TRIGGER reject_bed_audit BEFORE INSERT ON audit_logs
                WHEN NEW.entity_type='printer' BEGIN SELECT RAISE(ABORT,'synthetic audit failure'); END""")
        with pytest.raises(Exception, match='synthetic audit failure'):
            bootstrap_database(engine)
        with engine.connect() as conn:
            assert conn.execute(text('SELECT bed_x_mm FROM printers WHERE id IN (4201,4202,4203)')).scalars().all() == [320,320,320]
            assert conn.execute(text('SELECT COUNT(*) FROM audit_logs')).scalar_one() == 0
            assert conn.execute(text('SELECT COUNT(*) FROM odin_schema_migrations WHERE migration_id=:id'), {'id': M.MIGRATION_ID}).scalar_one() == 0
    finally:
        engine.dispose()
