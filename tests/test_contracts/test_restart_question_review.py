"""Regression scenarios from the independent restart-question review."""
import json
import time
from contextlib import contextmanager
from unittest.mock import Mock

import pytest
from sqlalchemy import text
from tests.test_contracts.test_restart_ask_operator import case, packet, start
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_economic_creator_wiring import creators


@pytest.mark.parametrize('change', ['closed', 'linked', 'unlinked', 'reassigned', 'claim_removed', 'claim_replaced', 'job_owner_changed'])
def test_changed_authority_retires_once_and_resumes(case, change):
    if change in ('unlinked', 'reassigned', 'job_owner_changed'):
        with case.engine.begin() as db:
            db.execute(text("INSERT INTO jobs(id,item_name,printer_id,charged_to_org_id,status) VALUES(239,'Earlier',9,1,'printing')"))
            db.execute(text('UPDATE print_jobs SET scheduled_job_id=239 WHERE id=241'))
    monitor = start(case)
    with case.engine.begin() as db:
        db.execute(text({
            'closed': "UPDATE print_jobs SET status='cancelled' WHERE id=241",
            'linked': 'UPDATE print_jobs SET scheduled_job_id=238 WHERE id=241',
            'unlinked': 'UPDATE print_jobs SET scheduled_job_id=NULL WHERE id=241',
            'reassigned': 'UPDATE jobs SET printer_id=NULL WHERE id=239',
            'job_owner_changed': 'UPDATE jobs SET charged_to_org_id=2 WHERE id=239',
            'claim_removed': 'DELETE FROM printer_dispatch_claims',
            'claim_replaced': "UPDATE printer_dispatch_claims SET token='eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee'",
        }[change]))
    packet(monitor, gcode_state='FINISH')
    if change in ('closed', 'claim_removed', 'unlinked', 'claim_replaced'):
        assert case.question() is None
    else:
        assert case.question()['state'] in ('pending', 'investigation')
    with case.engine.connect() as db:
        assert db.execute(text("SELECT count(*) FROM alerts WHERE is_dismissed IS TRUE AND title='Confirm the print after restart'")).scalar_one() == (6 if change in ('unlinked', 'claim_replaced') else 3)
        notices = db.execute(text("SELECT message FROM alerts WHERE title='Print confirmation closed'")).scalars().all()
        assert len(notices) == (6 if change in ('unlinked', 'claim_replaced') else 3)
        assert sum('changed' in message for message in notices) == 3
    for _ in range(5):
        packet(monitor, gcode_state='RUNNING', subtask_name='next-print')
    assert monitor._current_job_id is not None
    with case.engine.connect() as db:
        assert db.execute(text("SELECT count(*) FROM alerts WHERE title='Print confirmation closed'")).scalar_one() == (3 if change in ('closed', 'claim_removed') else 6)


def trace_connections(case, monkeypatch):
    from modules.printers.monitors import mqtt_printer
    calls = []
    @contextmanager
    def connect():
        calls.append('CONNECT')
        with case.connect() as conn:
            # The monitor adapter deliberately exposes only a small DBAPI surface.
            cursor = conn.cursor
            class Traced:
                def __getattr__(self, name): return getattr(conn, name)
                def execute(self, sql, args=()):
                    calls.append(sql)
                    return conn.execute(sql, args)
                def cursor(self):
                    original = cursor()
                    class Cursor:
                        def __getattr__(self, name): return getattr(original, name)
                        def execute(self, sql, args=()):
                            calls.append(sql)
                            original.execute(sql, args)
                            return self
                    return Cursor()
            yield Traced()
    monkeypatch.setattr(mqtt_printer, 'get_db', connect)
    return calls


def test_no_claim_idle_packets_add_zero_database_work(case, monkeypatch):
    with case.engine.begin() as db:
        db.execute(text('DELETE FROM printer_dispatch_claims'))
    monitor = case.monitor()
    monitor._last_gcode_state = 'IDLE'
    packet(monitor, gcode_state='IDLE')  # baseline live-evidence publisher primes once
    calls = trace_connections(case, monkeypatch)
    for _ in range(10): packet(monitor, gcode_state='IDLE')
    assert calls == []


