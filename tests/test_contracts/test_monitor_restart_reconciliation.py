"""Prints that end while no monitor is watching, through the real monitor paths."""
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from core.db_compat import as_utc_datetime
from tests.test_contracts.test_economic_creator_wiring import creators
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_dispatch_restart_resume import drift, running_bambu

CANCELLED_CODE = 0x0300400C
OUTCOMES = [('FINISH', 0, 'completed'), ('FAILED', 7, 'failed'), ('FAILED', CANCELLED_CODE, 'cancelled')]


def packet(monitor, **fields):
    """Deliver one MQTT report through the real status callback, which holds the lock."""
    monitor._on_status(SimpleNamespace(raw_data={'print': fields}))


def retained_packet(monitor, **fields):
    monitor._on_status(SimpleNamespace(raw_data={'print': fields}, mqtt_retained=True))


def ams_report(remain):
    """A full AMS list as a report carries it, with RFID spools so remain percentages count."""
    return {'ams': [{'id': '0', 'tray': [{'id': str(index), 'remain': remain, 'tag_uid': f'AB12CD34EF56AB{index:02d}',
                                          'tray_uuid': f'{index:032X}'} for index in range(4)]}], 'tray_exist_bits': 'f'}


def backoff_elapsed(monitor):
    monitor._recovery_retry_at = 0.0


def restarted(case):
    """A new monitor after a restart: nothing tracked, no state, no packet yet."""
    monitor = case.monitor()
    monitor._state = {}
    now = time.time()
    monitor._last_heartbeat = monitor._last_progress_update = monitor._last_spool_check = now
    monitor._camera_discovered = True
    return monitor


def observe(monitor, order, **fields):
    """Reach the monitor by each order a restart can produce; every order runs both paths."""
    if order == 'recovery_first':
        # The report is already merged when recovery wakes and is then redelivered.
        monitor._state.update(fields)
        monitor._recover_orphaned_jobs()
        packet(monitor, **fields)
    elif order == 'callback_first':
        packet(monitor, **fields)
        monitor._recover_orphaned_jobs()
    else:  # status_after_wait
        monitor._recover_orphaned_jobs()
        packet(monitor, **fields)


@pytest.fixture
def delivered(running_bambu, monkeypatch):
    """Terminal alerts (this job has no economic identity: the legacy route) and timeline items."""
    from core import job_timeline
    from modules.notifications import alert_dispatch
    seen = SimpleNamespace(alerts=[], timeline=[])
    monkeypatch.setattr(alert_dispatch, 'dispatch_alert', lambda **kw: seen.alerts.append(kw['alert_type']))
    monkeypatch.setattr(job_timeline, 'enqueue', lambda item: seen.timeline.append(item['kind']))
    return seen


def row_state(case):
    with case.engine.connect() as conn:
        rows = conn.execute(text('SELECT * FROM print_jobs')).mappings().all()
        claims = conn.execute(text('SELECT count(*) FROM printer_dispatch_claims')).scalar_one()
        job = conn.execute(text('SELECT status,actual_end FROM jobs WHERE id=53')).one()
    return rows, claims, job


def assert_fenced(case, monitor):
    rows, claims, job = row_state(case)
    assert monitor._current_job_id is None
    assert len(rows) == 1 and dict(rows[0]) == case.before
    assert claims == 1 and job[0] == 'printing'
    assert case.effects['archive'] == []


def assert_terminal_effects(case, delivered, expected):
    from core.base import AlertType
    assert case.effects['archive'] == [dict(print_job_id=case.identifier, printer_id=59, success=expected == 'completed',
                                            result_status='cancelled' if expected == 'cancelled' else None)]
    assert len(case.effects['completed']) == int(expected == 'completed')
    assert len(case.effects['care']) == int(expected == 'completed')
    assert delivered.alerts == [{'completed': AlertType.PRINT_COMPLETE, 'failed': AlertType.PRINT_FAILED,
                                 'cancelled': AlertType.PRINT_CANCELLED}[expected]]
    assert [kind for kind in delivered.timeline if kind in ('completed', 'failed', 'cancelled')] == [expected]


# ---------------------------------------------------------------- gap 1: dispatched print
@pytest.mark.parametrize('order', ['callback_first', 'recovery_first', 'status_after_wait'])
@pytest.mark.parametrize('state,error,expected', OUTCOMES)
def test_dispatched_print_ended_in_downtime_is_closed_once(running_bambu, delivered, order, state, error, expected):
    from modules.printers import dispatch
    case = running_bambu
    monitor = restarted(case)
    fields = dict(gcode_state=state, subtask_name=case.state['subtask_name'], print_error=error)
    observe(monitor, order, **fields)
    packet(monitor, **fields)  # A repeated report and a second recovery change nothing.
    monitor._recover_orphaned_jobs()
    rows, claims, job = row_state(case)
    assert len(rows) == 1 and rows[0]['id'] == case.identifier
    assert rows[0]['status'] == expected and rows[0]['ended_at'] is not None
    assert job[0] == expected and as_utc_datetime(job[1]) == as_utc_datetime(rows[0]['ended_at'])
    assert claims == 0
    assert monitor._current_job_id is None and monitor._linked_job_id is None
    assert_terminal_effects(case, delivered, expected)
    # The whole point: the printer accepts its next job again.
    assert dispatch.dispatch_job(59, 54)[0]


@pytest.mark.parametrize('order', ['callback_first', 'status_after_wait'])
def test_recovered_end_uses_the_first_start_filament_evidence(running_bambu, order):
    # Through the status callback: the report that shows the end is the only evidence of it.
    case = running_bambu
    monitor = restarted(case)
    fields = dict(gcode_state='FINISH', subtask_name=case.state['subtask_name'], print_error=0, ams=ams_report(70))
    observe(monitor, order, **fields)
    rows, _, _ = row_state(case)
    assert rows[0]['status'] == 'completed'
    assert rows[0]['filament_used_g'] == (100 if case.weights else None)


@pytest.mark.parametrize('end_report,expected', [(ams_report(70), 100), (None, 50)], ids=['in_the_end_packet', 'from_the_last_packet'])
def test_watched_end_still_measures_filament(running_bambu, end_report, expected):
    case = running_bambu
    monitor = restarted(case)
    packet(monitor, gcode_state='RUNNING', subtask_name=case.state['subtask_name'], print_error=0, ams=ams_report(75))
    assert monitor._current_job_id == case.identifier
    end = dict(gcode_state='FINISH', subtask_name=case.state['subtask_name'], print_error=0)
    packet(monitor, **(dict(end, ams=end_report) if end_report else end))
    rows, _, _ = row_state(case)
    assert rows[0]['status'] == 'completed'
    assert rows[0]['filament_used_g'] == (expected if case.weights else None)


def test_loadout_changed_in_downtime_still_proves_the_end(running_bambu):
    # An operator can swap a spool once the print is over and ODIN is still down.
    case = running_bambu
    drift(case.engine)
    monitor = restarted(case)
    packet(monitor, gcode_state='FINISH', subtask_name=case.state['subtask_name'], print_error=0)
    rows, claims, job = row_state(case)
    assert rows[0]['status'] == 'completed' and job[0] == 'completed' and claims == 0


def test_bambu_suffix_alias_proves_the_end(running_bambu):
    case = running_bambu
    monitor = restarted(case)
    packet(monitor, gcode_state='FINISH', subtask_name=case.state['subtask_name'].removesuffix('.3mf'), print_error=0)
    rows, claims, _ = row_state(case)
    assert rows[0]['status'] == 'completed' and claims == 0


@pytest.mark.parametrize('order', ['callback_first', 'recovery_first'])
@pytest.mark.parametrize('fields', [
    dict(gcode_state='IDLE'),  # Cannot be closed from idle.
    dict(gcode_state='FINISH', subtask_name='some-other-print.3mf'),  # Or an unrelated task.
    dict(gcode_state='FAILED', subtask_name='odin-dispatch-' + 'f' * 32 + '.3mf'),  # Or another reserved transport.
    dict(gcode_state='FINISH'),  # A report without a name proves nothing.
], ids=['idle', 'unrelated', 'other_reserved', 'unnamed'])
def test_unproven_end_keeps_the_dispatch_fence(running_bambu, order, fields):
    case = running_bambu
    monitor = restarted(case)
    if fields['gcode_state'] == 'IDLE':
        fields = dict(fields, subtask_name=case.state['subtask_name'])
    observe(monitor, order, **fields)
    assert_fenced(case, monitor)


@pytest.mark.parametrize('order', ['callback_first', 'recovery_first'])
def test_education_sensitive_end_stays_untouched(running_bambu, order):
    case = running_bambu
    monitor = restarted(case)
    observe(monitor, order, gcode_state='FINISH', subtask_name='odin-' + 'a' * 32 + '.3mf')
    assert_fenced(case, monitor)


@pytest.mark.parametrize('order', ['callback_first', 'recovery_first'])
@pytest.mark.parametrize('mutation', [
    "UPDATE printers SET api_host='changed.example.test' WHERE id=59",
    "UPDATE printer_dispatch_claims SET state='preparing' WHERE printer_id=59",
    "UPDATE dispatch_attempt_receipts SET stable_key_source='untrusted'",
])
def test_refused_identity_keeps_the_fence(running_bambu, order, mutation, caplog):
    case = running_bambu
    with case.engine.begin() as conn:
        conn.execute(text(mutation))
    monitor = restarted(case)
    observe(monitor, order, gcode_state='FINISH', subtask_name=case.state['subtask_name'])
    assert monitor._current_job_id is None
    rows, claims, _ = row_state(case)
    assert len(rows) == 1 and rows[0]['status'] == 'running' and claims == 1
    assert case.effects['archive'] == []
    assert 'reason=receipt_bound_resume_refused' in caplog.text


def test_failed_end_does_not_fail_connect_and_retries(running_bambu):
    # connect() reports any recovery exception as a failed connection.
    case = running_bambu
    monitor = restarted(case)
    attempted = []
    def fail(status):
        attempted.append(status)
        raise RuntimeError('synthetic')
    ended = monitor._job_ended
    monitor._job_ended = fail
    fields = dict(gcode_state='FINISH', subtask_name=case.state['subtask_name'], print_error=0)
    monitor._state.update(fields)
    monitor._recover_orphaned_jobs()
    assert attempted == ['completed']
    assert monitor._current_job_id is None and monitor._linked_job_id is None
    assert row_state(case)[0][0]['status'] == 'running' and monitor._recovery_pending
    monitor._job_ended = ended
    backoff_elapsed(monitor)
    packet(monitor, **fields)
    rows, claims, job = row_state(case)
    assert rows[0]['status'] == 'completed' and claims == 0 and job[0] == 'completed'
    assert not monitor._recovery_pending


@pytest.mark.parametrize('first', ['callback', 'recovery'])
@pytest.mark.parametrize('state,error,expected', OUTCOMES)
def test_end_lost_to_a_transient_database_error_retries_on_this_connection(running_bambu, delivered, monkeypatch, first, state, error, expected):
    # record_job_ended swallows this, as it does contention on a real database.
    from modules.printers import dispatch
    from modules.printers.monitors import mqtt_job_lifecycle as lifecycle
    case = running_bambu
    monitor = restarted(case)
    database = lifecycle.get_db
    monkeypatch.setattr(lifecycle, 'get_db', lambda: (_ for _ in ()).throw(RuntimeError('synthetic contention')))
    fields = dict(gcode_state=state, subtask_name=case.state['subtask_name'], print_error=error)
    if first == 'callback':
        packet(monitor, **fields)
    else:
        monitor._state.update(fields)
        monitor._recover_orphaned_jobs()
    rows, claims, job = row_state(case)
    assert rows[0]['status'] == 'running' and claims == 1 and job[0] == 'printing'
    assert monitor._current_job_id is None and monitor._recovery_pending
    monkeypatch.setattr(lifecycle, 'get_db', database)
    backoff_elapsed(monitor)
    packet(monitor, **fields)
    packet(monitor, **fields)
    rows, claims, job = row_state(case)
    assert rows[0]['status'] == expected and job[0] == expected and claims == 0
    assert not monitor._recovery_pending
    assert monitor._recovery_failures == 0  # The next failure backs off from the start again.
    assert_terminal_effects(case, delivered, expected)
    assert dispatch.dispatch_job(59, 54)[0]


def test_end_during_a_database_outage_stays_pending(running_bambu, monkeypatch):
    # The re-read fails too: an unreadable row is not a closed row, or nothing would retry it.
    from modules.printers.monitors import mqtt_job_lifecycle as lifecycle, mqtt_printer
    case = running_bambu
    monitor = restarted(case)
    lifecycle_db, printer_db = lifecycle.get_db, mqtt_printer.get_db
    def outage():
        raise RuntimeError('synthetic outage')
    def outage_begins():
        monkeypatch.setattr(mqtt_printer, 'get_db', outage)
        raise RuntimeError('synthetic outage')
    monkeypatch.setattr(lifecycle, 'get_db', outage_begins)
    fields = dict(gcode_state='FINISH', subtask_name=case.state['subtask_name'], print_error=0)
    packet(monitor, **fields)
    assert row_state(case)[0][0]['status'] == 'running'
    assert monitor._recovery_pending and monitor._recovery_failures == 1
    monkeypatch.setattr(lifecycle, 'get_db', lifecycle_db)
    monkeypatch.setattr(mqtt_printer, 'get_db', printer_db)
    backoff_elapsed(monitor)
    packet(monitor, **fields)
    rows, claims, job = row_state(case)
    assert rows[0]['status'] == 'completed' and job[0] == 'completed' and claims == 0
    assert monitor._recovery_failures == 0


