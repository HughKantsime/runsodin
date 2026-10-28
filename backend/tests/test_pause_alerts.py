"""Pause episodes, human copy and cancellation delivery; synthetic data only."""
import json
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import text

from backend.tests.test_alert_phone_delivery import alert_db, devices  # noqa: F401
from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401
from core.base import AlertType, AlertSeverity
from modules.notifications import job_faults


@pytest.fixture(autouse=True)
def synthetic_auth(monkeypatch):
    monkeypatch.setenv('JWT_SECRET_KEY', 'test-key-for-contract-tests-only')


def pause_item(**changes):
    return dict(printer=9, job=None, attempt=None, kind='paused',
                code='07007000_00020002', occurred=datetime(2026, 10, 28, tzinfo=timezone.utc),
                context={'filename': 'gear.gcode', 'before_printing': True}, **changes)


def test_pause_once_per_episode_across_transactions_and_reason_changes(alert_db, devices):
    from modules.notifications.alert_dispatcher import deliver_staged_alert
    plan = job_faults.stage(alert_db, pause_item())
    alert_db.commit()
    deliver_staged_alert(alert_db, plan)
    assert devices.send_push_result.call_count == 1
    assert plan.alert_type == AlertType.PRINT_PAUSED and plan.severity == AlertSeverity.CRITICAL
    assert 'gear paused before printing' in plan.title
    assert 'AMS' in plan.message and '07007000' not in plan.title
    assert devices.send_push_result.call_args.kwargs['category'] == 'PRINT_PAUSED'
    # A changed HMS code and newly linked physical identity do not start a new pause.
    repeated = {**pause_item(), 'attempt': 'physical', 'code': '03000D00_00020002'}
    assert job_faults.stage(alert_db, repeated) is None
    alert_db.commit()
    resumed = {**repeated, 'kind': 'resumed', 'occurred': repeated['occurred'] + timedelta(seconds=5)}
    assert job_faults.stage(alert_db, resumed) is None
    alert_db.commit()
    # Late durable timeline replay cannot undo the newer resume.
    assert job_faults.stage(alert_db, repeated) is None
    again = {**repeated, 'occurred': resumed['occurred'] + timedelta(seconds=1)}
    plan2 = job_faults.stage(alert_db, again)
    alert_db.commit()
    deliver_staged_alert(alert_db, plan2)
    assert devices.send_push_result.call_count == 2
    assert 'Motion/toolhead' in plan2.message
    assert job_faults.stage(alert_db, again) is None


def test_pause_rollback_does_not_claim_episode(alert_db):
    assert job_faults.stage(alert_db, pause_item()) is not None
    alert_db.rollback()
    assert job_faults.stage(alert_db, pause_item()) is not None


@pytest.mark.parametrize('scope', ['mode', 'job', 'reserved', 'unresolved'])
def test_pause_education_fail_closed(alert_db, monkeypatch, scope):
    item = pause_item()
    if scope == 'mode':
        alert_db.execute(text("INSERT INTO system_config(key,value) VALUES('education_mode','true')"))
    elif scope == 'job':
        item['job'] = 13
    elif scope == 'reserved':
        item['context']['filename'] = 'odin-' + 'a'*32 + '.3mf'
    else:
        from core import recipient_policy
        monkeypatch.setattr(recipient_policy, 'education_notification_scope', Mock(side_effect=RuntimeError('unresolved')))
        with pytest.raises(RuntimeError):
            job_faults.stage(alert_db, item)
        return
    assert job_faults.stage(alert_db, item) is None
    assert alert_db.execute(text('SELECT count(*) FROM alerts')).scalar_one() == 0


def test_fault_worker_delivers_mobile_once_without_a_linked_job(alert_db, devices, monkeypatch):
    from core import db
    monkeypatch.setattr(db, 'engine', alert_db.get_bind())
    job_faults._notify(9, 'paused', '07007000_00020002', context={'filename': 'gear.gcode'})
    job_faults._notify(9, 'paused', '03000D00_00020002')
    assert devices.send_push_result.call_count == 1