def test_no_claim_recovery_backoff_does_not_change_adoption_or_finish(case):
    with case.engine.begin() as db:
        db.execute(text('DELETE FROM printer_dispatch_claims'))
    monitor = case.monitor()
    monitor._recovery_retry_at = time.time() + 300
    packet(monitor, gcode_state='RUNNING', subtask_name='bracket')
    assert monitor._current_job_id == 241
    packet(monitor, gcode_state='FINISH', mc_percent=100)
    assert case.rows()[0]['status'] == 'completed'


@pytest.mark.parametrize('first_name', [None, 'bracket'])
@pytest.mark.parametrize('state', ['RUNNING', 'FINISH'])
def test_different_name_withdraws_without_ending_old_print(case, first_name, state):
    monitor = start(case, first_name)
    before = case.rows()[0]
    packet(monitor, gcode_state=state, subtask_name='other-print', mc_percent=100, layer_num=50)
    assert case.question() is None
    assert case.rows()[0] == before
    if state == 'RUNNING': assert len(case.rows()) == 2


def test_pending_hms_display_alerts_and_pause_effects_continue(case, monkeypatch):
    from modules.notifications import error_handling
    from core import job_timeline
    monkeypatch.setattr(error_handling, 'get_db', case.connect)
    effects = Mock()
    alerts = Mock()
    monkeypatch.setattr(job_timeline, 'for_printer', effects)
    monkeypatch.setattr(error_handling, 'record_error', alerts)
    monitor = start(case)
    before = case.rows()
    packet(monitor, gcode_state='PAUSE', hms=[{'attr': 0x07000000, 'code': 0x03008012}], stg_cur=6)
    with case.engine.connect() as db:
        assert json.loads(db.execute(text('SELECT hms_errors FROM printers WHERE id=9')).scalar_one())
    assert any(call.args[1] == 'paused' for call in effects.call_args_list)
    assert any(call.args[1] == 'runout' for call in effects.call_args_list)
    assert alerts.called
    packet(monitor, gcode_state='RUNNING', hms=[])
    assert any(call.args[1] == 'resumed' for call in effects.call_args_list)
    with case.engine.connect() as db:
        assert json.loads(db.execute(text('SELECT hms_errors FROM printers WHERE id=9')).scalar_one()) == []
    assert case.rows() == before


def test_finished_question_deleted_and_no_later_interception(case, monkeypatch):
    monitor = start(case)
    packet(monitor, gcode_state='FINISH')
    assert case.question() is None
    calls = trace_connections(case, monkeypatch)
    for _ in range(10): packet(monitor, gcode_state='FINISH')
    assert calls == []
    recovered = Mock(return_value=None)
    monkeypatch.setattr(monitor, '_recover_rows', recovered)
    monitor._recovery_pending = True
    packet(monitor, gcode_state='FINISH')
    recovered.assert_called_once()


def test_claim_without_candidate_never_opens_packet_write_transaction(case, monkeypatch):
    with case.engine.begin() as db:
        db.execute(text('DELETE FROM print_jobs'))
    monitor = case.monitor()
    monitor._last_gcode_state = 'IDLE'
    packet(monitor, gcode_state='IDLE')  # baseline live-evidence publisher primes once
    calls = trace_connections(case, monkeypatch)
    for _ in range(10): packet(monitor, gcode_state='IDLE')
    assert calls == []


def test_multiple_candidates_alert_once(case):
    with case.engine.begin() as db:
        db.execute(text("INSERT INTO print_jobs(id,printer_id,job_name,status,started_at) SELECT 242,9,job_name,status,started_at FROM print_jobs WHERE id=241"))
    monitor = start(case)
    for _ in range(5):
        monitor._recovery_retry_at = 0
        packet(monitor, gcode_state='RUNNING', subtask_name='bracket')
    with case.engine.connect() as db:
        messages = db.execute(text("SELECT message FROM alerts WHERE title='Print records need investigation'")).scalars().all()
        assert len(messages) == 3
        assert all('241, 242' in message and 'close records' in message for message in messages)
    assert len(case.rows()) == 2


@pytest.mark.parametrize('state,age', [('OFFLINE', 1), ('RUNNING', 86400)])
def test_no_answer_requires_fresh_running_report(case, state, age):
    from core.restart_questions import save
    monitor = start(case)
    before = case.rows()
    with case.engine.begin() as db:
        save(db, 'printer.live_state.9', {'state':state, 'at':time.time()-age})
    response = case.post('new')
    assert response.status_code == 409
    assert case.rows() == before