@pytest.fixture
def manual_bambu(review_db, tmp_path, monkeypatch):
    """An unreserved print ODIN saw start and fuzzy-linked to job 53; no dispatch claim exists."""
    from contextlib import contextmanager
    from core import event_bus, events, job_timeline
    from core.db_utils import _ConnectionAdapter
    from modules.archives import archive
    from modules.notifications import alert_dispatch, event_dispatcher
    from modules.printers.monitors import mqtt_job_lifecycle, mqtt_printer
    from tests.test_contracts.test_dispatch_real_monitor_correlation import configure_dispatch
    engine, _ = configure_dispatch(review_db, tmp_path, monkeypatch, protocol='bambu')

    @contextmanager
    def database():
        raw = engine.raw_connection()
        try:
            yield _ConnectionAdapter(raw, engine.dialect.name == 'postgresql')
        finally:
            raw.close()

    monkeypatch.setattr(mqtt_job_lifecycle, 'get_db', database)
    monkeypatch.setattr(mqtt_printer, 'get_db', database)
    seen = SimpleNamespace(alerts=[], timeline=[], archive=[], completed=[], care=[])
    event_bus.get_event_bus().subscribe(events.JOB_COMPLETED, lambda event: seen.completed.append(event))
    monkeypatch.setattr(alert_dispatch, 'dispatch_alert', lambda **kw: seen.alerts.append(kw['alert_type']))
    monkeypatch.setattr(job_timeline, 'enqueue', lambda item: seen.timeline.append(item['kind']))
    monkeypatch.setattr(alert_dispatch, '_start_bed_cooled_monitor', lambda *a: None)
    monkeypatch.setattr(event_dispatcher, 'increment_care_counters', lambda *a: seen.care.append(a))
    monkeypatch.setattr(archive, 'create_print_archive', lambda **kw: seen.archive.append(kw))

    def monitor():
        result = mqtt_printer.PrinterMonitor(59, 'Synthetic', '127.0.0.1', 'SYNTHETICPRINTER', 'synthetic')
        result._state = {}
        result._status_received.set()
        result._dispatch_alert = lambda **kw: None
        result._trigger_reschedule = lambda: None
        result._last_heartbeat = result._last_progress_update = result._last_spool_check = time.time()
        result._camera_discovered = True
        return result

    with engine.begin() as conn:
        conn.execute(text("INSERT INTO filament_library(id,brand,name) VALUES(700,'Synthetic','PLA')"))
        conn.execute(text('INSERT INTO spools(id,filament_id,initial_weight_g,remaining_weight_g,org_id) VALUES(700,700,1000,800,1)'))
        conn.execute(text('UPDATE filament_slots SET assigned_spool_id=700 WHERE printer_id=59'))
    first = monitor()
    first._state = {'gcode_state': 'RUNNING', 'subtask_name': 'Owned dispatch test', 'gcode_file': 'manual.gcode', '_ams_remain': {1: 80}}
    assert first._job_started()
    assert first._linked_job_id == 53
    assert first._start_spool_weights
    return SimpleNamespace(engine=engine, monitor=monitor, identifier=first._current_job_id, seen=seen, name='Owned dispatch test')


def manual_rows(case):
    with case.engine.connect() as conn:
        rows = conn.execute(text('SELECT id,status,scheduled_job_id FROM print_jobs ORDER BY id')).mappings().all()
        job = conn.execute(text('SELECT status FROM jobs WHERE id=53')).scalar_one()
    return [dict(row) for row in rows], job


# ---------------------------------------------------------------- gap 6: no status yet
def test_no_status_after_the_wait_leaves_a_live_print_alone(manual_bambu):
    case = manual_bambu
    monitor = case.monitor()
    monitor._recover_orphaned_jobs()
    assert manual_rows(case)[0][0]['status'] == 'running'
    assert monitor._current_job_id is None
    # A report without a state is still no evidence.
    packet(monitor, nozzle_temper=200)
    assert manual_rows(case)[0][0]['status'] == 'running'
    assert monitor._recovery_pending
    # The print was live all along: the same row is tracked, nothing is added.
    packet(monitor, gcode_state='RUNNING', subtask_name=case.name)
    rows, job = manual_rows(case)
    assert [row['status'] for row in rows] == ['running'] and job == 'printing'
    assert monitor._current_job_id == case.identifier
    assert not monitor._recovery_pending


def test_deferred_recovery_runs_once_and_keeps_the_idle_outcome(manual_bambu, monkeypatch):
    case = manual_bambu
    monitor = case.monitor()
    monitor._recover_orphaned_jobs()
    runs = []
    recover = monitor._recover_rows
    monkeypatch.setattr(monitor, '_recover_rows', lambda: runs.append(True) or recover())
    packet(monitor, gcode_state='IDLE')
    assert manual_rows(case)[0][0]['status'] == 'completed'  # Today's outcome; Shane's open decision.
    packet(monitor, gcode_state='IDLE')
    packet(monitor, gcode_state='RUNNING', subtask_name='next.3mf')
    packet(monitor, gcode_state='RUNNING', subtask_name='next.3mf')
    assert runs == [True]
    assert manual_rows(case)[0][-1]['status'] == 'running'


def test_recovery_waits_for_status_before_taking_the_lock(manual_bambu, monkeypatch):
    monitor = manual_bambu.monitor()
    held = []
    monkeypatch.setattr(monitor._status_received, 'wait', lambda timeout: held.append(monitor._lock.locked()))
    monitor._recover_orphaned_jobs()
    assert held == [False]


def test_failed_recovery_retries_on_the_next_report(manual_bambu, monkeypatch):
    from modules.printers.monitors import mqtt_printer
    case = manual_bambu
    monitor = case.monitor()
    monitor._state['gcode_state'] = 'IDLE'
    database = mqtt_printer.get_db
    monkeypatch.setattr(mqtt_printer, 'get_db', lambda: (_ for _ in ()).throw(RuntimeError('synthetic')))
    monitor._recover_orphaned_jobs()
    assert monitor._recovery_pending
    assert manual_rows(case)[0][0]['status'] == 'running'
    monkeypatch.setattr(mqtt_printer, 'get_db', database)
    backoff_elapsed(monitor)
    packet(monitor, gcode_state='IDLE')
    assert not monitor._recovery_pending
    assert manual_rows(case)[0][0]['status'] == 'completed'


def test_failing_recovery_backs_off_instead_of_retrying_every_report(manual_bambu, monkeypatch, caplog):
    import logging
    from modules.printers.monitors import mqtt_printer
    case = manual_bambu
    monitor = case.monitor()
    monitor._state['gcode_state'] = 'IDLE'
    database = mqtt_printer.get_db
    opened = []
    def refuse():
        opened.append(True)
        raise RuntimeError('synthetic')
    monkeypatch.setattr(mqtt_printer, 'get_db', refuse)
    with caplog.at_level(logging.ERROR, logger='mqtt_monitor'):
        monitor._recover_orphaned_jobs()
        for _ in range(10):
            packet(monitor, gcode_state='IDLE')
        # One attempt for eleven chances: no connection, no error line, no _lock stall.
        assert len(opened) == 1
        assert sum('Orphaned job recovery failed' in record.getMessage() for record in caplog.records) == 1
        assert 0 < monitor._recovery_retry_at - time.time() <= mqtt_printer.RECOVERY_RETRY_BASE_SECONDS
        delays = []
        for _ in range(8):
            backoff_elapsed(monitor)
            packet(monitor, gcode_state='IDLE')
            delays.append(round(monitor._recovery_retry_at - time.time()))
    assert delays == [20, 40, 80, 160, 300, 300, 300, 300]
    assert len(opened) == 9
    monkeypatch.setattr(mqtt_printer, 'get_db', database)
    backoff_elapsed(monitor)
    packet(monitor, gcode_state='IDLE')
    assert manual_rows(case)[0][0]['status'] == 'completed'
    assert not monitor._recovery_pending and monitor._recovery_failures == 0


def test_failed_commit_does_not_keep_the_ended_row_tracked(manual_bambu, monkeypatch):
    # Tracking is set inside the transaction. Left behind, it would make the next print look
    # already tracked, and block the retry, which runs only when nothing is tracked.
    from core.db_utils import _ConnectionAdapter
    case = manual_bambu
    monitor = case.monitor()
    fields = dict(gcode_state='FAILED', subtask_name=case.name, print_error=7)
    monitor._state.update(fields)
    commit = _ConnectionAdapter.commit
    def refuse(self):
        raise RuntimeError('synthetic')
    monkeypatch.setattr(_ConnectionAdapter, 'commit', refuse)
    monitor._recover_orphaned_jobs()
    monkeypatch.setattr(_ConnectionAdapter, 'commit', commit)
    assert monitor._current_job_id is None and monitor._linked_job_id is None
    assert monitor._recovery_pending
    assert manual_rows(case)[0][0]['status'] == 'running'
    backoff_elapsed(monitor)
    packet(monitor, **fields)
    rows, job = manual_rows(case)
    assert [row['status'] for row in rows] == ['failed'] and job == 'failed'


def test_first_state_after_no_status_does_not_wait_for_any_backoff(manual_bambu):
    case = manual_bambu
    monitor = case.monitor()
    monitor._recover_orphaned_jobs()
    assert monitor._recovery_pending and monitor._recovery_failures == 0
    packet(monitor, gcode_state='IDLE')
    assert manual_rows(case)[0][0]['status'] == 'completed'


def test_retrying_recovery_leaves_a_tracked_print_to_its_live_end(manual_bambu):
    case = manual_bambu
    monitor = case.monitor()
    monitor._current_job_id = case.identifier
    monitor._linked_job_id = 53
    monitor._last_gcode_state = 'RUNNING'
    monitor._recovery_pending = True
    # An unproven name would raw-close this row as completed if recovery ran first.
    packet(monitor, gcode_state='FAILED', subtask_name='another-print', print_error=7)
    rows, job = manual_rows(case)
    assert [row['status'] for row in rows] == ['failed'] and job == 'failed'


# ---------------------------------------------------------------- retained first report
@pytest.mark.parametrize('state,expected', [('FINISH', 'completed'), ('FAILED', 'failed')])
def test_retained_first_end_is_not_proof_until_a_live_report(manual_bambu, state, expected):
    case = manual_bambu
    monitor = case.monitor()
    fields = dict(gcode_state=state, subtask_name=case.name, print_error=7 if state == 'FAILED' else 0)
    retained_packet(monitor, **fields)
    monitor._recover_orphaned_jobs()
    rows, job = manual_rows(case)
    assert [row['status'] for row in rows] == ['running'] and job == 'printing'
    assert monitor._current_job_id is None and case.seen.alerts == []
    assert monitor._state['gcode_state'] == state  # Still usable, only not proof.
    packet(monitor, **fields)
    rows, job = manual_rows(case)
    assert [row['status'] for row in rows] == [expected] and job == expected
    assert len(case.seen.alerts) == 1


def test_retained_idle_does_not_raw_close_a_row(manual_bambu):
    case = manual_bambu
    monitor = case.monitor()
    retained_packet(monitor, gcode_state='IDLE')
    monitor._recover_orphaned_jobs()
    assert [row['status'] for row in manual_rows(case)[0]] == ['running']


def test_retained_end_of_an_earlier_print_leaves_the_live_print_tracked(manual_bambu):
    case = manual_bambu
    monitor = case.monitor()
    retained_packet(monitor, gcode_state='FINISH', subtask_name=case.name, print_error=0)
    packet(monitor, gcode_state='RUNNING', subtask_name=case.name, print_error=0)
    rows, job = manual_rows(case)
    assert [row['status'] for row in rows] == ['running'] and job == 'printing'
    assert monitor._current_job_id == case.identifier
    assert case.seen.archive == []


@pytest.mark.parametrize('order', ['callback_first', 'recovery_first'])
def test_retained_end_of_a_dispatched_print_keeps_the_fence_until_a_live_report(running_bambu, order):
    case = running_bambu
    monitor = restarted(case)
    fields = dict(gcode_state='FINISH', subtask_name=case.state['subtask_name'], print_error=0)
    if order == 'callback_first':
        retained_packet(monitor, **fields)
        monitor._recover_orphaned_jobs()
    else:
        monitor._recover_orphaned_jobs()
        retained_packet(monitor, **fields)
    assert_fenced(case, monitor)
    packet(monitor, **fields)
    rows, claims, job = row_state(case)
    assert rows[0]['status'] == 'completed' and job[0] == 'completed' and claims == 0


def retained_log_lines(caplog):
    return [record.getMessage() for record in caplog.records if 'Retained MQTT report' in record.getMessage()]


def test_retained_report_is_logged_once_per_interval_without_its_payload(manual_bambu, caplog):
    import logging
    from modules.printers.monitors import mqtt_status
    case = manual_bambu
    monitor = case.monitor()
    with caplog.at_level(logging.INFO, logger='mqtt_monitor'):
        for _ in range(5):
            retained_packet(monitor, gcode_state='FINISH', subtask_name='private-file-name', print_error=0)
        lines = retained_log_lines(caplog)
        assert lines == ["Retained MQTT report: printer_id=59 gcode_state='FINISH'"]
        monitor._last_retained_report_log = time.time() - mqtt_status.RETAINED_REPORT_LOG_SECONDS
        retained_packet(monitor, nozzle_temper=200)  # A report with no state is still a replay.
        assert retained_log_lines(caplog)[1:] == ['Retained MQTT report: printer_id=59 gcode_state=None']
    assert 'private-file-name' not in caplog.text and monitor.access_code not in caplog.text


