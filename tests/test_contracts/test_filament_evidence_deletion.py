"""Exercise the real deletion entry points against retained print evidence."""
import asyncio
import json

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from core import filament_ledger as ledger
from core.print_attempts import sqlalchemy_runner
from tests.test_contracts.test_archive_filament_settlement import (
    settlement_case, archive_case, review_db, post, preview, query, write, manual,
)

from tests.test_contracts.test_cert_D_jobs_lifecycle_idempotency import owned_completion_background  # noqa: F401


@pytest.fixture(autouse=True)
def owned_delivery(owned_completion_background):
    yield


def delete(case, path):
    from modules.jobs.routes import jobs_crud
    from modules.jobs.services import delete_org_jobs
    from modules.system.routes_config import run_retention_cleanup
    case[0].app.include_router(jobs_crud.router)
    case[3].update(role='admin', is_superadmin=True)
    write(case, "UPDATE jobs SET status='cancelled',updated_at='2000-01-01' WHERE id=7099")
    if path == 'single':
        response = case[0].delete('/jobs/7099')
        assert response.status_code == 204, response.text
    elif path == 'bulk':
        response = case[0].post('/jobs/bulk-update', json={'action': 'delete', 'job_ids': [7099]})
        assert response.status_code == 200, response.text
    elif path == 'org':
        with Session(case[1]) as db:
            db.execute(text("INSERT INTO groups(id,name,is_org) VALUES(91,'Deletion fixture',TRUE)"))
            db.execute(text('UPDATE jobs SET charged_to_org_id=91 WHERE id=7099'))
            delete_org_jobs(db, 91)
            db.execute(text('DELETE FROM groups WHERE id=91'))
            db.commit()
    else:
        write(case, "INSERT INTO system_config(key,value) VALUES('data_retention',:value)",
              {'value': json.dumps(dict(completed_jobs_days=1, audit_logs_days=0, timelapses_days=0, alert_history_days=0))})
        with Session(case[1]) as db:
            asyncio.run(run_retention_cleanup(case[3], db))
    assert not query(case, 'SELECT id FROM jobs WHERE id=7099')


@pytest.mark.parametrize('path', ['single', 'bulk', 'retention', 'org'])
def test_job_deletion_keeps_print_link_and_controlled_audience(settlement_case, path):
    from core.controlled_parts import allowed
    from core.controlled_redaction import redact_export
    case = settlement_case
    # A global controlled file can be used by an organization job; the group
    # deletion fence correctly refuses files owned by the group itself.
    write(case, 'UPDATE print_files SET org_id=NULL WHERE id=7099')
    write(case, "INSERT INTO controlled_audiences(id,name) VALUES(91,'Fixture audience')")
    write(case, 'INSERT INTO controlled_audience_members(audience_id,user_id) VALUES(91,3)')
    write(case, "INSERT INTO controlled_files(file_id,label,audience_id,printer_tag,retention_days,reason,marked_at) VALUES(7099,'CUI',91,'Secure',7,'Fixture','2026-01-01')")
    delete(case, path)
    assert query(case, 'SELECT scheduled_job_id FROM print_jobs WHERE id=7099') == [(7099,)]
    outsider = dict(id=4, role='operator', group_id=1, is_active=True)
    with Session(case[1]) as db:
        assert not allowed(db, 'print_job', 7099, outsider)
        assert not allowed(db, 'archive', case[2], outsider)
        exported = redact_export(db, outsider, {'id': 7099, 'job_name': 'Synthetic print'}, resource_kind='print_job')
        assert exported['job_name'] == '[controlled]'
    case[3].clear(); case[3].update(outsider)
    assert preview(case).status_code == 404


