"""Owned PostgreSQL contention check for marking a print as failed.

Runs on the shared disposable Docker PostgreSQL server (pg_engine), like every other PostgreSQL
contract test: it is skipped only where Docker is unavailable. The release gate refuses skips, so
an environment-variable opt-in here would keep the release from ever passing (2.3.0 pre-check).
"""
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from tests.test_contracts.test_post027_migration_033_postgres import pg_engine  # noqa: F401

pytestmark = pytest.mark.postgresql


@pytest.fixture
def failure_pg(request):
    from core.schema import bootstrap_database
    engine = request.getfixturevalue('pg_engine')
    bootstrap_database(engine)
    return engine


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