@pytest.mark.parametrize('flag', [False, None], ids=['live', 'unknown'])
def test_live_report_is_not_logged_as_retained(manual_bambu, caplog, flag):
    import logging
    case = manual_bambu
    monitor = case.monitor()
    with caplog.at_level(logging.INFO, logger='mqtt_monitor'):
        monitor._on_status(SimpleNamespace(raw_data={'print': dict(gcode_state='RUNNING', subtask_name=case.name)}, mqtt_retained=flag))
    assert retained_log_lines(caplog) == []


def test_unknown_retain_flag_is_not_treated_as_retained(manual_bambu):
    # The legacy adapter reports None when the broker message carries no boolean flag.
    case = manual_bambu
    monitor = case.monitor()
    monitor._on_status(SimpleNamespace(raw_data={'print': dict(gcode_state='FINISH', subtask_name=case.name, print_error=0)}, mqtt_retained=None))
    assert [row['status'] for row in manual_rows(case)[0]] == ['completed']


# ---------------------------------------------------------------- gap 7: unreserved rows
@pytest.mark.parametrize('order', ['callback_first', 'recovery_first', 'status_after_wait'])
@pytest.mark.parametrize('state,error,expected', OUTCOMES)
def test_unreserved_print_ended_in_downtime_maps_like_a_watched_end(manual_bambu, order, state, error, expected):
    from core.base import AlertType
    case = manual_bambu
    monitor = case.monitor()
    fields = dict(gcode_state=state, subtask_name=case.name, print_error=error)
    observe(monitor, order, **fields)
    packet(monitor, **fields)
    monitor._recover_orphaned_jobs()
    rows, job = manual_rows(case)
    assert rows == [dict(id=case.identifier, status=expected, scheduled_job_id=53)]
    assert job == expected  # The linked job moves, which the raw close never did.
    assert monitor._current_job_id is None and monitor._linked_job_id is None
    seen = case.seen
    assert seen.alerts == [{'completed': AlertType.PRINT_COMPLETE, 'failed': AlertType.PRINT_FAILED,
                            'cancelled': AlertType.PRINT_CANCELLED}[expected]]
    assert [kind for kind in seen.timeline if kind in ('completed', 'failed', 'cancelled')] == [expected]
    assert [item['success'] for item in seen.archive] == [expected == 'completed']
    assert len(seen.completed) == len(seen.care) == int(expected == 'completed')


@pytest.mark.parametrize('report', ['garbled', 7, [1, 2], {'ams': 'garbled'}, {'ams': 7}, {'ams': [1, 2]}, {'ams': [{'id': '0', 'tray': 'garbled'}]}],
                         ids=['string', 'number', 'list', 'dict_of_string', 'dict_of_number', 'dict_of_list', 'garbled_tray'])
def test_malformed_ams_report_does_not_cost_a_print_its_end(manual_bambu, report):
    case = manual_bambu
    monitor = case.monitor()
    packet(monitor, gcode_state='FAILED', subtask_name=case.name, print_error=7, ams=report)
    rows, job = manual_rows(case)
    assert [row['status'] for row in rows] == ['failed'] and job == 'failed'
    packet(monitor, gcode_state='FAILED', subtask_name=case.name, print_error=7, ams=report)  # And the tail of a later packet.


def test_watched_cancel_to_idle_measures_filament_in_its_own_packet(running_bambu):
    # RUNNING -> IDLE is a cancel, and its report is the only one with what it really used.
    case = running_bambu
    monitor = restarted(case)
    name = case.state['subtask_name']
    packet(monitor, gcode_state='RUNNING', subtask_name=name, print_error=0, ams=ams_report(75))
    packet(monitor, gcode_state='IDLE', subtask_name=name, print_error=0, ams=ams_report(70))
    rows, _, _ = row_state(case)
    assert rows[0]['status'] == 'cancelled'
    assert rows[0]['filament_used_g'] == (100 if case.weights else None)


@pytest.mark.parametrize('report', ['garbled', {'ams': 'garbled'}], ids=['not_a_report', 'not_a_list'])
def test_malformed_ams_report_keeps_the_last_good_measurement(running_bambu, report):
    case = running_bambu
    monitor = restarted(case)
    name = case.state['subtask_name']
    packet(monitor, gcode_state='RUNNING', subtask_name=name, print_error=0, ams=ams_report(75))
    packet(monitor, gcode_state='FINISH', subtask_name=name, print_error=0, ams=report)
    rows, _, _ = row_state(case)
    assert rows[0]['status'] == 'completed'
    assert rows[0]['filament_used_g'] == (50 if case.weights else None)


def test_unreserved_end_measures_filament_against_its_first_start(manual_bambu):
    case = manual_bambu
    packet(case.monitor(), gcode_state='FINISH', subtask_name=case.name, print_error=0, ams=ams_report(70))
    with case.engine.connect() as conn:
        assert conn.execute(text('SELECT filament_used_g FROM print_jobs WHERE id=:id'), {'id': case.identifier}).scalar_one() == 100


def test_first_packet_of_a_print_under_way_is_not_its_start_evidence(manual_bambu):
    # This print started while ODIN was down, so this remain says nothing about its start.
    case = manual_bambu
    with case.engine.begin() as conn:
        conn.execute(text("UPDATE print_jobs SET status='cancelled' WHERE id=:id"), {'id': case.identifier})
    monitor = case.monitor()
    packet(monitor, gcode_state='RUNNING', subtask_name='started-in-downtime', print_error=0, ams=ams_report(40))
    identifier = monitor._current_job_id
    assert identifier and identifier != case.identifier
    assert monitor._start_spool_weights == {}
    packet(monitor, gcode_state='FINISH', subtask_name='started-in-downtime', print_error=0, ams=ams_report(30))
    with case.engine.connect() as conn:
        assert conn.execute(text('SELECT status,filament_used_g FROM print_jobs WHERE id=:id'), {'id': identifier}).one() == ('completed', None)


@pytest.mark.parametrize('order', ['callback_first', 'recovery_first'])
@pytest.mark.parametrize('fields', [
    dict(gcode_state='IDLE'),
    dict(gcode_state='FINISH'),
    dict(gcode_state='FAILED', subtask_name='another-print'),
], ids=['idle', 'unnamed', 'other_name'])
def test_unproven_unreserved_end_keeps_the_open_outcome(manual_bambu, order, fields):
    # What these really were is Shane's open decision: only the raw close stays, nothing new.
    case = manual_bambu
    monitor = case.monitor()
    observe(monitor, order, **fields)
    rows, job = manual_rows(case)
    assert [row['status'] for row in rows] == ['completed']
    assert job == 'printing'
    assert monitor._current_job_id is None
    assert case.seen.alerts == [] and case.seen.archive == []


@pytest.mark.parametrize('order', ['callback_first', 'recovery_first', 'status_after_wait'])
def test_older_duplicates_stay_stale_and_cannot_take_the_outcome(manual_bambu, order):
    from core.base import AlertType
    case = manual_bambu
    with case.engine.begin() as conn:
        conn.execute(text("INSERT INTO print_jobs(id,printer_id,filename,job_name,status,started_at) VALUES(:id,59,'manual.gcode',:name,'running',CURRENT_TIMESTAMP)"),
                     {'id': case.identifier + 1, 'name': case.name})
    monitor = case.monitor()
    fields = dict(gcode_state='FAILED', subtask_name=case.name, print_error=7)
    observe(monitor, order, **fields)
    packet(monitor, **fields)
    monitor._recover_orphaned_jobs()
    rows, job = manual_rows(case)
    assert [row['status'] for row in rows] == ['cancelled', 'failed']
    assert case.seen.alerts == [AlertType.PRINT_FAILED]
    assert [item['print_job_id'] for item in case.seen.archive] == [case.identifier + 1]


def latest_row(case, name):
    """A later running row under another name; the fixture's own row becomes the older one."""
    with case.engine.begin() as conn:
        return conn.execute(text("INSERT INTO print_jobs(id,printer_id,filename,job_name,status,started_at) "
                                 "VALUES(:id,59,'latest.gcode',:name,'running',CURRENT_TIMESTAMP) RETURNING id"),
                            {'id': case.identifier + 1, 'name': name}).scalar_one()


@pytest.mark.parametrize('order', ['callback_first', 'recovery_first'])
def test_older_row_with_another_name_does_not_stop_the_latest_ending(manual_bambu, order):
    case = manual_bambu
    latest = latest_row(case, 'latest-print')
    observe(case.monitor(), order, gcode_state='FAILED', subtask_name='latest-print', print_error=7)
    assert [(row['id'], row['status']) for row in manual_rows(case)[0]] == [(case.identifier, 'cancelled'), (latest, 'failed')]
    assert [item['print_job_id'] for item in case.seen.archive] == [latest]


@pytest.mark.parametrize('order', ['callback_first', 'recovery_first'])
def test_name_of_an_older_row_does_not_prove_the_latest_ended(manual_bambu, order):
    case = manual_bambu
    latest_row(case, 'latest-print')
    observe(case.monitor(), order, gcode_state='FAILED', subtask_name=case.name, print_error=7)
    assert [row['status'] for row in manual_rows(case)[0]] == ['completed', 'completed']  # The open outcome.
    assert case.seen.alerts == [] and case.seen.archive == []


@pytest.mark.parametrize('order', ['callback_first', 'recovery_first'])
def test_education_row_ended_in_downtime_keeps_its_policy_path(manual_bambu, order, monkeypatch):
    case = manual_bambu
    with case.engine.begin() as conn:
        conn.execute(text('UPDATE education_submissions SET job_id=53 WHERE id=(SELECT MIN(id) FROM education_submissions)'))
    monitor = case.monitor()
    monkeypatch.setattr(monitor, '_job_ended', lambda status: pytest.fail('Education terminal authority bypassed'))
    observe(monitor, order, gcode_state='FINISH', subtask_name=case.name)
    assert monitor._current_job_id is None
    assert case.seen.alerts == []


@pytest.fixture
def owned_bambu(creators, review_db, tmp_path, monkeypatch):
    """An order-owned dispatched print, so a terminal claims its durable summary and alert intent."""
    import hashlib
    from contextlib import contextmanager
    from core.db_utils import _ConnectionAdapter
    from core.registry import registry
    from modules.archives import archive
    from modules.notifications import alert_dispatch, alert_outbox, event_dispatcher
    from modules.orders.production import OrderFulfillmentService
    from modules.orders.routes.orders_crud import router
    from modules.printers import dispatch
    from modules.printers.monitors import mqtt_job_lifecycle, mqtt_printer
    from tests.test_contracts.dispatch_test_support import configure_dispatch
    client, auth, _, _ = creators
    client.app.include_router(router, prefix='/api')
    monkeypatch.setitem(registry._providers, 'OrderFulfillmentProvider', OrderFulfillmentService())
    engine, source = configure_dispatch(review_db, tmp_path, monkeypatch, protocol='bambu')
    with engine.begin() as c:
        c.execute(text("INSERT INTO models(id,name,org_id,quantity_per_bed,build_time_hours,default_filament_type) VALUES(101,'Owned model',1,1,1,'PLA')"))
        c.execute(text("INSERT INTO products(id,name,org_id,price) VALUES(101,'Owned product',1,20)"))
        c.execute(text('INSERT INTO product_components(product_id,model_id,quantity_needed) VALUES(101,101,1)'))
        c.execute(text('INSERT INTO print_files(id,model_id,filename,original_filename,stored_path,org_id) VALUES(58,101,:name,:name,:source,1)'), {'name': source.name, 'source': str(source)})
    reply = client.post('/api/orders', headers=auth('admin'), json={'order_number': 'Owned order', 'items': [{'product_id': 101, 'quantity': 1, 'unit_price': 20}]})
    assert reply.status_code == 200, reply.text
    order_id = reply.json()['id']
    reply = client.post(f'/api/orders/{order_id}/schedule', headers=auth('admin'))
    assert reply.status_code == 200, reply.text
    with engine.begin() as c:
        job = c.execute(text('SELECT min(j.id) FROM jobs j JOIN order_items i ON i.id=j.order_item_id WHERE i.order_id=:id'), {'id': order_id}).scalar_one()
        c.execute(text('UPDATE jobs SET printer_id=59,print_file_id=58,payload_sha256=:sha,plate_index=0 WHERE id=:id'), {'sha': hashlib.sha256(source.read_bytes()).hexdigest(), 'id': job})
    names = []
    monkeypatch.setattr(dispatch, '_dispatch_bambu', lambda jid, path, name, creds, **kw: (names.append(name) or True, 'Owned stub'))
    assert dispatch.dispatch_job(59, job)[0]

    @contextmanager
    def database():
        raw = engine.raw_connection()
        try:
            yield _ConnectionAdapter(raw, engine.dialect.name == 'postgresql')
        finally:
            raw.close()

    monkeypatch.setattr(mqtt_job_lifecycle, 'get_db', database)
    monkeypatch.setattr(mqtt_printer, 'get_db', database)
    alerts = []
    monkeypatch.setattr(alert_outbox, 'dispatch', lambda *a: alerts.append(a))
    monkeypatch.setattr(alert_dispatch, 'dispatch_alert', lambda **kw: pytest.fail('Durable intent must deliver this alert'))
    monkeypatch.setattr(alert_dispatch, '_start_bed_cooled_monitor', lambda *a: None)
    monkeypatch.setattr(event_dispatcher, 'increment_care_counters', lambda *a: None)
    monkeypatch.setattr(archive, 'create_print_archive', lambda **kw: None)
    identifier, linked, _ = mqtt_job_lifecycle.record_job_started(
        59, 'Owned', {'subtask_name': names[0], 'gcode_file': 'Metadata/plate_1.gcode'}, lambda **kw: None, lambda: None)
    assert identifier and linked == job

    def monitor():
        result = mqtt_printer.PrinterMonitor(59, 'Owned', '127.0.0.1', 'SYNTHETICPRINTER', 'synthetic')
        result._state = {}
        result._status_received.set()
        result._last_heartbeat = result._last_progress_update = result._last_spool_check = time.time()
        result._camera_discovered = True
        return result

    return SimpleNamespace(engine=engine, monitor=monitor, identifier=identifier, job=job, name=names[0], alerts=alerts)


