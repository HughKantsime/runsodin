"""Owned PostgreSQL contention check, offline and skipped without local binaries."""
from pathlib import Path
import shutil
import subprocess

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session


@pytest.fixture
def failure_pg(tmp_path):
    initdb, pg_ctl = shutil.which('initdb'), shutil.which('pg_ctl')
    if not initdb or not pg_ctl:
        pytest.skip('Local PostgreSQL binaries unavailable; no download or server connection attempted')
    pytest.importorskip('psycopg')
    # Unix socket only: no TCP listener, no credentials or external server.
    import tempfile
    with tempfile.TemporaryDirectory(prefix='odin-pg-', dir='/private/tmp') as root:
        root = Path(root)
        data, sockets = root / 'data', root / 'sockets'
        sockets.mkdir()
        subprocess.run([initdb, '-D', str(data), '-A', 'trust', '-U', 'odin_test', '--no-locale'],
                       check=True, capture_output=True)
        subprocess.run([pg_ctl, '-D', str(data), '-l', str(root / 'server.log'),
                        '-o', f"-k {sockets} -h ''", '-w', 'start'], check=True, capture_output=True)
        engine = create_engine('postgresql+psycopg://odin_test@/postgres', connect_args={'host': str(sockets)})
        try:
            from core.schema import bootstrap_database
            bootstrap_database(engine, Path(__file__).resolve().parents[2] / 'backend')
            yield engine
        finally:
            engine.dispose()
            subprocess.run([pg_ctl, '-D', str(data), '-m', 'immediate', '-w', 'stop'], check=True, capture_output=True)


def test_correction_and_undo_while_telemetry_writes_are_open(failure_pg):
    from core.registry import registry
    from modules.organizations.services import EducationPolicyService
    from modules.jobs.routes.print_failure import MarkPrintFailed
    from modules.jobs.print_failure import change
    registry.register_provider('EducationPolicyProvider', EducationPolicyService())
    with failure_pg.begin() as db:
        db.execute(text("INSERT INTO printers(id,name) VALUES(9,'Owned synthetic printer')"))
        db.execute(text("INSERT INTO jobs(id,item_name,status) VALUES(901,'Owned synthetic job','cancelled')"))
        db.execute(text("INSERT INTO print_jobs(id,printer_id,scheduled_job_id,status,started_at,ended_at,filament_used_g) VALUES(902,9,901,'cancelled',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,50)"))
        db.execute(text("INSERT INTO print_jobs(id,printer_id,status,started_at) VALUES(904,9,'running',CURRENT_TIMESTAMP)"))
        db.execute(text("INSERT INTO print_archives(id,job_id,print_job_id,printer_id,status,filament_used_grams,ownership_state) VALUES(903,901,902,9,'cancelled',50,'known')"))
    with failure_pg.connect() as telemetry, Session(failure_pg) as correction:
        telemetry.execute(text('UPDATE printers SET last_seen=CURRENT_TIMESTAMP WHERE id=9'))
        telemetry.execute(text('UPDATE print_jobs SET progress_percent=25 WHERE id=904'))
        try:
            for body, expected in [(MarkPrintFailed(reason='no_filament'), 'failed'), (None, 'cancelled')]:
                correction.execute(text("SET LOCAL lock_timeout='500ms'"))
                result = change(correction, 902, {'role': 'admin', 'group_id': None, 'username': 'Synthetic operator'}, body)
                correction.commit()
                assert result['status'] == expected
                assert telemetry.in_transaction()
            assert correction.execute(text('SELECT status FROM print_archives WHERE id=903')).scalar_one() == 'cancelled'
        finally:
            telemetry.rollback()