@pytest.mark.parametrize('previous', [None, 'PREPARE', 'RUNNING', 'PAUSE'])
def test_bambu_pause_transition_including_preprint(monkeypatch, previous):
    from modules.printers.monitors.mqtt_printer import PrinterMonitor
    from core import job_timeline
    monkeypatch.setattr(job_timeline, 'close_pause_episode', Mock())
    monitor = object.__new__(PrinterMonitor)
    monitor.printer_id = 9
    monitor.name = 'H2D'
    monitor._current_job_id = None
    monitor._state = {'hms': [{'attr': 0x07007000, 'code': 0x00020002}], 'layer_num': 0}
    def started():
        monitor._current_job_id = 42
    monitor._job_started = started
    calls = []
    def observed(*args, **kwargs):
        assert monitor._current_job_id == 42 or previous == 'RUNNING'
        calls.append((args, kwargs))
    monkeypatch.setattr(job_timeline, 'for_printer', observed)
    monitor._on_state_change(previous, 'PAUSE')
    assert len(calls) == (0 if previous == 'PAUSE' else 1)
    if calls:
        assert calls[0][0] == (9, 'paused')
        assert calls[0][1]['code'] == '07007000_00020002'
        assert calls[0][1]['context']['before_printing']


def test_preferences_backfill_partial_users_preserves_opt_out(alert_db):
    from core.alert_preferences import seed_attention_preferences
    alert_db.execute(text("INSERT INTO alert_preferences(user_id,alert_type,in_app,browser_push,email) VALUES(3,'PRINT_PAUSED',FALSE,FALSE,FALSE)"))
    seed_attention_preferences(alert_db)
    seed_attention_preferences(alert_db)
    rows = alert_db.execute(text("SELECT user_id,alert_type,in_app FROM alert_preferences WHERE LOWER(alert_type) IN ('print_paused','print_cancelled')")).all()
    assert len(rows) == len({(r[0], r[1].lower()) for r in rows})
    assert (3, 'PRINT_PAUSED', 0) in rows
    assert (5, 'print_paused', 1) in rows
    from modules.push.schemas import PushPreferences
    assert PushPreferences().categories['PRINT_PAUSED'] is True
    assert PushPreferences().categories['PRINT_CANCELLED'] is True


@pytest.mark.parametrize('existing', [False, True])
def test_get_preferences_returns_defaults_without_writes(alert_db, monkeypatch, existing):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy import event
    from core.db import get_db
    from core.dependencies import get_current_user
    from modules.notifications.routes.alerts import router

    alert_db.execute(text('DELETE FROM alert_preferences WHERE user_id=3'))
    if existing:
        alert_db.execute(text("INSERT INTO alert_preferences(user_id,alert_type,in_app,browser_push,email,threshold_value) "
                              "VALUES(3,'print_paused',FALSE,FALSE,TRUE,25),(3,'printer_error',TRUE,FALSE,FALSE,NULL)"))
    alert_db.commit()
    before = alert_db.execute(text('SELECT * FROM alert_preferences ORDER BY id')).all()
    commit = Mock(side_effect=AssertionError('GET must not commit')) if existing else Mock(wraps=alert_db.commit)
    monkeypatch.setattr(alert_db, 'commit', commit)
    statements = []
    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    app = FastAPI()
    app.include_router(router, prefix='/api')
    app.dependency_overrides[get_db] = lambda: alert_db
    app.dependency_overrides[get_current_user] = lambda: {'id': 3, 'role': 'admin'}
    engine = alert_db.get_bind()
    event.listen(engine, 'before_cursor_execute', capture)
    try:
        with TestClient(app) as client:
            response = client.get('/api/alert-preferences')
            assert response.status_code == 200, response.text
            assert client.get('/api/alert-preferences').json() == response.json()
    finally:
        event.remove(engine, 'before_cursor_execute', capture)
    rows = response.json()
    assert all(row['user_id'] == 3 for row in rows)
    by_type = {row['alert_type']: row for row in rows}
    if not existing:
        from modules.notifications.alert_dispatcher import DEFAULT_PREFERENCES
        defaults = {pref['alert_type'].value: pref for pref in DEFAULT_PREFERENCES}
        assert len(rows) == len(by_type) == len(defaults)
        assert set(by_type) == set(defaults)
        for kind, row in by_type.items():
            assert row['id'] > 0
            assert {key: row[key] for key in defaults[kind] if key != 'alert_type'} == {
                key: value for key, value in defaults[kind].items() if key != 'alert_type'}
        commit.assert_called_once()
        assert alert_db.execute(text('SELECT count(*) FROM alert_preferences WHERE user_id=3')).scalar_one() == len(defaults)
        return
    assert statements and all(sql.lstrip().upper().startswith('SELECT') for sql in statements)
    commit.assert_not_called()
    assert alert_db.execute(text('SELECT * FROM alert_preferences ORDER BY id')).all() == before
    assert len(rows) == len(by_type) == 3
    assert by_type['print_cancelled'] == dict(id=-2, user_id=3, alert_type='print_cancelled',
                                            in_app=True, browser_push=True, email=False, threshold_value=None)
    paused = by_type['print_paused']
    assert paused['in_app'] is (not existing)
    assert paused['browser_push'] is (not existing)
    assert paused['email'] is existing
    assert paused['threshold_value'] == (25 if existing else None)
    assert paused['id'] > 0