def tray_markers(engine):
    with engine.connect() as conn:
        return conn.execute(text("SELECT key FROM system_config WHERE key LIKE '%tray_evidence_incomplete'")).scalars().all()


def test_reconciled_unreserved_end_marks_its_tray_evidence_incomplete(manual_bambu):
    case = manual_bambu
    packet(case.monitor(), gcode_state='FINISH', subtask_name=case.name, print_error=0)
    from core.filament_ledger import print_marker
    assert tray_markers(case.engine) == [print_marker(53, case.identifier) + ':tray_evidence_incomplete']


@pytest.mark.parametrize('state,error,expected', OUTCOMES)
def test_reconciled_end_cannot_charge_a_single_tray_it_did_not_watch(owned_bambu, state, error, expected):
    import json
    from modules.archives import attempt_accounting
    case = owned_bambu
    # One tray seen before the restart would otherwise resolve the plan to that tray.
    with case.engine.begin() as conn:
        assert attempt_accounting.record_attempt_tray(conn, case.identifier, 59, 2)
    packet(case.monitor(), gcode_state=state, subtask_name=case.name, print_error=error)
    assert tray_markers(case.engine)
    with case.engine.connect() as conn:
        snapshot = json.loads(conn.execute(text('SELECT spool_snapshot FROM print_jobs WHERE id=:id'), {'id': case.identifier}).scalar_one())
        assert attempt_accounting.resolve_attempt_material_plan(conn, snapshot, case.identifier)['reason'] == 'tray_evidence_incomplete'


def test_watched_end_keeps_its_tray_evidence(owned_bambu):
    import json
    from modules.archives import attempt_accounting
    case = owned_bambu
    monitor = case.monitor()
    packet(monitor, gcode_state='RUNNING', subtask_name=case.name, print_error=0, ams={'tray_now': 2})
    assert monitor._current_job_id == case.identifier
    packet(monitor, gcode_state='FINISH', subtask_name=case.name, print_error=0)
    assert tray_markers(case.engine) == []
    with case.engine.connect() as conn:
        assert conn.execute(text('SELECT status FROM print_jobs WHERE id=:id'), {'id': case.identifier}).scalar_one() == 'completed'
        snapshot = json.loads(conn.execute(text('SELECT spool_snapshot FROM print_jobs WHERE id=:id'), {'id': case.identifier}).scalar_one())
        assert attempt_accounting.resolve_attempt_material_plan(conn, snapshot, case.identifier)['reason'] != 'tray_evidence_incomplete'


@pytest.mark.parametrize('order', ['callback_first', 'recovery_first', 'status_after_wait'])
@pytest.mark.parametrize('state,error,expected', OUTCOMES)
def test_durable_terminal_intent_is_claimed_once(owned_bambu, order, state, error, expected):
    case = owned_bambu
    monitor = case.monitor()
    fields = dict(gcode_state=state, subtask_name=case.name, print_error=error)
    observe(monitor, order, **fields)
    packet(monitor, **fields)
    monitor._recover_orphaned_jobs()
    assert len(case.alerts) == 1
    with case.engine.connect() as conn:
        uid, status = conn.execute(text('SELECT economic_uid,status FROM print_jobs WHERE id=:id'), {'id': case.identifier}).one()
        assert uid and status == expected
        assert conn.execute(text('SELECT status FROM jobs WHERE id=:id'), {'id': case.job}).scalar_one() == expected
        assert tuple(conn.execute(text('SELECT print_attempt_uid,outcome FROM job_summary_outbox')).one()) == (uid, expected)
        assert conn.execute(text("SELECT count(*) FROM alert_outbox WHERE alert_kind LIKE 'terminal:%'")).scalar_one() == 1
        assert conn.execute(text('SELECT count(*) FROM printer_dispatch_claims')).scalar_one() == 0
        assert conn.execute(text('SELECT count(*) FROM print_jobs')).scalar_one() == 1


# ---------------------------------------------------------------- gap 2: Moonraker
def moonraker_status(raw_state, filename='part.gcode', total=3600.0):
    """Exactly what the adapter hands the monitor for this print_stats object."""
    from modules.printers.adapters.moonraker import MoonrakerState, MoonrakerStatus
    from modules.printers.parsing.moonraker import parse_status
    status = MoonrakerStatus()
    for name, value in parse_status({'print_stats': {'state': raw_state, 'filename': filename, 'total_duration': total,
                                                     'print_duration': total, 'filament_used': 0.0}}).items():
        if name == 'state':
            status.state = MoonrakerState(value)
        elif hasattr(status, name):
            setattr(status, name, value)
    return status


def ago(seconds):
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).strftime('%Y-%m-%d %H:%M:%S')


@pytest.fixture
def moonraker(review_db, tmp_path, monkeypatch):
    from core import event_bus, events, job_timeline
    from modules.archives import archive
    from modules.notifications import event_dispatcher
    from modules.printers import dispatch
    from modules.printers.monitors import moonraker_monitor as module
    from tests.test_contracts.test_dispatch_real_monitor_correlation import configure_dispatch
    engine, _ = configure_dispatch(review_db, tmp_path, monkeypatch, protocol='moonraker')
    monkeypatch.setattr(module, 'engine', engine)
    seen = SimpleNamespace(alerts=[], timeline=[], archive=[], completed=[], care=[], errors=[])
    event_bus.get_event_bus().subscribe(events.JOB_COMPLETED, lambda event: seen.completed.append(event))
    monkeypatch.setattr(event_dispatcher, 'dispatch_alert', lambda **kw: seen.alerts.append(kw['alert_type']))
    monkeypatch.setattr(event_dispatcher, 'record_error', lambda **kw: seen.errors.append(kw))
    monkeypatch.setattr(event_dispatcher, 'increment_care_counters', lambda *a: seen.care.append(a))
    monkeypatch.setattr(job_timeline, 'enqueue', lambda item: seen.timeline.append(item['kind']))
    monkeypatch.setattr(archive, 'create_print_archive', lambda **kw: seen.archive.append(kw))
    monkeypatch.setattr(module.MoonrakerMonitor, '_try_dispatch', lambda self: None)
    monkeypatch.setattr(module.MoonrakerMonitor, '_try_auto_link', lambda self, *a: None)

    def monitor(history=None):
        result = module.MoonrakerMonitor(59, 'Synthetic', '127.0.0.1')
        result.printer = SimpleNamespace(get_job_history=lambda limit: history or [])
        result._last_heartbeat = time.time()
        return result

    def row(name='part.gcode', started=3600, job=None):
        with engine.begin() as conn:
            return conn.execute(text("INSERT INTO print_jobs(printer_id,filename,job_name,status,started_at,scheduled_job_id) "
                                     "VALUES(59,:name,:name,'running',:start,:job) RETURNING id"),
                                {'name': name, 'start': ago(started), 'job': job}).scalar_one()

    def state(identifier):
        with engine.connect() as conn:
            return conn.execute(text('SELECT status,ended_at FROM print_jobs WHERE id=:id'), {'id': identifier}).one()

    def seen_printer(seconds):
        with engine.begin() as conn:
            conn.execute(text('UPDATE printers SET last_seen=:at WHERE id=59'), {'at': ago(seconds)})

    return SimpleNamespace(engine=engine, monitor=monitor, row=row, state=state, seen=seen, dispatch=dispatch, seen_printer=seen_printer)


def running_row_count(case):
    with case.engine.connect() as conn:
        return conn.execute(text("SELECT count(*) FROM print_jobs WHERE status='running'")).scalar_one()


MOONRAKER_OUTCOMES = [('complete', 'completed'), ('cancelled', 'cancelled'), ('error', 'failed')]


@pytest.mark.parametrize('raw,expected', MOONRAKER_OUTCOMES)
def test_moonraker_end_without_history_closes_its_row_once(moonraker, raw, expected):
    case = moonraker
    # An hour unwatched: only measured at last_seen does the row's age fit inside the print.
    identifier = case.row(started=2 * 3600, job=53)
    case.seen_printer(3600)
    monitor = case.monitor()
    status = moonraker_status(raw, total=1.5 * 3600)
    monitor._process_status(status)
    monitor._process_status(status)
    assert case.state(identifier)[0] == expected
    with case.engine.connect() as conn:
        assert conn.execute(text('SELECT status FROM jobs WHERE id=53')).scalar_one() == expected
        assert conn.execute(text('SELECT count(*) FROM print_jobs')).scalar_one() == 1
    assert monitor._current_job_db_id is None and monitor._prev_state != 'RUNNING'
    assert len(case.seen.alerts) == 1 and len(case.seen.archive) == 1
    assert [kind for kind in case.seen.timeline if kind in ('completed', 'failed', 'cancelled')] == [expected]
    assert len(case.seen.completed) == len(case.seen.care) == int(expected == 'completed')


@pytest.mark.parametrize('raw,recorded,expected', [
    ('complete', 'completed', 'completed'), ('cancelled', 'cancelled', 'cancelled'), ('error', 'error', 'failed'),
    ('complete', 'cancelled', 'cancelled'),  # The recorded truth outranks the state.
])
def test_moonraker_history_supplies_truth_long_after_the_end(moonraker, raw, recorded, expected):
    # Ended two hours before the first look: far outside the live end's +/-300 s.
    case = moonraker
    identifier = case.row(started=3 * 3600)
    start = time.time() - 3 * 3600
    end = time.time() - 2 * 3600
    monitor = case.monitor(history=[dict(filename='part.gcode', status=recorded, start_time=start, end_time=end, print_duration=3500)])
    monitor._process_status(moonraker_status(raw, total=3600))
    status, ended = case.state(identifier)
    assert status == expected
    assert as_utc_datetime(ended) == datetime.fromtimestamp(end, timezone.utc)


def test_moonraker_history_of_another_print_does_not_decide(moonraker):
    # The only recorded job started after this row: it cannot be the job this row watched.
    case = moonraker
    identifier = case.row(started=1800)
    case.seen_printer(60)
    start = time.time() - 600
    monitor = case.monitor(history=[dict(filename='part.gcode', status='cancelled', start_time=start, end_time=time.time() - 300, print_duration=300)])
    monitor._process_status(moonraker_status('complete', total=3600))
    status, ended = case.state(identifier)
    assert status == 'completed'
    assert as_utc_datetime(ended) != datetime.fromtimestamp(start + 300, timezone.utc)


def history_job(status, start, end, total, printed):
    return dict(filename='part.gcode', status=status, start_time=start, end_time=end, total_duration=total, print_duration=printed)


def paused_print(status, ended_ago=60):
    """The real shape of a pause: 000007 cancelled, 000008 completed. Newest first, as Moonraker lists them."""
    end = time.time() - ended_ago
    start = end - 1402.767
    resumed = start + 630.946
    return start, resumed - 0.08, end, [history_job(status, resumed, end, 1081.0, 803.0),
                                        history_job('cancelled', start, resumed - 0.08, 309.0, 31.0)]


def test_moonraker_paused_print_finished_unwatched_records_its_true_end(moonraker):
    case = moonraker
    start, paused_at, end, jobs = paused_print('completed')
    identifier = case.row(started=time.time() - start, job=53)
    case.monitor(history=jobs)._process_status(moonraker_status('complete', total=1081))
    status, ended = case.state(identifier)
    assert status == 'completed'
    assert abs((as_utc_datetime(ended) - datetime.fromtimestamp(end, timezone.utc)).total_seconds()) < 1
    with case.engine.connect() as conn:
        row = conn.execute(text('SELECT started_at,print_duration_seconds FROM print_jobs WHERE id=:id'), {'id': identifier}).one()
        assert conn.execute(text('SELECT status FROM jobs WHERE id=53')).scalar_one() == 'completed'
    assert abs((as_utc_datetime(row[0]) - datetime.fromtimestamp(start, timezone.utc)).total_seconds()) < 2 and row[1] == 803
    assert len(case.seen.completed) == 1


def test_moonraker_paused_print_cancelled_unwatched_stays_cancelled_at_its_real_end(moonraker):
    # 000005 and 000006: a pause, a resume, then a real cancel. The first job is not its end.
    case = moonraker
    start, paused_at, end, jobs = paused_print('cancelled')
    identifier = case.row(started=time.time() - start)
    case.monitor(history=jobs)._process_status(moonraker_status('cancelled', total=1081))
    status, ended = case.state(identifier)
    assert status == 'cancelled'
    assert abs((as_utc_datetime(ended) - datetime.fromtimestamp(end, timezone.utc)).total_seconds()) < 1


def test_moonraker_print_cancelled_while_paused_with_no_resume_stays_cancelled(moonraker):
    case = moonraker
    end = time.time() - 60
    identifier = case.row(started=1000)
    monitor = case.monitor(history=[history_job('cancelled', end - 940, end, 309.0, 31.0)])
    monitor._process_status(moonraker_status('cancelled', total=309))
    status, ended = case.state(identifier)
    assert status == 'cancelled'
    assert abs((as_utc_datetime(ended) - datetime.fromtimestamp(end, timezone.utc)).total_seconds()) < 1