def test_unanswered_end_creates_archive_immediately(case):
    monitor = start(case)
    packet(monitor, gcode_state='FINISH')
    with case.engine.connect() as db:
        assert db.execute(text('SELECT accounting_reason FROM print_archives WHERE print_job_id=241')).scalar_one() == 'ended_while_unconfirmed'


def test_new_midprint_archive_discloses_unverified_totals(case):
    from core.monitor_observations import persist
    with case.engine.begin() as db: persist(db, 9, 'RUNNING')
    monitor = start(case)
    assert case.post('new').status_code == 200
    packet(monitor, gcode_state='FINISH', mc_percent=100)
    from modules.archives.archive import create_print_archive
    identifier = case.rows()[-1]['id']
    create_print_archive(identifier, 9, True)
    with case.engine.connect() as db:
        row = db.execute(text('SELECT actual_duration_seconds,filament_used_grams,notes FROM print_archives WHERE print_job_id=:id'), {'id':identifier}).one()
        assert row[0] is None and row[1] is None and 'first seen' in row[2].lower()


def test_restart_bridge_is_classified():
    from ops.database_parity.sqlite_compatibility_inventory import CLASSIFICATIONS
    assert 'backend/modules/printers/monitors/restart_question.py' in CLASSIFICATIONS

@pytest.mark.parametrize('retry', [0, 300])
def test_no_claim_packet_outcomes_match_57329df9_byte_for_byte(case, monkeypatch, retry):
    """Execute the base handlers against the same synthetic DB snapshot and clock."""
    import ast
    import sqlite3
    import subprocess
    from datetime import datetime, timezone
    from types import MethodType
    from modules.printers.monitors import mqtt_printer, mqtt_job_lifecycle
    from modules.notifications import event_dispatcher
    from modules.archives import archive
    from core import job_timeline

    fixed = datetime.now(timezone.utc)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None): return fixed if tz else fixed.replace(tzinfo=None)
    monkeypatch.setattr(mqtt_job_lifecycle, 'datetime', Clock)
    monkeypatch.setattr(job_timeline, 'for_printer', lambda *a, **kw: None)
    monkeypatch.setattr(job_timeline, 'close_pause_episode', lambda *a: None)
    monkeypatch.setattr(archive, 'create_print_archive', lambda *a, **kw: None)
    for name in ('job_completed', 'job_failed', 'job_cancelled'):
        if hasattr(event_dispatcher, name): monkeypatch.setattr(event_dispatcher, name, lambda *a, **kw: None)
    with case.engine.begin() as db:
        db.execute(text('DELETE FROM printer_dispatch_claims'))
        db.execute(text('UPDATE print_jobs SET scheduled_job_id=238 WHERE id=241'))
        db.execute(text("UPDATE jobs SET status='printing' WHERE id=238"))
    snapshot = sqlite3.connect(':memory:')
    with case.engine.connect() as db:
        db.connection.driver_connection.backup(snapshot)
    base_source = subprocess.check_output(['git', 'show', '57329df9:backend/modules/printers/monitors/mqtt_printer.py'], text=True)
    base_status = subprocess.check_output(['git', 'show', '57329df9:backend/modules/printers/monitors/mqtt_status.py'], text=True)
    namespace = dict(vars(mqtt_printer))
    methods = next(n for n in ast.parse(base_source).body if isinstance(n, ast.ClassDef) and n.name == 'PrinterMonitor')
    for method in methods.body:
        if isinstance(method, ast.FunctionDef) and method.name in ('_job_started', '_on_state_change', '_reconcile_running_rows'):
            exec(compile(ast.Module(body=[method], type_ignores=[]), '<base monitor>', 'exec'), namespace)
    status_namespace = {}
    exec(compile(base_status, '<base status>', 'exec'), status_namespace)
    results = []
    traces = []
    for baseline in (True, False):
        if not baseline:
            with case.engine.connect() as db: snapshot.backup(db.connection.driver_connection)
        monitor = case.monitor()
        monitor._recovery_retry_at = time.time() + retry
        if baseline:
            for name in ('_job_started', '_on_state_change', '_reconcile_running_rows'):
                setattr(monitor, name, MethodType(namespace[name], monitor))
        from types import SimpleNamespace
        calls = trace_connections(case, monkeypatch)
        namespace['get_db'] = mqtt_printer.get_db
        def deliver(**fields):
            status = SimpleNamespace(raw_data={'print': fields})
            if baseline: status_namespace['handle_status'](monitor, status, None, mqtt_printer)
            else: monitor._on_status(status)
        deliver(gcode_state='RUNNING', subtask_name='bracket', mc_percent=37, layer_num=17)
        deliver(gcode_state='PAUSE')
        deliver(gcode_state='RUNNING', mc_percent=44)
        deliver(gcode_state='FINISH', mc_percent=100)
        results.append(json.dumps({'rows':case.rows(), 'tracked':monitor._current_job_id,
                                   'state':monitor._state, 'last':monitor._last_gcode_state}, sort_keys=True, default=str))
        traces.append(calls[:])
    snapshot.close()
    assert results[0] == results[1]
    assert traces[0] == traces[1]


