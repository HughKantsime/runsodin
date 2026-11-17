"""Shared definitions exercised against SQL, real transitions and correction routes."""
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from core.physical_print import CLOSED_UNKNOWN
from core.restart_questions import ENDED_UNKNOWN
from core.print_outcomes import (
    PRINT_FAILED_SQL, PRINT_STOPPED_SQL, PRINT_DID_NOT_COMPLETE_SQL,
    JOB_FAILED_SQL, JOB_STOPPED_SQL, JOB_DID_NOT_COMPLETE_SQL, print_outcome, job_outcome,
)
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_api_token_delegation_behavior import token_client
from tests.test_contracts.test_job_outcome_correction import correction_client
from tests.test_contracts.test_mark_print_failed import client, mark, undo
from tests.test_contracts.test_smart_plug_auto_power_behavior import auto_power
from tests.test_contracts.test_automatic_power_terminal_delivery import terminal_environment


@pytest.mark.parametrize('status', ['completed', 'failed', 'cancelled', 'running', 'pending'])
@pytest.mark.parametrize('end', [None, '2026-11-01 12:00:00'])
def test_python_and_sql_definitions_agree(review_db, status, end):
    for predicate, fragments, end_column, expected in (
        (print_outcome, (PRINT_FAILED_SQL, PRINT_STOPPED_SQL, PRINT_DID_NOT_COMPLETE_SQL), 'ended_at',
         'failed' if status == 'failed' and end else 'stopped' if status == 'cancelled' and end else None),
        (job_outcome, (JOB_FAILED_SQL, JOB_STOPPED_SQL, JOB_DID_NOT_COMPLETE_SQL), 'actual_end',
         'failed' if status == 'failed' else 'stopped' if status == 'cancelled' and end else None),
    ):
        assert predicate(status, end) == expected
        for fragment, counted in zip(fragments, (expected == 'failed', expected == 'stopped', expected is not None)):
            value = review_db.execute(text('SELECT CASE WHEN ' + fragment.format(prefix='r.') +
                f' THEN 1 ELSE 0 END FROM (SELECT :status AS status, :end AS {end_column}) r'),
                {'status': status, 'end': end}).scalar_one()
            assert value == int(counted)


@pytest.mark.parametrize('code', [CLOSED_UNKNOWN, ENDED_UNKNOWN])
def test_unknown_print_is_in_neither_count(review_db, code):
    review_db.execute(text("INSERT INTO print_jobs(printer_id,status,started_at,error_code) VALUES(9,'cancelled',CURRENT_TIMESTAMP,:code)"), {'code': code})
    for fragment in (PRINT_FAILED_SQL, PRINT_STOPPED_SQL, PRINT_DID_NOT_COMPLETE_SQL):
        assert review_db.execute(text('SELECT COUNT(*) FROM print_jobs WHERE ' + fragment.format(prefix=''))).scalar_one() == 0


def test_operator_cancel_before_start_is_not_counted(review_db):
    from modules.jobs.state_machine import transition_job
    identifier = review_db.execute(text("INSERT INTO jobs(item_name,status) VALUES('Not started','pending') RETURNING id")).scalar_one()
    assert transition_job(review_db, identifier, 'cancelled', 'operator')
    row = review_db.execute(text('SELECT status,actual_end FROM jobs WHERE id=:id'), {'id': identifier}).one()
    assert row.actual_end is None and job_outcome(*row) is None


@pytest.mark.parametrize('source', ['bambu', 'moonraker', 'generic'])
def test_monitor_stop_counts_print_and_job(terminal_environment, monkeypatch, source):
    engine, _, _, calls, mqtt, moon = terminal_environment
    if source == 'bambu':
        mqtt.record_job_ended(551, 'Owned terminal', 553, 552, 'cancelled', {'subtask_name': 'Owned terminal'}, {}, lambda **kwargs: None)
    elif source == 'moonraker':
        monitor = moon.MoonrakerMonitor(551, 'Owned terminal', '127.0.0.1')
        monitor._current_job_db_id = 553
        monitor._last_filename = 'owned.gcode'
        monitor._job_ended('cancelled', SimpleNamespace(filename='owned.gcode', filament_used_mm=0, error_message=None))
    else:
        from modules.notifications import job_events
        monkeypatch.setattr(job_events, 'get_db', mqtt.get_db)
        monkeypatch.setattr(job_events, 'dispatch_alert', lambda *a, **k: None)
        job_events.job_cancelled(551, 553)
    with engine.connect() as conn:
        assert print_outcome(*conn.execute(text('SELECT status,ended_at FROM print_jobs WHERE id=553')).one()) == 'stopped'
        assert job_outcome(*conn.execute(text('SELECT status,actual_end FROM jobs WHERE id=552')).one()) == 'stopped'
    assert calls == []


def test_mark_and_undo_preserve_combined_count(client):
    c, issue = client
    def counts():
        response = c.get('/print-jobs/stats', headers=issue(['read:jobs']))
        assert response.status_code == 200, response.text
        row = next(r for r in response.json() if r['printer_id'] == 9)
        return row['failed_jobs'], row['stopped_jobs'], row['did_not_complete_jobs']
    assert counts() == (0, 1, 1)
    assert mark(client).status_code == 200
    assert counts() == (1, 0, 1)
    assert undo(client).status_code == 200
    assert counts() == (0, 1, 1)


def test_failure_report_headline_keeps_existing_tables(review_db):
    from modules.reporting.report_generators import generate_failure_analysis
    review_db.execute(text("INSERT INTO jobs(item_name,status,created_at,actual_end) VALUES "
        "('Failed','failed',CURRENT_TIMESTAMP,NULL), ('Stopped','cancelled',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP),"
        "('Not started','cancelled',CURRENT_TIMESTAMP,NULL), ('Old','failed','2000-01-01',NULL)"))
    report = generate_failure_analysis(review_db, {})
    assert 'Did not complete: 2 (failed 1, stopped 1), last 30 days' in report
    assert 'AI Detections' in report and 'Job Failure Reasons' in report