def test_moonraker_cancelled_print_is_not_joined_to_the_next_print_of_that_file(moonraker):
    # 00000E and 00000F, 95.8 s apart, each with its own clock. The printer's complete is the
    # later print's; this row watched the earlier one.
    case = moonraker
    first_end = time.time() - 2000
    first_start = first_end - 601.3
    identifier = case.row(started=time.time() - first_start)
    second_start = first_end + 95.8
    monitor = case.monitor(history=[history_job('completed', second_start, second_start + 1903.4, 1904.0, 1545.0),
                                    history_job('cancelled', first_start, first_end, 601.0, 601.0)])
    monitor._process_status(moonraker_status('complete', total=1904))
    status, ended = case.state(identifier)
    assert status == 'cancelled'
    assert abs((as_utc_datetime(ended) - datetime.fromtimestamp(first_end, timezone.utc)).total_seconds()) < 1


@pytest.mark.parametrize('gap,total,joined', [(0.08, 1081.0, True), (0.08, 771.8, False), (95.8, 1176.8, False)],
                         ids=['resumed', 'own_clock', 'too_late'])
def test_history_joins_only_a_job_that_opens_as_the_last_closes_and_keeps_its_clock(gap, total, joined):
    from modules.printers.monitors.moonraker_history import terminal_history
    start = 1000.0
    first_end = start + 630.865
    second_start = first_end + gap
    second_end = second_start + 771.821
    jobs = [history_job('completed', second_start, second_end, total, 803.0), history_job('cancelled', start, first_end, 309.0, 31.0)]
    found = terminal_history(SimpleNamespace(get_job_history=lambda limit: jobs), 'part.gcode', 0, row_started_at=start)
    assert found['status'] == ('completed' if joined else 'cancelled')
    assert found['ended_at'] == datetime.fromtimestamp(second_end if joined else first_end, timezone.utc).isoformat()
    assert found['duration'] == (803.0 if joined else 31.0)


def test_history_joins_a_print_paused_more_than_once():
    from modules.printers.monitors.moonraker_history import terminal_history
    jobs = [history_job('cancelled', 1000.0, 1100.0, 100.0, 90.0), history_job('cancelled', 1100.1, 1200.0, 200.0, 180.0),
            history_job('completed', 1200.1, 1300.0, 300.0, 270.0)]
    found = terminal_history(SimpleNamespace(get_job_history=lambda limit: jobs), 'part.gcode', 0, row_started_at=1000.0)
    assert (found['status'], found['duration']) == ('completed', 270.0)
    assert found['started_at'] == datetime.fromtimestamp(1000.0, timezone.utc).isoformat()
    assert found['ended_at'] == datetime.fromtimestamp(1300.0, timezone.utc).isoformat()


def test_history_of_a_print_still_in_progress_does_not_speak_for_its_end():
    # A pause's cancelled job, with the resumed job not yet settled, is not this print's outcome.
    from modules.printers.monitors.moonraker_history import terminal_history
    jobs = [dict(filename='part.gcode', status='in_progress', start_time=1100.1, end_time=None, total_duration=150.0, print_duration=120.0),
            history_job('cancelled', 1000.0, 1100.0, 100.0, 90.0)]
    history = SimpleNamespace(get_job_history=lambda limit: jobs)
    assert terminal_history(history, 'part.gcode', 1200.0) is None
    assert terminal_history(history, 'part.gcode', 1200.0, row_started_at=1000.0) is None


@pytest.mark.parametrize('resumed_ago', [772, 100], ids=['pause_long_ago', 'pause_just_before_the_end'])
def test_moonraker_watched_paused_print_keeps_its_start_and_outcome(moonraker, resumed_ago):
    # Watched to the end. One job alone moved the start to the resume; two made history ambiguous.
    case = moonraker
    now = time.time()
    end, resumed = now - 1, now - resumed_ago
    start = resumed - 630.946
    monitor = case.monitor()
    monitor._process_status(moonraker_status('printing', total=5))
    identifier = monitor._current_job_db_id
    with case.engine.begin() as conn:
        conn.execute(text('UPDATE print_jobs SET started_at=:at WHERE id=:id'), {'at': datetime.fromtimestamp(start, timezone.utc).strftime('%Y-%m-%d %H:%M:%S'), 'id': identifier})
    elapsed = 309.0 + (end - resumed)
    monitor.printer = SimpleNamespace(get_job_history=lambda limit: [history_job('completed', resumed, end, elapsed, 803.0),
                                                                     history_job('cancelled', start, resumed - 0.08, 309.0, 31.0)])
    monitor._process_status(moonraker_status('complete', total=elapsed))
    status, ended = case.state(identifier)
    assert status == 'completed'
    assert abs((as_utc_datetime(ended) - datetime.fromtimestamp(end, timezone.utc)).total_seconds()) < 1
    with case.engine.connect() as conn:
        row = conn.execute(text('SELECT started_at,print_duration_seconds FROM print_jobs WHERE id=:id'), {'id': identifier}).one()
    assert abs((as_utc_datetime(row[0]) - datetime.fromtimestamp(start, timezone.utc)).total_seconds()) < 2 and row[1] == 803


def test_moonraker_history_that_ended_before_the_row_started_does_not_decide(moonraker):
    # The same file printed and ended before this row existed: not the job this row watched.
    case = moonraker
    identifier = case.row(started=1800)
    case.seen_printer(60)
    end = time.time() - 3600
    monitor = case.monitor(history=[dict(filename='part.gcode', status='cancelled', start_time=end - 3600, end_time=end, print_duration=3500)])
    monitor._process_status(moonraker_status('complete', total=1700))
    status, ended = case.state(identifier)
    assert status == 'completed'
    assert as_utc_datetime(ended) != datetime.fromtimestamp(end, timezone.utc)


@pytest.mark.parametrize('raw', ['standby', 'ready'])
def test_moonraker_unknown_outcome_is_not_guessed(moonraker, raw):
    case = moonraker
    identifier = case.row()
    case.seen_printer(60)
    case.monitor()._process_status(moonraker_status(raw, filename=''))
    assert case.state(identifier)[0] == 'running'  # True outcome unknown: an open owner decision.


@pytest.mark.parametrize('case_name', ['other_name', 'no_name', 'two_rows', 'older_than_print', 'education_name', 'education_row'])
def test_moonraker_unproven_identity_changes_nothing(moonraker, case_name):
    case = moonraker
    name = {'other_name': 'another.gcode', 'no_name': '', 'education_name': 'odin-' + 'a' * 32 + '.gcode'}.get(case_name, 'part.gcode')
    identifier = case.row(name=name if case_name == 'education_name' else 'part.gcode',
                          started=3 * 86400 if case_name == 'older_than_print' else 3600,
                          job=53 if case_name == 'education_row' else None)
    if case_name == 'two_rows':
        case.row()
    if case_name == 'education_row':
        with case.engine.begin() as conn:
            conn.execute(text('UPDATE education_submissions SET job_id=53 WHERE id=(SELECT MIN(id) FROM education_submissions)'))
    case.seen_printer(60)
    with case.engine.connect() as conn:
        before = conn.execute(text('SELECT count(*) FROM print_jobs')).scalar_one()
    monitor = case.monitor()
    monitor._process_status(moonraker_status('complete', filename=name, total=3600))
    assert case.state(identifier)[0] == 'running'
    assert monitor._current_job_db_id is None
    assert case.seen.alerts == [] and case.seen.archive == []
    with case.engine.connect() as conn:
        assert conn.execute(text('SELECT count(*) FROM print_jobs')).scalar_one() == before


def test_moonraker_end_lost_to_a_transient_error_retries_and_the_next_print_is_new(moonraker, monkeypatch):
    from modules.printers.monitors import moonraker_monitor as module
    case = moonraker
    identifier = case.row(job=53)
    case.seen_printer(120)
    monitor = case.monitor()
    prepare = module.prepare_print_job_write
    monkeypatch.setattr(module, 'prepare_print_job_write', lambda *a: (_ for _ in ()).throw(RuntimeError('synthetic contention')))
    status = moonraker_status('complete')
    monitor._process_status(status)
    assert case.state(identifier)[0] == 'running'
    assert monitor._current_job_db_id == identifier and monitor._prev_state == 'RUNNING'
    assert case.seen.alerts == [] and case.seen.errors == []
    monkeypatch.setattr(module, 'prepare_print_job_write', prepare)
    monitor._process_status(status)
    monitor._process_status(status)
    assert case.state(identifier)[0] == 'completed'
    assert monitor._current_job_db_id is None and monitor._prev_state == 'IDLE'
    assert len(case.seen.alerts) == 1 and len(case.seen.archive) == 1
    # The stale tracking was what made a following print look already tracked.
    monitor._process_status(moonraker_status('printing', filename='next.gcode', total=5))
    assert monitor._current_job_db_id not in (None, identifier)


def test_moonraker_retried_end_still_places_old_history_by_its_row(moonraker, monkeypatch):
    # Ended two hours before the first look: a retry on the live +/-300 s would lose the truth.
    from modules.printers.monitors import moonraker_monitor as module
    case = moonraker
    identifier = case.row(started=3 * 3600)
    end = time.time() - 2 * 3600
    monitor = case.monitor(history=[dict(filename='part.gcode', status='cancelled', start_time=time.time() - 3 * 3600, end_time=end, print_duration=3500)])
    prepare = module.prepare_print_job_write
    monkeypatch.setattr(module, 'prepare_print_job_write', lambda *a: (_ for _ in ()).throw(RuntimeError('synthetic contention')))
    status = moonraker_status('complete', total=3600)
    monitor._process_status(status)
    assert case.state(identifier)[0] == 'running'
    monkeypatch.setattr(module, 'prepare_print_job_write', prepare)
    monitor._process_status(status)
    state, ended = case.state(identifier)
    assert state == 'cancelled' and as_utc_datetime(ended) == datetime.fromtimestamp(end, timezone.utc)


def test_moonraker_live_end_lost_to_a_transient_error_retries_too(moonraker, monkeypatch):
    from modules.printers.monitors import moonraker_monitor as module
    case = moonraker
    monitor = case.monitor()
    monitor._process_status(moonraker_status('printing', total=5))
    identifier = monitor._current_job_db_id
    prepare = module.prepare_print_job_write
    monkeypatch.setattr(module, 'prepare_print_job_write', lambda *a: (_ for _ in ()).throw(RuntimeError('synthetic contention')))
    monitor._process_status(moonraker_status('cancelled'))
    assert case.state(identifier)[0] == 'running' and monitor._prev_state == 'RUNNING'
    monkeypatch.setattr(module, 'prepare_print_job_write', prepare)
    monitor._process_status(moonraker_status('cancelled'))
    assert case.state(identifier)[0] == 'cancelled' and monitor._current_job_db_id is None


def test_moonraker_network_reconnect_closes_the_end(moonraker):
    case = moonraker
    identifier = case.row()
    case.seen_printer(300)
    monitor = case.monitor()
    monitor._current_job_db_id = identifier
    monitor._last_filename = 'part.gcode'
    monitor._prev_state = 'RUNNING'
    monitor._recover_after_reconnect()  # As _handle_disconnect does after any network drop.
    monitor._process_status(moonraker_status('error'))
    assert case.state(identifier)[0] == 'failed'
    assert monitor._current_job_db_id is None


def test_moonraker_failed_lookup_retries_on_the_next_poll(moonraker, monkeypatch):
    # With the real heartbeat running: it refreshes last_seen on the failed poll, and the
    # retry still has to measure the row's age at the time ODIN last saw this printer.
    from core import dispatch_observations
    case = moonraker
    identifier = case.row(started=2 * 3600)
    case.seen_printer(3600)
    monitor = case.monitor()
    monitor._last_heartbeat = 0
    status = moonraker_status('complete', total=1.5 * 3600)
    reserved = dispatch_observations.generic_observation_reserved
    monkeypatch.setattr(dispatch_observations, 'generic_observation_reserved', lambda *a, **kw: (_ for _ in ()).throw(RuntimeError('synthetic')))
    monitor._process_status(status)
    assert case.state(identifier)[0] == 'running'
    assert monitor._prev_state is None
    with case.engine.connect() as conn:
        last_seen = conn.execute(text('SELECT last_seen FROM printers WHERE id=59')).scalar_one()
    assert (datetime.now(timezone.utc) - as_utc_datetime(last_seen)).total_seconds() < 60  # The heartbeat ran.
    monkeypatch.setattr(dispatch_observations, 'generic_observation_reserved', reserved)
    monitor._process_status(status)
    assert case.state(identifier)[0] == 'completed'


def test_moonraker_reconnect_measures_the_new_downtime(moonraker):
    case = moonraker
    monitor = case.monitor()
    monitor._first_look = (ago(30 * 86400),)
    monitor._recover_after_reconnect()
    assert monitor._first_look is None
    identifier = case.row(started=2 * 3600)
    case.seen_printer(3600)
    monitor._process_status(moonraker_status('complete', total=1.5 * 3600))
    assert case.state(identifier)[0] == 'completed'