def test_recovery_retain_cannot_bind_receipt_to_pending_row(case):
    import importlib
    from core.restart_questions import save
    with case.engine.begin() as db:
        importlib.import_module('core.schema.migrations.038_dispatch_recovery').apply(db)
        db.execute(text("INSERT INTO dispatch_attempt_receipts(claim_token,printer_id,job_id,configuration_key,prepared_power_key,observed_filename_key) VALUES(:token,9,238,:config,:power,:filename)"), {'token':'d'*32,'config':'b'*64,'power':'e'*64,'filename':'f'*64})
    start(case)
    before = case.rows()
    inspection = case.client.get('/api/printers/9/dispatch-recovery', headers=case.auth()).json()
    response = case.client.post('/api/printers/9/dispatch-recovery/reconcile-retain', headers=case.auth(), json={
        'attempt_handle':inspection['attempt_handle'], 'snapshot_handle':inspection['snapshot_handle'],
        'reason_category':'investigation', 'education_action':'none'})
    assert response.status_code == 200, response.text
    assert case.rows() == before and case.question()['state'] == 'pending'
    with case.engine.connect() as db:
        assert db.execute(text('SELECT observed_print_job_id FROM dispatch_attempt_receipts')).scalar_one() is None


def test_multiple_candidate_investigation_does_not_block_a_different_print(case):
    with case.engine.begin() as db:
        db.execute(text("INSERT INTO print_jobs(id,printer_id,job_name,status,started_at) SELECT 242,9,job_name,status,started_at FROM print_jobs WHERE id=241"))
    monitor = start(case)
    before = case.rows()
    packet(monitor, gcode_state='RUNNING', subtask_name='other-print')
    assert case.question() is None
    assert case.rows()[:2] == before
    assert len(case.rows()) == 3


def test_retirement_storage_failure_never_inserts_and_retries_atomically(case, monkeypatch):
    from modules.notifications import restart_alerts
    monitor = start(case)
    with case.engine.begin() as db:
        db.execute(text("UPDATE print_jobs SET status='cancelled' WHERE id=241"))
    before = case.rows()
    stage = restart_alerts.stage
    def unavailable(*args, **kwargs): raise RuntimeError('Synthetic storage failure')
    monkeypatch.setattr(restart_alerts, 'stage', unavailable)
    for _ in range(3): packet(monitor, gcode_state='RUNNING', subtask_name='next-print')
    assert case.rows() == before and case.question()['state'] == 'pending'
    with case.engine.connect() as db:
        assert db.execute(text('SELECT count(*) FROM alerts WHERE is_dismissed IS FALSE')).scalar_one() == 3
    monkeypatch.setattr(restart_alerts, 'stage', stage)
    monitor._restart_retry_at = 0
    packet(monitor, gcode_state='RUNNING', subtask_name='next-print')
    assert case.question() is None and len(case.rows()) == 2


@pytest.mark.parametrize('decision', [None, 'new'])
def test_print_history_does_not_present_unverified_totals_as_measured(case, decision):
    from modules.jobs.routes.presets import router
    case.client.app.include_router(router, prefix='/api')
    monitor = start(case)
    if decision: assert case.post(decision).status_code == 200
    packet(monitor, gcode_state='FINISH')
    identifier = case.rows()[-1]['id']
    response = case.client.get(f'/api/print-jobs/{identifier}', headers=case.auth())
    assert response.status_code == 200, response.text
    row = response.json()
    assert row['duration_minutes'] is None and row['print_duration_seconds'] is None
    assert row['filament_used_g'] is None
    assert row['duration_verified'] is False and row['usage_verified'] is False
    assert 'unverified' in row['recording_note']