@pytest.mark.parametrize('item_name,project_name,original,expected', [
    ('Human Gear', 'Display Name', 'gear.3mf', 'Human Gear'),
    ('odin-dispatch-deadbeef.3mf', 'Display Name', 'gear.3mf', 'Display Name'),
    ('odin-dispatch-deadbeef', '', 'nice-gear.gcode.3mf', 'nice-gear'),
    ('odin-dispatch-deadbeef', '', 'odin-dispatch-another.3mf', 'print'),
])
def test_human_job_name_precedence(alert_db, item_name, project_name, original, expected):
    from modules.notifications.alert_copy import humanize
    alert_db.execute(text("UPDATE printers SET nickname='Herbie' WHERE id=9"))
    alert_db.execute(text("UPDATE print_files SET project_name=:project,original_filename=:original,filename='odin-dispatch-hidden.3mf' WHERE id=11"), {'project': project_name, 'original': original})
    alert_db.execute(text("INSERT INTO jobs(id,item_name,status,print_file_id,submitted_by,charged_to_org_id) VALUES(99,:name,'pending',11,3,1)"), {'name': item_name})
    title, body, meta = humanize(alert_db, 'print_failed', 'Print Failed: odin-dispatch-hidden',
                                '', 9, 99, {'hms_code': '07007000_00020002'})
    assert title == f'Herbie: {expected} failed'
    assert 'odin-dispatch-' not in title + body
    assert 'Check' in body and 'AMS' in body
    assert 'Details:' in body
    assert meta['hms_code'] == '07007000_00020002'


@pytest.mark.parametrize('kind', ['print_failed', 'printer_error'])
@pytest.mark.parametrize('opaque', ['', 'odin-dispatch-01234567-89ab-cdef-0123-456789abcdef.gcode.3mf '])
def test_human_failure_copy_retains_non_code_explanation(alert_db, kind, opaque):
    from modules.notifications.alert_copy import humanize
    title, body, _ = humanize(alert_db, kind, 'Failure',
                             f'{opaque}Filament ran out; reload slot 2. [raw-code]', 9, None)
    assert 'Filament ran out; reload slot 2.' in body
    assert 'Check the printer' in body
    assert 'odin-dispatch-' not in title + body
    assert '[raw-code]' not in body


@pytest.mark.parametrize('skip', ['education_scope', 'org_mismatch', 'preference_off', 'quiet_hours',
                                  'category_off', 'not_configured', 'no_devices'])
def test_every_fanout_skip_has_info_proof(alert_db, devices, caplog, skip):
    from modules.push import fanout
    from modules.push.apns import PushResult
    devices.send_push_result.return_value = PushResult('not_configured')
    plan = SimpleNamespace(alert_type=AlertType.PRINTER_ERROR, severity=AlertSeverity.WARNING,
                           printer_id=9, job_id=None, spool_id=None, org_id=1, suppress_external=False,
                           in_app_users=(3,), title='Test', message='Check printer')
    if skip == 'education_scope':
        alert_db.execute(text("INSERT INTO system_config(key,value) VALUES('education_mode','true')"))
    elif skip == 'org_mismatch':
        plan.org_id = 2
    elif skip == 'preference_off':
        alert_db.execute(text("UPDATE alert_preferences SET in_app=FALSE WHERE alert_type='printer_error'"))
    elif skip == 'no_devices':
        alert_db.execute(text('DELETE FROM push_devices'))
    elif skip in {'quiet_hours', 'category_off'}:
        prefs = {'quiet_hours_enabled': True, 'quiet_hours_start': '00:00', 'quiet_hours_end': '23:59'} if skip == 'quiet_hours' else {'categories': {'HMS_ERROR': False}}
        alert_db.execute(text('UPDATE push_devices SET preferences_json=:prefs'), {'prefs': json.dumps(prefs)})
    alert_db.commit()
    with caplog.at_level(logging.INFO):
        fanout.deliver_alert(alert_db, plan)
    assert 'reason=' + skip in caplog.text
    assert 'synthetic-device-token' not in caplog.text
    if skip != 'not_configured':
        devices.send_push_result.assert_not_called()