def test_moonraker_end_without_history_is_bounded_by_the_print_duration(moonraker):
    case = moonraker
    identifier = case.row(started=2 * 3600)
    case.seen_printer(3600)
    case.monitor()._process_status(moonraker_status('complete', total=1.5 * 3600))
    status, ended = case.state(identifier)
    assert status == 'completed'
    with case.engine.connect() as conn:
        started, duration = conn.execute(text('SELECT started_at,print_duration_seconds FROM print_jobs WHERE id=:id'), {'id': identifier}).one()
    assert as_utc_datetime(ended) == as_utc_datetime(started) + timedelta(seconds=1.5 * 3600)
    assert duration == 1.5 * 3600


def test_moonraker_implausible_duration_does_not_lose_the_end(moonraker):
    # Beyond now, or unreadable: the bound does not apply and the row still closes.
    case = moonraker
    identifier = case.row(started=3600)
    case.seen_printer(60)
    case.monitor()._process_status(moonraker_status('complete', total=30 * 86400))
    assert case.state(identifier)[0] == 'completed'
    assert (datetime.now(timezone.utc) - as_utc_datetime(case.state(identifier)[1])).total_seconds() < 60


def test_moonraker_live_end_is_not_bounded_by_the_print_duration(moonraker):
    case = moonraker
    monitor = case.monitor()
    monitor._process_status(moonraker_status('printing', total=5))
    identifier = monitor._current_job_db_id
    with case.engine.begin() as conn:
        conn.execute(text('UPDATE print_jobs SET started_at=:at WHERE id=:id'), {'at': ago(3600), 'id': identifier})
    monitor._process_status(moonraker_status('complete', total=60))
    assert (datetime.now(timezone.utc) - as_utc_datetime(case.state(identifier)[1])).total_seconds() < 60


def test_moonraker_bounded_end_still_retires_a_dispatched_claim(dispatched_moonraker):
    case = dispatched_moonraker
    with case.engine.begin() as conn:
        conn.execute(text('UPDATE print_jobs SET started_at=:at WHERE id=:id'), {'at': ago(2 * 3600), 'id': case.identifier})
        conn.execute(text('UPDATE jobs SET actual_start=:at WHERE id=53'), {'at': ago(2 * 3600)})
    case.seen_printer(3600)
    case.monitor()._process_status(moonraker_status('complete', filename=case.name, total=1.5 * 3600))
    assert case.state(case.identifier)[0] == 'completed'
    with case.engine.connect() as conn:
        assert conn.execute(text('SELECT count(*) FROM printer_dispatch_claims')).scalar_one() == 0


def test_moonraker_history_fetch_holds_no_lock(moonraker):
    case = moonraker
    case.row()
    monitor = case.monitor()
    fetched = []
    def history(limit):
        fetched.append(monitor._status_lock._is_owned())
        return []
    monitor.printer = SimpleNamespace(get_job_history=history)
    monitor._process_status(moonraker_status('complete'))
    assert fetched == [False]


@pytest.fixture
def dispatched_moonraker(moonraker, monkeypatch):
    case = moonraker
    names = []
    monkeypatch.setattr(case.dispatch, '_dispatch_moonraker', lambda jid, path, name, creds, **kw: (names.append(name) or True, 'Synthetic accepted'))
    assert case.dispatch.dispatch_job(59, 53)[0]
    started = case.monitor()
    started._job_started(moonraker_status('printing', filename=names[0], total=1))
    case.identifier = started._current_job_db_id
    case.name = names[0]
    assert case.identifier
    return case


def test_moonraker_unrelated_start_leaves_a_receipt_bound_row_and_its_claim(dispatched_moonraker):
    case = dispatched_moonraker
    monitor = case.monitor()
    monitor._process_status(moonraker_status('printing', filename='manual.gcode', total=5))
    assert case.state(case.identifier)[0] == 'running'
    assert monitor._current_job_db_id != case.identifier
    with case.engine.connect() as conn:
        assert conn.execute(text('SELECT count(*) FROM printer_dispatch_claims')).scalar_one() == 1
        assert conn.execute(text('SELECT status FROM jobs WHERE id=53')).scalar_one() == 'printing'
        assert conn.execute(text("SELECT count(*) FROM print_jobs WHERE status='running'")).scalar_one() == 2


EDUCATION_NAME = 'odin-' + 'a' * 32 + '.gcode'
DISPATCH_NAME = 'odin-dispatch-' + 'f' * 32 + '.gcode'


def assert_closed_unbound_reserved_row(status, ended, error):
    """Closed with its end unknown, which is what keeps it out of archive recovery."""
    from core.physical_print import CLOSED_UNKNOWN
    assert (status, ended, error) == ('cancelled', None, CLOSED_UNKNOWN)


@pytest.mark.parametrize('name', [EDUCATION_NAME, DISPATCH_NAME], ids=['education', 'dispatch'])
def test_moonraker_unrelated_start_closes_a_reserved_row_with_no_claim(moonraker, name):
    case = moonraker
    stale = case.row(name=name, started=3 * 86400)
    monitor = case.monitor()
    monitor._process_status(moonraker_status('printing', filename='manual.gcode', total=5))
    with case.engine.connect() as conn:
        assert_closed_unbound_reserved_row(*conn.execute(text('SELECT status,ended_at,error_code FROM print_jobs WHERE id=:id'), {'id': stale}).one())
    assert monitor._current_job_db_id != stale and case.state(monitor._current_job_db_id)[0] == 'running'
    assert running_row_count(case) == 1
    assert case.seen.alerts == [] and case.seen.archive == []


def test_moonraker_unrelated_start_under_a_held_claim_leaves_an_unknown_row(dispatched_moonraker):
    # Nothing about this row's name shows it is protected: only the claim does.
    case = dispatched_moonraker
    unnamed = case.row(name='Unknown', started=60)
    case.monitor()._process_status(moonraker_status('printing', filename='manual.gcode', total=5))
    assert case.state(unnamed)[0] == 'running'
    assert case.state(case.identifier)[0] == 'running'
    with case.engine.connect() as conn:
        assert conn.execute(text('SELECT count(*) FROM printer_dispatch_claims')).scalar_one() == 1


def test_moonraker_unrelated_start_never_closes_an_education_owned_row(moonraker):
    case = moonraker
    owned = case.row(name='part.gcode', started=3 * 86400, job=53)
    with case.engine.begin() as conn:
        conn.execute(text('UPDATE education_submissions SET job_id=53 WHERE id=(SELECT MIN(id) FROM education_submissions)'))
    monitor = case.monitor()
    monitor._process_status(moonraker_status('printing', filename='manual.gcode', total=5))
    assert case.state(owned)[0] == 'running'
    assert monitor._current_job_db_id != owned


@pytest.mark.parametrize('next_start', ['manual.gcode', DISPATCH_NAME], ids=['manual', 'same_retained_file'])
def test_moonraker_reprint_of_a_retained_dispatch_file_cannot_block_the_printer(moonraker, monkeypatch, next_start):
    # Reprinted from the printer's own UI: a reserved name, and no claim ever held for this row.
    case = moonraker
    monitor = case.monitor()
    monitor._process_status(moonraker_status('printing', filename=DISPATCH_NAME, total=5))
    reprint = monitor._current_job_db_id
    assert reprint
    # It ends unwatched, and the next start is the first this monitor sees.
    restarted_monitor = case.monitor()
    restarted_monitor._process_status(moonraker_status('printing', filename=next_start, total=5))
    following = restarted_monitor._current_job_db_id
    assert following != reprint and case.state(reprint)[0] == 'cancelled' and running_row_count(case) == 1
    restarted_monitor._process_status(moonraker_status('complete', filename=next_start, total=60))
    assert case.state(following)[0] == 'completed' and running_row_count(case) == 0
    monkeypatch.setattr(case.dispatch, '_dispatch_moonraker', lambda *a, **kw: (True, 'Synthetic accepted'))
    assert case.dispatch.dispatch_job(59, 53)[0]


def test_moonraker_education_named_start_closes_nothing(moonraker):
    case = moonraker
    stale = case.row(name='old.gcode', started=3 * 86400)
    monitor = case.monitor()
    monitor._process_status(moonraker_status('printing', filename='odin-' + 'a' * 32 + '.gcode', total=5))
    assert case.state(stale)[0] == 'running'
    assert running_row_count(case) == 2


@pytest.mark.parametrize('raw,expected', MOONRAKER_OUTCOMES)
def test_moonraker_dispatched_end_retires_the_claim_and_dispatches_again(dispatched_moonraker, raw, expected):
    case = dispatched_moonraker
    case.seen_printer(60)
    monitor = case.monitor()
    monitor._process_status(moonraker_status(raw, filename=case.name, total=60))
    assert case.state(case.identifier)[0] == expected
    with case.engine.connect() as conn:
        assert conn.execute(text('SELECT status FROM jobs WHERE id=53')).scalar_one() == expected
        assert conn.execute(text('SELECT count(*) FROM printer_dispatch_claims')).scalar_one() == 0
        assert conn.execute(text('SELECT count(*) FROM print_jobs')).scalar_one() == 1
    assert case.dispatch.dispatch_job(59, 54)[0]


@pytest.mark.parametrize('name,mutation', [
    ('another.gcode', None),
    ('odin-dispatch-' + 'f' * 32 + '.gcode', None),
    (None, "UPDATE printers SET api_host='changed.example.test' WHERE id=59"),
    (None, "UPDATE printer_dispatch_claims SET state='preparing' WHERE printer_id=59"),
], ids=['unrelated', 'other_reserved', 'changed_printer', 'changed_claim'])
def test_moonraker_dispatched_end_follows_the_receipt_rules(dispatched_moonraker, name, mutation):
    case = dispatched_moonraker
    if mutation:
        with case.engine.begin() as conn:
            conn.execute(text(mutation))
    case.seen_printer(60)
    monitor = case.monitor()
    monitor._process_status(moonraker_status('complete', filename=name or case.name, total=60))
    assert case.state(case.identifier)[0] == 'running'
    with case.engine.connect() as conn:
        assert conn.execute(text('SELECT count(*) FROM printer_dispatch_claims')).scalar_one() == 1
        assert conn.execute(text('SELECT count(*) FROM print_jobs')).scalar_one() == 1
    assert monitor._current_job_db_id is None


# ---------------------------------------------------------------- gap 3: PrusaLink, Elegoo, Anycubic
STOPPED = {'prusalink': 'STOPPED', 'elegoo': 'STOPPING', 'anycubic_lan': 'STOPPING'}


def generic_status(protocol, state, filename, timer=0):
    from modules.printers.adapters.anycubic_lan import DEFAULTS
    from modules.printers.adapters.elegoo import ElegooStatus
    from modules.printers.adapters.prusalink import PrusaLinkState, PrusaLinkStatus
    if protocol == 'prusalink':
        return PrusaLinkStatus(state=PrusaLinkState[state], internal_state=state, filename=filename, time_printing=timer)
    if protocol == 'elegoo':
        return ElegooStatus(internal_state=state, filename=filename, current_ticks=timer)
    return SimpleNamespace(**(DEFAULTS | dict(internal_state=state, filename=filename, current_ticks=timer, boxes=[])))


def prusalink_wire(state='PRINTING', job_id=297, timer=7718):
    """The documented /api/v1/status shape, through the adapter's parser: it names no file."""
    from modules.printers.adapters.prusalink import PrusaLinkPrinter, PrusaLinkStatus
    from modules.printers.parsing.prusalink import parse_v1_status
    status = PrusaLinkStatus()
    PrusaLinkPrinter._apply_parsed(status, parse_v1_status({
        'job': {'id': job_id, 'progress': 91.0, 'time_remaining': 600, 'time_printing': timer},
        'storage': {'path': '/usb/', 'name': 'usb', 'read_only': False},
        'printer': {'state': state, 'temp_bed': 60.0, 'target_bed': 60.0, 'temp_nozzle': 209.9, 'target_nozzle': 210.0,
                    'axis_z': 2.4, 'flow': 100, 'speed': 100, 'fan_hotend': 3099, 'fan_print': 5964}}))
    assert status.filename == ''
    return status


@pytest.fixture(params=['prusalink', 'elegoo', 'anycubic_lan'])
def generic(request, review_db, tmp_path, monkeypatch):
    from contextlib import contextmanager
    from core import event_bus, events, job_timeline
    from core.db_utils import _ConnectionAdapter
    from modules.archives import archive
    from modules.notifications import job_events, job_faults
    from modules.printers import dispatch
    from modules.printers.monitors import anycubic_lan_monitor, elegoo_monitor, prusalink_monitor
    from tests.test_contracts.test_dispatch_real_monitor_correlation import configure_dispatch
    protocol = request.param
    engine, _ = configure_dispatch(review_db, tmp_path, monkeypatch, protocol=protocol)

    @contextmanager
    def database():
        raw = engine.raw_connection()
        try:
            yield _ConnectionAdapter(raw, engine.dialect.name == 'postgresql')
        finally:
            raw.close()

    monkeypatch.setattr(job_events, 'get_db', database)
    bus = event_bus.get_event_bus()
    monkeypatch.setattr(job_events, 'get_event_bus', lambda: bus)
    seen = SimpleNamespace(alerts=[], archive=[], started=[], faults=[])
    bus.subscribe(events.JOB_STARTED, lambda event: seen.started.append(event))
    monkeypatch.setattr(job_events, 'dispatch_alert', lambda **kw: seen.alerts.append(kw['alert_type']))
    monkeypatch.setattr(job_events, '_start_bed_cooled_monitor', lambda *a: None)
    monkeypatch.setattr(job_events, 'increment_care_counters', lambda *a: None)
    monkeypatch.setattr(job_events, 'record_error', lambda **kw: None)
    monkeypatch.setattr(job_timeline, 'enqueue', lambda item: None)
    monkeypatch.setattr(job_faults, 'notify', lambda printer, kind, *a, **kw: seen.faults.append(kind))
    monkeypatch.setattr(archive, 'create_print_archive', lambda **kw: seen.archive.append(kw))
    monkeypatch.setattr(job_events, '_active_print_jobs', {})
    monkeypatch.setattr(prusalink_monitor.PrusaLinkMonitorThread, '_try_dispatch', lambda self: None)

    def monitor():
        job_events._active_print_jobs.clear()  # A restarted process tracks nothing in memory.
        make = {'prusalink': prusalink_monitor.PrusaLinkMonitorThread, 'elegoo': elegoo_monitor.ElegooMonitorThread,
                'anycubic_lan': anycubic_lan_monitor.AnycubicLANMonitorThread}[protocol]
        result = make(59, 'Synthetic', '127.0.0.1')
        result._last_heartbeat = time.time()
        return result

    def rows():
        with engine.connect() as conn:
            return [dict(row) for row in conn.execute(text('SELECT id,filename,status,ended_at,error_code FROM print_jobs ORDER BY id')).mappings()]

    return SimpleNamespace(engine=engine, protocol=protocol, monitor=monitor, rows=rows, seen=seen, dispatch=dispatch,
                           tracked=job_events._active_print_jobs,
                           status=lambda state, filename='part.gcode', timer=0: generic_status(protocol, state, filename, timer))