@pytest.mark.parametrize('path', ['bulk', 'retention'])
def test_manual_use_then_bulk_delete_or_retention_still_requires_review(settlement_case, path):
    case = settlement_case
    uid = manual(case, 40)
    delete(case, path)
    assert query(case, 'SELECT remaining_weight_g FROM spools WHERE id=7099') == [(960,)]
    response = post(case, entries=[dict(spool_id=7099, grams=40)])
    assert response.status_code == 409, response.text
    assert response.json()['detail']['code'] == 'manual_usage_unreviewed'
    assert post(case, entries=[dict(spool_id=7099, grams=40)], counted_usage_ids=[uid]).status_code == 200
    assert query(case, 'SELECT remaining_weight_g FROM spools WHERE id=7099') == [(960,)]


def test_pre_ledger_job_row_then_purge_is_unknown_ask(settlement_case):
    from core.filament_evidence_backfill import backfill
    case = settlement_case
    write(case, "INSERT INTO spool_usage(spool_id,job_id,weight_used_g,used_at,notes) VALUES(7099,7099,40,'2026-10-11 12:30:00','Legacy use')")
    write(case, 'UPDATE spools SET remaining_weight_g=960 WHERE id=7099')
    # The purge predates adoption: only the archive and dangling link survive.
    delete(case, 'retention')
    backfill(case[1])
    assert preview(case).json()['prior_usage']
    assert post(case, entries=[dict(spool_id=7099, grams=40)]).status_code == 409
    assert query(case, 'SELECT remaining_weight_g FROM spools WHERE id=7099') == [(960,)]


def test_org_deletion_keeps_evidence(settlement_case):
    case = settlement_case
    with case[1].begin() as db:
        ledger.record_print_usage(sqlalchemy_runner(db), 7099, source='moonraker', grams_by_spool={7099: 40})
    before = query(case, 'SELECT * FROM print_filament_evidence')
    delete(case, 'org')
    assert query(case, 'SELECT * FROM print_filament_evidence') == before
    assert post(case, entries=[dict(spool_id=7099, grams=40)]).status_code == 409


def test_archive_delete_then_startup_recovery_deducts_nothing(archive_case):
    archive, engine = archive_case
    assert archive.create_print_archive(7099, 9, True)
    with engine.begin() as db:
        assert db.execute(text('SELECT remaining_weight_g FROM spools WHERE id=7099')).scalar_one() == 960
        db.execute(text('DELETE FROM print_archives WHERE print_job_id=7099'))
    assert archive.recover_missing_archives()['recovered'] == 1
    with engine.connect() as db:
        assert db.execute(text('SELECT remaining_weight_g FROM spools WHERE id=7099')).scalar_one() == 960
        assert db.execute(text('SELECT COUNT(*) FROM print_filament_evidence WHERE claim=1')).scalar_one() == 1


def test_print_of_deleted_job_is_not_unlinked(settlement_case):
    from modules.jobs.routes import jobs_lifecycle, presets
    case = settlement_case
    delete(case, 'single')
    case[0].app.include_router(jobs_lifecycle.router)
    case[0].app.include_router(presets.router)
    assert not any(row['id'] == 7099 for row in case[0].get('/print-jobs/unlinked').json())
    write(case, "INSERT INTO jobs(id,item_name,printer_id,charged_to_org_id,status,quantity,hold,ams_mapping,print_file_id) VALUES(7102,'New job',9,1,'failed',1,FALSE,'[0,1]',7099)")
    assert case[0].post('/jobs/7102/link-print?print_job_id=7099').status_code == 409
    assert case[0].post('/jobs/7102/complete').status_code == 200
    assert query(case, 'SELECT remaining_weight_g FROM spools WHERE id=7099') == [(960,)]


from tests.test_contracts.test_outside_print_binding import outside, batch, shop, bind  # noqa: E402,F401


def test_print_of_deleted_job_refuses_outside_binding(outside):
    case, pid, _ = outside
    db = case[3]
    db.execute(text('UPDATE print_jobs SET scheduled_job_id=999999 WHERE id=:id'), {'id': pid})
    db.commit()
    response = bind(outside)
    assert response.status_code == 409, response.text
    assert 'Print resources changed' in response.text