@pytest.mark.parametrize('brand', ['moonraker', 'elegoo', 'prusalink'])
def test_other_monitors_pause_resume_pause_and_repeated_telemetry(monkeypatch, brand):
    import time
    from core import job_timeline
    monkeypatch.setattr(job_timeline, 'close_pause_episode', Mock())
    from modules.notifications import event_dispatcher
    calls = []
    monkeypatch.setattr(job_timeline, 'for_printer', lambda *a, **kw: calls.append((a, kw)))
    monkeypatch.setattr(event_dispatcher, 'on_print_start', Mock())
    if brand == 'moonraker':
        from modules.printers.monitors.moonraker_monitor import MoonrakerMonitor
        from modules.printers.adapters.moonraker import MoonrakerStatus
        monitor = MoonrakerMonitor(9, 'Klipper', '127.0.0.1')
        paused = MoonrakerStatus(internal_state='PAUSE', filename='gear.gcode', error_message='Filament sensor needs attention')
        running = MoonrakerStatus(internal_state='RUNNING', filename='gear.gcode')
    elif brand == 'elegoo':
        from modules.printers.monitors.elegoo_monitor import ElegooMonitorThread
        from modules.printers.adapters.elegoo import ElegooStatus
        monitor = ElegooMonitorThread(9, 'Elegoo', '127.0.0.1')
        paused = ElegooStatus(internal_state='PAUSED', filename='gear.gcode')
        running = ElegooStatus(internal_state='PRINTING', filename='gear.gcode')
    else:
        from modules.printers.monitors.prusalink_monitor import PrusaLinkMonitorThread
        from modules.printers.adapters.prusalink import PrusaLinkStatus, PrusaLinkState
        monitor = PrusaLinkMonitorThread(9, 'Prusa', '127.0.0.1')
        paused = PrusaLinkStatus(state=PrusaLinkState.PAUSED, internal_state='PAUSED', filename='gear.gcode')
        running = PrusaLinkStatus(state=PrusaLinkState.PRINTING, internal_state='PRINTING', filename='gear.gcode')
    monitor._last_heartbeat = time.time()
    for status in (paused, paused, running, paused, paused):
        monitor._process_status_locked(status)
    assert [a[1] for a, kw in calls] == ['paused', 'resumed', 'paused']
    assert calls[0][1]['context']['filename'] == 'gear.gcode'
    if brand == 'moonraker':
        assert calls[0][1]['context']['pause_reason'] == 'Filament sensor needs attention'


@pytest.mark.parametrize('error,outcome', [(0x0300400C, 'cancelled'), (0x0500400E, 'cancelled'), (0x05008024, 'failed')])
def test_bambu_explicit_cancel_code_is_not_failure(monkeypatch, error, outcome):
    from modules.printers.monitors.mqtt_printer import PrinterMonitor
    from core import job_timeline
    monkeypatch.setattr(job_timeline, 'close_pause_episode', Mock())
    monkeypatch.setattr(job_timeline, 'for_printer', Mock())
    monitor = object.__new__(PrinterMonitor)
    monitor.printer_id = 9
    monitor.name = 'Herbie'
    monitor._current_job_id = 42
    monitor._state = {'print_error': error}
    monitor._job_ended = Mock()
    monitor._on_state_change('RUNNING', 'FAILED')
    monitor._job_ended.assert_called_once_with(outcome)