def test_generic_print_continuing_through_a_restart_keeps_its_row(generic):
    case = generic
    case.monitor()._process_status(case.status('PRINTING'))
    before = case.rows()
    assert [row['status'] for row in before] == ['running'] and len(case.seen.started) == 1
    monitor = case.monitor()
    monitor._process_status(case.status('PRINTING'))
    assert case.rows() == before
    assert case.tracked[59] == before[0]['id']
    assert len(case.seen.started) == 1
    monitor._process_status(case.status('FINISHED'))
    assert [row['status'] for row in case.rows()] == ['completed']
    assert len(case.seen.alerts) == 1


def age_row(case, identifier, seconds):
    with case.engine.begin() as conn:
        conn.execute(text('UPDATE print_jobs SET started_at=:at WHERE id=:id'), {'at': ago(seconds), 'id': identifier})


def test_generic_reprint_days_later_gets_its_own_row(generic):
    # The first print ended unwatched and the row stayed running. The same file again, with its
    # timer just started, cannot be that row: it must not inherit its start or spool snapshot.
    from core.physical_print import CLOSED_UNKNOWN
    case = generic
    case.monitor()._process_status(case.status('PRINTING'))
    old = case.rows()[0]['id']
    age_row(case, old, 3 * 86400)
    started = len(case.seen.started)
    monitor = case.monitor()
    monitor._process_status(case.status('PRINTING', timer=60))
    first, second = case.rows()
    assert (first['id'], first['status'], first['ended_at'], first['error_code']) == (old, 'cancelled', None, CLOSED_UNKNOWN)
    assert second['status'] == 'running' and case.tracked[59] == second['id']
    assert len(case.seen.started) == started + 1
    assert case.seen.alerts == [] and case.seen.archive == []


def test_generic_watched_start_is_new_however_recent_the_stale_row(generic):
    # This monitor saw the printer idle, so no pause allowance applies to the row left behind.
    case = generic
    case.monitor()._process_status(case.status('PRINTING'))
    old = case.rows()[0]['id']
    age_row(case, old, 2 * 3600)
    started = len(case.seen.started)
    monitor = case.monitor()
    monitor._process_status(case.status('IDLE', filename=''))
    assert case.rows()[0]['status'] == 'running'
    monitor._process_status(case.status('PRINTING', timer=5))
    first, second = case.rows()
    assert first['id'] == old and first['status'] == 'cancelled'
    assert second['status'] == 'running' and case.tracked[59] == second['id']
    assert len(case.seen.started) == started + 1


@pytest.mark.parametrize('timer,age', [(3600, 3600), (3600, 10 * 3600)], ids=['no_pause', 'long_pause'])
def test_generic_first_look_at_a_continuing_print_resumes_within_the_pause_allowance(generic, timer, age):
    case = generic
    case.monitor()._process_status(case.status('PRINTING'))
    before = case.rows()
    age_row(case, before[0]['id'], age)
    case.monitor()._process_status(case.status('PRINTING', timer=timer))
    assert [row['id'] for row in case.rows()] == [before[0]['id']]
    assert case.rows()[0]['status'] == 'running' and case.tracked[59] == before[0]['id']


def test_generic_first_look_past_the_pause_allowance_is_refused(generic):
    from core.physical_print import PAUSE_ALLOWANCE_SECONDS
    case = generic
    case.monitor()._process_status(case.status('PRINTING'))
    old = case.rows()[0]['id']
    age_row(case, old, 3600 + PAUSE_ALLOWANCE_SECONDS + 300)
    case.monitor()._process_status(case.status('PRINTING', timer=3600))
    assert [row['status'] for row in case.rows()] == ['cancelled', 'running']


def test_start_with_no_timer_evidence_neither_resumes_nor_closes(generic):
    from modules.notifications import job_events
    case = generic
    case.monitor()._process_status(case.status('PRINTING'))
    first = case.rows()[0]['id']
    assert job_events.job_started(59, 'part.gcode') != first
    assert [row['status'] for row in case.rows()] == ['running', 'running']


@pytest.mark.parametrize('generic', ['prusalink'], indirect=True)
def test_prusalink_real_status_shape_keeps_one_row_through_a_restart(generic):
    case = generic
    case.monitor()._process_status(prusalink_wire())
    before = case.rows()
    assert [(row['filename'], row['status']) for row in before] == [('Unknown', 'running')]
    age_row(case, before[0]['id'], 3 * 86400)  # The same job id decides, however long it paused.
    case.monitor()._process_status(prusalink_wire(timer=7800))
    assert [row['id'] for row in case.rows()] == [before[0]['id']]
    assert case.tracked[59] == before[0]['id']
    assert len(case.seen.started) == 1


@pytest.mark.parametrize('generic', ['prusalink'], indirect=True)
def test_prusalink_next_job_id_is_a_new_print(generic):
    from core.physical_print import CLOSED_UNKNOWN
    case = generic
    case.monitor()._process_status(prusalink_wire(job_id=297))
    old = case.rows()[0]['id']
    case.monitor()._process_status(prusalink_wire(job_id=298, timer=5))
    first, second = case.rows()
    assert (first['id'], first['status'], first['error_code']) == (old, 'cancelled', CLOSED_UNKNOWN)
    assert second['status'] == 'running' and case.tracked[59] == second['id']


@pytest.mark.parametrize('generic', ['prusalink'], indirect=True)
def test_prusalink_same_file_next_job_id_is_a_new_print_even_with_a_fitting_timer(generic):
    from modules.printers.adapters.prusalink import PrusaLinkState, PrusaLinkStatus
    case = generic
    def status(job_id):
        return PrusaLinkStatus(state=PrusaLinkState.PRINTING, internal_state='PRINTING', filename='part.gcode', time_printing=60, job_id=job_id)
    case.monitor()._process_status(status(297))
    case.monitor()._process_status(status(298))
    assert [row['status'] for row in case.rows()] == ['cancelled', 'running']


@pytest.mark.parametrize('generic', ['prusalink'], indirect=True)
@pytest.mark.parametrize('name', [EDUCATION_NAME, DISPATCH_NAME], ids=['education', 'dispatch'])
def test_prusalink_matching_job_id_does_not_resume_a_reserved_row(generic, name):
    # A reserved name's owner decides what it was, even when the printer's job id matches.
    case = generic
    with case.engine.begin() as conn:
        conn.execute(text("INSERT INTO print_jobs(id,printer_id,job_id,filename,job_name,status,started_at) VALUES(700,59,'prusalink_297',:name,:name,'running',CURRENT_TIMESTAMP)"), {'name': name})
    case.monitor()._process_status(prusalink_wire(job_id=297))
    rows = {row['id']: row for row in case.rows()}
    assert case.tracked[59] != 700 and rows[case.tracked[59]]['status'] == 'running'
    assert rows[700]['status'] == 'cancelled'


@pytest.mark.parametrize('generic', ['prusalink'], indirect=True)
def test_prusalink_job_id_cannot_adopt_an_unknown_row_it_cannot_compare(generic):
    # A row from before job ids were kept has only the name Unknown to compare.
    from core.physical_print import CLOSED_UNKNOWN
    case = generic
    with case.engine.begin() as conn:
        conn.execute(text("INSERT INTO print_jobs(id,printer_id,filename,job_name,status,started_at) VALUES(700,59,'Unknown','Unknown','running',CURRENT_TIMESTAMP)"))
    case.monitor()._process_status(prusalink_wire())
    first, second = case.rows()
    assert (first['id'], first['status'], first['error_code']) == (700, 'cancelled', CLOSED_UNKNOWN)
    assert second['status'] == 'running'


def elegoo_frame(print_status, current=0, ticks=0, filename='part.gcode'):
    """Through the real SDCP parser. Status 13 and 9 with CurrentStatus 0 parse as UNKNOWN."""
    from modules.printers.adapters.elegoo import ElegooStatus
    from modules.printers.parsing.elegoo import parse_status
    status = ElegooStatus()
    for name, value in parse_status({'Status': {'CurrentStatus': [current], 'PrintInfo': {
            'Status': print_status, 'Filename': filename, 'CurrentTicks': ticks, 'TotalTicks': 7200, 'Progress': 50}}}).items():
        if hasattr(status, name):
            setattr(status, name, value)
    return status


def prusalink_named_wire(state, timer=0):
    """/api/v1/status through the real parser, from firmware that names the file but gives no job id."""
    from modules.printers.adapters.prusalink import PrusaLinkPrinter, PrusaLinkStatus
    from modules.printers.parsing.prusalink import parse_v1_status
    status = PrusaLinkStatus()
    PrusaLinkPrinter._apply_parsed(status, parse_v1_status({
        'job': {'file': {'display': 'part.gcode'}, 'time_printing': timer}, 'printer': {'state': state}}))
    return status


@pytest.mark.parametrize('generic', ['elegoo'], indirect=True)
@pytest.mark.parametrize('undecided', [13, 9])
def test_elegoo_first_look_lasts_until_a_known_state(generic, undecided):
    case = generic
    assert elegoo_frame(undecided).internal_state == 'UNKNOWN'
    case.monitor()._process_status(elegoo_frame(8, ticks=60))
    before = case.rows()
    age_row(case, before[0]['id'], 10 * 3600)
    started = len(case.seen.started)
    monitor = case.monitor()
    monitor._process_status(elegoo_frame(undecided, ticks=3600))
    monitor._process_status(elegoo_frame(8, ticks=3600))
    assert [row['id'] for row in case.rows()] == [before[0]['id']]
    assert case.rows()[0]['status'] == 'running' and case.tracked[59] == before[0]['id']
    assert len(case.seen.started) == started


@pytest.mark.parametrize('generic', ['elegoo'], indirect=True)
@pytest.mark.parametrize('end,expected', [(16, 'completed'), (7, 'cancelled')], ids=['finished', 'stopped'])
def test_elegoo_end_after_an_undecided_first_frame_still_closes_the_row(generic, end, expected):
    case = generic
    case.monitor()._process_status(elegoo_frame(8, ticks=60))
    monitor = case.monitor()
    monitor._process_status(elegoo_frame(13))
    monitor._process_status(elegoo_frame(end))
    assert [row['status'] for row in case.rows()] == [expected]


@pytest.mark.parametrize('generic', ['elegoo'], indirect=True)
def test_elegoo_undecided_frame_during_a_watched_start_is_not_a_first_look(generic):
    case = generic
    case.monitor()._process_status(elegoo_frame(8, ticks=60))
    old = case.rows()[0]['id']
    age_row(case, old, 2 * 3600)
    monitor = case.monitor()
    monitor._process_status(elegoo_frame(0))
    assert case.rows()[0]['status'] == 'running'
    monitor._process_status(elegoo_frame(13))
    monitor._process_status(elegoo_frame(8, ticks=5))
    rows = {row['id']: row for row in case.rows()}
    assert rows[old]['status'] == 'cancelled' and rows[case.tracked[59]]['status'] == 'running'
    assert case.tracked[59] != old


@pytest.mark.parametrize('generic', ['prusalink'], indirect=True)
@pytest.mark.parametrize('undecided', ['BUSY', 'UNKNOWN'])
def test_prusalink_first_look_lasts_until_a_state_that_decides(generic, undecided):
    case = generic
    case.monitor()._process_status(prusalink_named_wire('PRINTING', timer=60))
    before = case.rows()
    age_row(case, before[0]['id'], 10 * 3600)
    monitor = case.monitor()
    monitor._process_status(prusalink_named_wire(undecided))
    monitor._process_status(prusalink_named_wire('PRINTING', timer=3600))
    assert [row['id'] for row in case.rows()] == [before[0]['id']]
    assert case.tracked[59] == before[0]['id']


@pytest.mark.parametrize('generic', ['prusalink'], indirect=True)
def test_prusalink_undecided_state_during_a_watched_start_is_not_a_first_look(generic):
    case = generic
    case.monitor()._process_status(prusalink_named_wire('PRINTING', timer=60))
    old = case.rows()[0]['id']
    age_row(case, old, 2 * 3600)
    monitor = case.monitor()
    monitor._process_status(prusalink_named_wire('IDLE'))
    monitor._process_status(prusalink_named_wire('BUSY'))
    monitor._process_status(prusalink_named_wire('PRINTING', timer=5))
    rows = {row['id']: row for row in case.rows()}
    assert rows[old]['status'] == 'cancelled' and case.tracked[59] != old


@pytest.mark.parametrize('generic', ['anycubic_lan'], indirect=True)
def test_anycubic_has_no_undecided_frame_before_its_first_look(generic):
    # Same check on Anycubic: an unrecognised project state stays OFFLINE, which the monitor
    # skips before the shared sink, so no frame can use up the first look.
    from modules.printers.adapters.anycubic_lan import DEFAULTS
    from modules.printers.parsing.anycubic_lan import parse_report
    topic = 'anycubic/anycubicCloud/v1/printer/public/20025/CN0000/info/report'
    case = generic
    case.monitor()._process_status(case.status('PRINTING', timer=60))
    before = case.rows()
    age_row(case, before[0]['id'], 10 * 3600)
    monitor = case.monitor()
    state = parse_report(topic, {'action': 'x', 'data': {'project': {'state': 'unrecognised', 'filename': 'part.gcode'}}}, {'internal_state': 'OFFLINE'})
    assert state['internal_state'] == 'OFFLINE'
    monitor._process_status(SimpleNamespace(**(DEFAULTS | state)))
    state = parse_report(topic, {'action': 'x', 'data': {'project': {'state': 'printing', 'filename': 'part.gcode', 'print_time': 60}}}, state)
    assert state['internal_state'] == 'PRINTING' and state['current_ticks'] == 3600
    monitor._process_status(SimpleNamespace(**(DEFAULTS | state)))
    assert [row['id'] for row in case.rows()] == [before[0]['id']]
    assert case.tracked[59] == before[0]['id']


def test_generic_pause_after_a_restart_still_reaches_the_attention_lane(generic, monkeypatch):
    # Two running rows silently drop pause alerts, so the duplicate is what must not happen.
    from modules.notifications import alert_dispatcher, job_faults
    case = generic
    staged, stage = [], alert_dispatcher.stage_alert
    monkeypatch.setattr(alert_dispatcher, 'stage_alert', lambda db, alert_type, *a, **kw: staged.append(alert_type) or stage(db, alert_type, *a, **kw))
    monkeypatch.setattr(alert_dispatcher, '_deliver_staged_alert', lambda db, plan: None)
    case.monitor()._process_status(case.status('PRINTING'))
    monitor = case.monitor()
    monitor._process_status(case.status('PRINTING'))
    monitor._process_status(case.status('PAUSED'))
    monitor._process_status(case.status('PAUSED'))
    assert case.seen.faults.count('paused') == 1
    job_faults._notify(59, 'paused', None)
    job_faults._notify(59, 'paused', None)  # The durable episode claim, for a pre-restart pause.
    assert len(staged) == 1


def test_generic_print_paused_across_a_restart_ends_on_its_own_row(generic):
    case = generic
    case.monitor()._process_status(case.status('PRINTING'))
    before = case.rows()
    monitor = case.monitor()
    monitor._process_status(case.status('PAUSED'))
    monitor._process_status(case.status('PRINTING'))
    assert case.rows() == before
    monitor._process_status(case.status('FINISHED'))
    assert [row['status'] for row in case.rows()] == ['completed']


@pytest.mark.parametrize('name', ['', 'Unknown'])
def test_generic_unnamed_print_does_not_resume_or_close_anything(generic, name):
    # Nothing identifies it: the duplicate is ugly, but a guess could end the wrong print.
    case = generic
    case.monitor()._process_status(case.status('PRINTING'))
    case.monitor()._process_status(case.status('PRINTING', filename=name))
    case.monitor()._process_status(case.status('PRINTING', filename=name))
    rows = case.rows()
    assert [row['filename'] for row in rows] == ['part.gcode', 'Unknown', 'Unknown']
    assert [row['status'] for row in rows] == ['running'] * 3


def test_generic_different_print_does_not_adopt_the_old_row(generic):
    from core.physical_print import CLOSED_UNKNOWN
    case = generic
    case.monitor()._process_status(case.status('PRINTING'))
    case.monitor()._process_status(case.status('PRINTING', filename='next.gcode'))
    old, new = case.rows()
    assert (old['status'], old['ended_at'], old['error_code']) == ('cancelled', None, CLOSED_UNKNOWN)
    assert (new['filename'], new['status']) == ('next.gcode', 'running')
    assert case.tracked[59] == new['id']
    assert case.seen.alerts == [] and case.seen.archive == []
    assert len(case.seen.started) == 2


@pytest.mark.parametrize('generic', ['prusalink'], indirect=True)
def test_generic_dispatched_attempt_keeps_the_receipt_rules(generic, monkeypatch):
    from modules.notifications import job_events
    case = generic
    names = []
    monkeypatch.setattr(case.dispatch, '_dispatch_prusalink', lambda jid, path, name, creds, **kw: (names.append(name) or True, 'Synthetic accepted'))
    assert case.dispatch.dispatch_job(59, 53)[0]
    identifier = job_events.job_started(59, names[0])
    assert identifier
    case.monitor()._process_status(case.status('PRINTING', filename=names[0]))
    assert [row['id'] for row in case.rows()] == [identifier]
    assert case.tracked[59] == identifier
    # An unrelated start leaves the unresolved attempt and its fence alone.
    case.monitor()._process_status(case.status('PRINTING', filename='manual.gcode'))
    rows = case.rows()
    assert [row['status'] for row in rows] == ['running', 'running'] and rows[0]['id'] == identifier
    with case.engine.connect() as conn:
        assert conn.execute(text('SELECT count(*) FROM printer_dispatch_claims')).scalar_one() == 1


def test_generic_education_row_is_never_resumed_or_closed(generic):
    case = generic
    with case.engine.begin() as conn:
        conn.execute(text("INSERT INTO print_jobs(id,printer_id,filename,job_name,status,started_at,scheduled_job_id) VALUES(700,59,'part.gcode','part.gcode','running',CURRENT_TIMESTAMP,53)"))
        conn.execute(text('UPDATE education_submissions SET job_id=53 WHERE id=(SELECT MIN(id) FROM education_submissions)'))
    case.monitor()._process_status(case.status('PRINTING'))
    rows = {row['id']: row for row in case.rows()}
    assert rows[700]['status'] == 'running' and len(rows) == 2
    assert case.tracked[59] != 700 and rows[case.tracked[59]]['status'] == 'running'


@pytest.mark.parametrize('name', [EDUCATION_NAME, DISPATCH_NAME], ids=['education', 'dispatch'])
def test_generic_unrelated_start_closes_a_reserved_row_with_no_claim(generic, name):
    case = generic
    with case.engine.begin() as conn:
        conn.execute(text("INSERT INTO print_jobs(id,printer_id,filename,job_name,status,started_at) VALUES(700,59,:name,:name,'running',CURRENT_TIMESTAMP)"), {'name': name})
    case.monitor()._process_status(case.status('PRINTING', filename='manual.gcode'))
    rows = {row['id']: row for row in case.rows()}
    assert_closed_unbound_reserved_row(rows[700]['status'], rows[700]['ended_at'], rows[700]['error_code'])
    assert case.tracked[59] != 700 and rows[case.tracked[59]]['status'] == 'running'
    assert case.seen.alerts == [] and case.seen.archive == []


def test_quarantined_row_ends_through_education_and_is_never_archived(generic, monkeypatch):
    # The real recovery sweep and archive writer: this row has no real end to report.
    from contextlib import contextmanager
    from core.db_utils import _ConnectionAdapter
    from core.registry import registry
    from modules.archives import archive
    case = generic
    ended = []
    policy = registry.get_provider('EducationPolicyProvider')
    terminal = policy.terminal_monitor_observation
    monkeypatch.setattr(policy, 'terminal_monitor_observation', lambda db, **kw: ended.append(kw) or terminal(db, **kw))
    with case.engine.begin() as conn:
        conn.execute(text("INSERT INTO print_jobs(id,printer_id,filename,job_name,status,started_at) VALUES(700,59,:name,:name,'running',:at)"), {'name': EDUCATION_NAME, 'at': ago(3 * 86400)})
    case.monitor()._process_status(case.status('PRINTING', filename='manual.gcode'))
    assert [(item['print_job_id'], item['terminal_status']) for item in ended] == [(700, 'cancelled')]

    @contextmanager
    def database():
        raw = case.engine.raw_connection()
        try:
            yield _ConnectionAdapter(raw, case.engine.dialect.name == 'postgresql')
        finally:
            raw.close()

    monkeypatch.setattr(archive, 'get_db', database)
    assert archive.recover_missing_archives()['attempted'] == 0
    with case.engine.connect() as conn:
        assert conn.execute(text('SELECT COUNT(*) FROM print_archives')).scalar_one() == 0


@pytest.mark.parametrize('generic', ['prusalink'], indirect=True)
def test_generic_unrelated_start_under_a_held_claim_leaves_an_unknown_row(generic, monkeypatch):
    case = generic
    monkeypatch.setattr(case.dispatch, '_dispatch_prusalink', lambda *a, **kw: (True, 'Synthetic accepted'))
    assert case.dispatch.dispatch_job(59, 53)[0]
    with case.engine.begin() as conn:
        conn.execute(text("INSERT INTO print_jobs(id,printer_id,filename,job_name,status,started_at) VALUES(700,59,'Unknown','Unknown','running',CURRENT_TIMESTAMP)"))
    case.monitor()._process_status(case.status('PRINTING', filename='manual.gcode'))
    rows = {row['id']: row for row in case.rows()}
    assert rows[700]['status'] == 'running' and case.tracked[59] != 700
    with case.engine.connect() as conn:
        assert conn.execute(text('SELECT count(*) FROM printer_dispatch_claims')).scalar_one() == 1


@pytest.mark.parametrize('generic', ['prusalink'], indirect=True)
def test_generic_reprint_of_a_retained_dispatch_file_cannot_block_the_printer(generic, monkeypatch):
    case = generic
    case.monitor()._process_status(case.status('PRINTING', filename=DISPATCH_NAME))
    reprint = case.tracked[59]
    case.monitor()._process_status(case.status('PRINTING', filename='manual.gcode'))
    following = case.tracked[59]
    assert following != reprint
    assert {row['id']: row['status'] for row in case.rows()} == {reprint: 'cancelled', following: 'running'}
    from modules.notifications import alert_dispatcher, job_faults
    staged, stage = [], alert_dispatcher.stage_alert
    monkeypatch.setattr(alert_dispatcher, 'stage_alert', lambda db, alert_type, *a, **kw: staged.append(alert_type) or stage(db, alert_type, *a, **kw))
    monkeypatch.setattr(alert_dispatcher, '_deliver_staged_alert', lambda db, plan: None)
    job_faults._notify(59, 'paused', None)  # Two running rows would drop this silently.
    assert len(staged) == 1
    monitor = case.monitor()
    monitor._process_status(case.status('FINISHED', filename='manual.gcode'))
    assert {row['id']: row['status'] for row in case.rows()}[following] == 'completed'
    monkeypatch.setattr(case.dispatch, '_dispatch_prusalink', lambda *a, **kw: (True, 'Synthetic accepted'))
    assert case.dispatch.dispatch_job(59, 53)[0]


def test_generic_education_named_start_closes_nothing(generic):
    case = generic
    case.monitor()._process_status(case.status('PRINTING'))
    case.monitor()._process_status(case.status('PRINTING', filename='odin-' + 'a' * 32 + '.gcode'))
    assert [row['status'] for row in case.rows()] == ['running', 'running']


@pytest.mark.parametrize('state,expected', [('FINISHED', 'completed'), ('stopped', 'cancelled')])
def test_generic_terminal_first_seen_closes_the_unique_row(generic, state, expected):
    case = generic
    case.monitor()._process_status(case.status('PRINTING'))
    case.monitor()._process_status(case.status(STOPPED[case.protocol] if state == 'stopped' else state))
    assert [row['status'] for row in case.rows()] == [expected]


def test_generic_idle_first_seen_changes_nothing(generic):
    # The open owner decision: the true outcome is unknown, so nothing is recorded yet.
    case = generic
    case.monitor()._process_status(case.status('IDLE', filename=''))
    assert case.rows() == []
    case.monitor()._process_status(case.status('PRINTING'))
    before = case.rows()
    case.monitor()._process_status(case.status('IDLE', filename=''))
    assert case.rows() == before


# ---------------------------------------------------------------- pause, all five monitors
def test_bambu_first_seen_pause_alerts_once_on_its_own_row(manual_bambu, monkeypatch):
    from modules.notifications import job_faults
    case = manual_bambu
    faults = []
    monkeypatch.setattr(job_faults, 'notify', lambda printer, kind, *a, **kw: faults.append(kind))
    monitor = case.monitor()
    for _ in range(2):
        packet(monitor, gcode_state='PAUSE', subtask_name=case.name)
    assert faults.count('paused') == 1
    assert monitor._current_job_id == case.identifier
    assert [row['status'] for row in manual_rows(case)[0]] == ['running']


def test_moonraker_first_seen_pause_alerts_once_on_its_own_row(moonraker, monkeypatch):
    from modules.notifications import job_faults
    case = moonraker
    identifier = case.row(started=60)
    faults = []
    monkeypatch.setattr(job_faults, 'notify', lambda printer, kind, *a, **kw: faults.append(kind))
    monitor = case.monitor()
    for _ in range(2):
        monitor._process_status(moonraker_status('paused', total=60))
    assert faults.count('paused') == 1
    assert monitor._current_job_db_id == identifier and running_row_count(case) == 1