@pytest.mark.parametrize('bulk', [False, True])
def test_pending_job_cancel_http_stages_no_alert_or_push(alert_db, devices, monkeypatch, bulk):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from core.db import get_db
    from core.dependencies import get_current_user
    from modules.jobs.routes import jobs_agent, jobs_crud
    from core.registry import registry
    from modules.organizations.services import EducationPolicyService
    monkeypatch.setitem(registry._providers, 'EducationPolicyProvider', EducationPolicyService())
    alert_db.execute(text("INSERT INTO jobs(id,item_name,status,printer_id,submitted_by,charged_to_org_id,is_locked) VALUES(99,'Gear','pending',9,3,1,FALSE)"))
    alert_db.commit()
    app = FastAPI()
    from core.app_errors import register_error_handlers
    register_error_handlers(app)
    app.include_router(jobs_agent.router, prefix='/api')
    app.include_router(jobs_crud.router, prefix='/api')
    app.dependency_overrides[get_db] = lambda: alert_db
    app.dependency_overrides[get_current_user] = lambda: {'id': 3, 'role': 'admin', 'group_id': 1}
    path = '/api/jobs/bulk-update' if bulk else '/api/jobs/99/cancel'
    kwargs = {'json': {'job_ids': [99, 99], 'action': 'cancel'}} if bulk else {}
    with TestClient(app) as client:
        response = client.post(path, **kwargs)
        assert response.status_code == 200, response.text
        assert alert_db.execute(text('SELECT status FROM jobs WHERE id=99')).scalar_one() == 'cancelled'
        assert alert_db.execute(text('SELECT count(*) FROM alerts')).scalar_one() == 0
        assert alert_db.execute(text('SELECT count(*) FROM alert_outbox')).scalar_one() == 0
        devices.send_push_result.assert_not_called()
        second = client.post(path, **kwargs)
        assert second.status_code == (200 if bulk else 400)
        assert alert_db.execute(text('SELECT count(*) FROM alerts')).scalar_one() == 0
        assert alert_db.execute(text('SELECT count(*) FROM alert_outbox')).scalar_one() == 0
        devices.send_push_result.assert_not_called()


def test_pause_restart_observes_resume_before_next_episode(alert_db, devices, monkeypatch):
    from core import db
    monkeypatch.setattr(db, 'engine', alert_db.get_bind())
    job_faults._notify(9, 'paused', None)
    # A fresh process sees RUNNING before it later sees another PAUSE.
    job_faults._notify(9, 'resumed', None)
    job_faults._notify(9, 'paused', None)
    assert devices.send_push_result.call_count == 2


def test_new_unlinked_pause_does_not_inherit_previous_print_identity(alert_db):
    first = {**pause_item(), 'attempt': 'old-print'}
    assert job_faults.stage(alert_db, first) is not None
    assert job_faults.stage(alert_db, {**first, 'kind': 'completed',
                                    'occurred': first['occurred'] + timedelta(seconds=1)}) is None
    second = {**pause_item(), 'occurred': first['occurred'] + timedelta(seconds=2)}
    assert job_faults.stage(alert_db, second) is not None
    assert job_faults.stage(alert_db, {**second, 'attempt': 'new-print'}) is None


def test_numeric_print_error_keeps_decoded_slot_reason():
    from modules.notifications.alert_copy import reason_for
    reason = reason_for(0x12028005)
    assert 'slot 2' in reason and 'feed' in reason
    assert 'Camera' not in reason


def test_summary_and_alert_api_job_names_hide_dispatch_aliases(alert_db):
    import asyncio
    from modules.notifications.alert_dispatcher import stage_alert
    from modules.notifications.routes.alerts import list_alerts
    alias = 'odin-dispatch-hidden.3mf'
    alert_db.execute(text("INSERT INTO jobs(id,item_name,status,printer_id,print_file_id,submitted_by,charged_to_org_id) VALUES(99,:name,'pending',9,11,3,1)"), {'name': alias})
    stage_alert(alert_db, AlertType.PRINT_PAUSED, AlertSeverity.CRITICAL, 'Paused',
                printer_id=9, job_id=99, metadata={'job_name': alias,
                    'summary': {'job_name': alias, 'text': alias + ' paused'}})
    alert_db.commit()
    alerts = asyncio.run(list_alerts(current_user={'id': 3}, db=alert_db))
    assert len(alerts) == 1
    assert alerts[0].job_name == 'Class Gear'
    assert alerts[0].metadata_json['job_name'] == 'Class Gear'
    assert alerts[0].metadata_json['summary']['job_name'] == 'Class Gear'
    assert alias not in alerts[0].title + alerts[0].metadata_json['summary']['text']
