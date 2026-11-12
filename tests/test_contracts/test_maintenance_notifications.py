"""Maintenance notifications, back-dated logs, implicit default task, failure rate."""
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_maintenance_behavior import maintenance_client

STATUS = '/api/v1/maintenance/status'
LOGS = '/api/v1/maintenance/logs'


# ---------------------------------------------------------------- helpers

@pytest.fixture
def checker(review_db, tmp_path, monkeypatch):
    """Runs one hourly check against the test database; delivery is recorded, not sent."""
    from modules.notifications import alert_dispatcher
    from modules.system import maintenance_alerts
    monkeypatch.setattr(maintenance_alerts, 'LOCK_PATH', tmp_path / 'maintenance.lock')
    delivered = []
    monkeypatch.setattr(alert_dispatcher, 'deliver_staged_alert', lambda db, plan: delivered.append(plan))
    monkeypatch.setattr(alert_dispatcher, 'should_suppress_notification', lambda org_id=None, db=None: False)

    def run():
        return maintenance_alerts.check_maintenance(session_factory=lambda: Session(review_db.get_bind()))
    run.delivered = delivered
    return run


def _hours(review_db, hours, printer_id=9):
    review_db.execute(text('UPDATE printers SET total_print_hours=:h WHERE id=:p'), {'h': hours, 'p': printer_id})
    review_db.commit()


def _alerts(review_db, printer_id=9):
    review_db.commit()
    rows = review_db.execute(text('SELECT user_id,severity,message,metadata_json FROM alerts WHERE printer_id=:p ORDER BY id'),
                             {'p': printer_id}).all()
    out = []
    for row in rows:
        meta = json.loads(row.metadata_json) if isinstance(row.metadata_json, str) else row.metadata_json
        out.append({'user_id': row.user_id, 'severity': str(row.severity).lower(), 'message': row.message, 'meta': meta or {}})
    return out


def _state(review_db):
    review_db.commit()
    value = review_db.execute(text("SELECT value FROM system_config WHERE key='maintenance_alert_state'")).scalar()
    return json.loads(value) if isinstance(value, str) else value


def _printer(client, printer_id=9):
    return next(p for p in client.get(STATUS).json() if p['printer_id'] == printer_id)


def _task(client, task_id, printer_id=9):
    return next(t for t in _printer(client, printer_id)['tasks'] if t['task_id'] == task_id)


# ---------------------------------------------------------------- shared status function

def _legacy_status(db, user):
    """The route body as it was before the extraction, kept as the parity reference."""
    from modules.printers.models import Printer
    from modules.system import routes_maintenance
    from modules.system.models import MaintenanceLog, MaintenanceTask
    printers = routes_maintenance._visible_printer_query(db.query(Printer), user).filter(Printer.is_active.is_(True)).order_by(Printer.name).all()
    tasks = db.query(MaintenanceTask).filter(MaintenanceTask.is_active.is_(True)).all()
    hours_map = {p.id: float(p.total_print_hours or 0) for p in printers}
    all_logs = db.query(MaintenanceLog).filter(MaintenanceLog.printer_id.in_([p.id for p in printers])).order_by(
        MaintenanceLog.performed_at.desc(), MaintenanceLog.id.desc()).all()
    log_map = {}
    for mlog in all_logs:
        key = (mlog.printer_id, mlog.task_id)
        if key not in log_map or (mlog.performed_at and log_map[key].performed_at and mlog.performed_at > log_map[key].performed_at):
            log_map[key] = mlog
    now = datetime.now(timezone.utc)
    result = []
    for printer in printers:
        total_hours = hours_map.get(printer.id, 0)
        printer_tasks, worst = [], 'ok'
        for task in tasks:
            if task.printer_model_filter and task.printer_model_filter.lower() not in (printer.model or '').lower():
                continue
            last_log = log_map.get((printer.id, task.id))
            if last_log and last_log.performed_at:
                hours_since = max(0.0, total_hours - (last_log.print_hours_at_service or 0))
                performed_at = last_log.performed_at
                if performed_at.tzinfo is None:
                    performed_at = performed_at.replace(tzinfo=timezone.utc)
                anchor, days_since = performed_at, (now - performed_at).days
                last_serviced, last_by = last_log.performed_at.isoformat(), last_log.performed_by
            else:
                hours_since = total_hours
                created = printer.created_at
                if created and created.tzinfo is None:
                    created = created.replace(tzinfo=timezone.utc)
                days_since = (now - created).days if created else 0
                anchor, last_serviced, last_by = created, None, None
            next_hours = until = next_date = None
            if task.interval_print_hours and task.interval_print_hours > 0:
                next_hours = round(total_hours - hours_since + task.interval_print_hours, 1)
                until = round(task.interval_print_hours - hours_since, 1)
            if task.interval_days and task.interval_days > 0 and anchor:
                next_date = (anchor + timedelta(days=task.interval_days)).isoformat()
            st, progress = 'ok', 0.0
            if task.interval_print_hours and task.interval_print_hours > 0:
                progress = max(progress, hours_since / task.interval_print_hours * 100)
                if hours_since >= task.interval_print_hours:
                    st = 'overdue'
                elif hours_since >= task.interval_print_hours * 0.8:
                    st = 'due_soon'
            if task.interval_days and task.interval_days > 0:
                progress = max(progress, days_since / task.interval_days * 100)
                if days_since >= task.interval_days:
                    st = 'overdue'
                elif days_since >= task.interval_days * 0.8 and st != 'overdue':
                    st = 'due_soon'
            if st == 'overdue':
                worst = 'overdue'
            elif st == 'due_soon' and worst == 'ok':
                worst = 'due_soon'
            printer_tasks.append({
                'task_id': task.id, 'task_name': task.name, 'description': task.description,
                'interval_print_hours': task.interval_print_hours, 'interval_days': task.interval_days,
                'hours_since_service': round(hours_since, 1), 'days_since_service': days_since,
                'last_serviced': last_serviced, 'last_by': last_by, 'status': st,
                'progress_percent': round(min(progress, 150), 1), 'next_due_print_hours': next_hours,
                'hours_until_due': until, 'next_due_date': next_date})
        result.append({
            'printer_id': printer.id, 'printer_name': printer.name, 'printer_model': printer.model,
            'total_print_hours': round(total_hours, 1), 'total_print_count': printer.total_print_count or 0,
            'prints_since_maintenance': printer.prints_since_maintenance or 0,
            'hours_since_maintenance': round(float(printer.hours_since_maintenance or 0), 1),
            'tasks': sorted(printer_tasks, key=lambda t: {'overdue': 0, 'due_soon': 1, 'ok': 2}.get(t['status'], 3)),
            'overall_status': worst})
    result.sort(key=lambda p: {'overdue': 0, 'due_soon': 1, 'ok': 2}.get(p['overall_status'], 3))
    return result


def test_route_output_matches_the_pre_extraction_computation(maintenance_client, review_db):
    client, user = maintenance_client
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    review_db.execute(text("INSERT INTO maintenance_tasks(id,name,printer_model_filter,interval_print_hours,interval_days,is_active) VALUES"
                           "(401,'Days task',NULL,NULL,30,1),(402,'Other model','X1',50,NULL,1),(403,'Inactive',NULL,5,NULL,0),(404,'Both','P1S',200,60,1)"))
    review_db.execute(text("INSERT INTO maintenance_logs(id,printer_id,task_id,task_name,performed_at,print_hours_at_service) VALUES"
                           "(410,9,401,'x',:a,50),(411,9,404,'x',:b,60),(412,12,401,'x',:c,0)"),
                      {'a': now - timedelta(days=26), 'b': now - timedelta(days=5), 'c': now - timedelta(days=45)})
    review_db.commit()
    with Session(review_db.get_bind()) as db:
        legacy = _legacy_status(db, user)
    current = client.get(STATUS).json()
    for printer in current:
        printer.pop('failure_rate')
        for task in printer['tasks']:
            assert task.pop('is_default') is False
    assert current == legacy
    assert {p['printer_id'] for p in current} >= {9, 12}


def test_compute_status_without_a_user_sees_every_printer(maintenance_client, review_db):
    from modules.system.maintenance_status import compute_status
    with Session(review_db.get_bind()) as db:
        ids = {p['printer_id'] for p in compute_status(db, None)}
    assert {9, 11, 12} <= ids  # printer 11 belongs to another org: a user would not see it


# ---------------------------------------------------------------- notifications

def test_alert_raised_once_per_transition_not_hourly(maintenance_client, review_db, checker):
    client, _ = maintenance_client
    name = review_db.execute(text('SELECT name FROM printers WHERE id=9')).scalar_one()
    _hours(review_db, 95)                       # 5 h since service of a 10 h task: ok
    checker()
    assert _alerts(review_db) == []

    _hours(review_db, 99)                       # 9 h: due soon
    checker()
    soon = _alerts(review_db)
    assert soon and {a['meta']['stage'] for a in soon} == {'due_soon'}
    assert {a['severity'] for a in soon} == {'info'}
    assert all(a['message'].startswith(f'Maintenance due soon on {name}: Synthetic service') for a in soon)
    checker(); checker()                        # hourly passes: nothing new
    assert len(_alerts(review_db)) == len(soon)

    _hours(review_db, 101)                      # 11 h: overdue (not swallowed by the 24 h per-printer rule)
    checker()
    alerts = _alerts(review_db)
    overdue = [a for a in alerts if a['meta']['stage'] == 'overdue']
    assert overdue and {a['severity'] for a in overdue} == {'critical'}
    assert len(alerts) == len(soon) + len(overdue)
    assert overdue[0]['message'] == f'Maintenance overdue on {name}: Synthetic service (every 10 print hours; 11 since last service)'
    assert overdue[0]['meta']['task_id'] == 101 and overdue[0]['meta']['default'] is False
    checker()
    assert len(_alerts(review_db)) == len(alerts)
    assert _state(review_db)['9:101'] == 'overdue'


def test_jump_from_ok_to_overdue_raises_only_overdue(maintenance_client, review_db, checker):
    _hours(review_db, 95)
    checker()
    _hours(review_db, 120)
    checker()
    assert {a['meta']['stage'] for a in _alerts(review_db)} == {'overdue'}


def test_logging_service_resets_the_stage(maintenance_client, review_db, checker):
    client, _ = maintenance_client
    _hours(review_db, 101)
    checker()
    raised = len(_alerts(review_db))
    assert raised
    assert client.post(LOGS, json={'printer_id': 9, 'task_id': 101, 'task_name': 'Synthetic service'}).status_code == 200
    checker()
    assert len(_alerts(review_db)) == raised
    assert _state(review_db)['9:101'] == 'ok'
    _hours(review_db, 112)                      # 11 h since the new service
    checker()
    assert len(_alerts(review_db)) > raised


def test_quiet_hours_keep_the_inbox_row_and_hold_external_delivery(maintenance_client, review_db, checker, monkeypatch):
    from modules.notifications import alert_dispatcher
    monkeypatch.setattr(alert_dispatcher, 'should_suppress_notification', lambda org_id=None, db=None: True)
    _hours(review_db, 95); checker()            # not the first run
    _hours(review_db, 99)
    checker()
    assert _alerts(review_db)
    plans = [p for p in checker.delivered if p.printer_id == 9]
    assert plans and all(p.suppress_external for p in plans)


def test_alert_preferences_are_respected(maintenance_client, review_db, checker):
    _hours(review_db, 95); checker()            # not the first run
    _hours(review_db, 99)
    checker()
    recipients = {a['user_id'] for a in _alerts(review_db)}
    assert recipients
    muted = sorted(recipients)[0]
    review_db.execute(text('DELETE FROM alerts WHERE printer_id=9'))
    review_db.execute(text("UPDATE system_config SET value='{}' WHERE key='maintenance_alert_state'"))
    review_db.execute(text("UPDATE alert_preferences SET in_app=0 WHERE user_id=:u AND lower(alert_type)='maintenance_overdue'"), {'u': muted})
    review_db.commit()
    checker()
    assert {a['user_id'] for a in _alerts(review_db)} == recipients - {muted}


def test_first_run_after_upgrade_alerts_overdue_only(maintenance_client, review_db, checker):
    # No stored state yet: a task already due soon is noted quietly; an overdue one alerts.
    _hours(review_db, 99)
    checker()
    assert _alerts(review_db) == [] and _state(review_db)['9:101'] == 'due_soon'
    checker()
    assert _alerts(review_db) == []
    _hours(review_db, 101)
    checker()
    assert {a['meta']['stage'] for a in _alerts(review_db)} == {'overdue'}
    review_db.execute(text('DELETE FROM alerts WHERE printer_id=9'))
    review_db.execute(text("DELETE FROM system_config WHERE key='maintenance_alert_state'"))
    review_db.commit()
    checker()                                   # first run again, already overdue
    assert {a['meta']['stage'] for a in _alerts(review_db)} == {'overdue'}


def test_back_dated_log_takes_off_hours_printed_since(maintenance_client, review_db):
    client, _ = maintenance_client
    review_db.execute(text("UPDATE maintenance_logs SET performed_at='2020-02-01 00:00:00' WHERE printer_id=9"))
    _hours(review_db, 200)
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    review_db.execute(text("INSERT INTO print_jobs(printer_id,job_name,status,started_at,ended_at) VALUES"
                           "(9,'after','completed',:s1,:e1),(9,'before','completed',:s2,:e2),(9,'straddles','completed',:s3,:e3),"
                           "(9,'failed after','failed',:s1,:e1),(9,'cancelled after','cancelled',:s1,:e1)"),
                      {'s1': now - timedelta(days=2, hours=3), 'e1': now - timedelta(days=2),
                       's2': now - timedelta(days=9, hours=5), 'e2': now - timedelta(days=9),
                       's3': now - timedelta(days=5, hours=1), 'e3': now - timedelta(days=5) + timedelta(hours=1)})
    review_db.commit()
    when = datetime.now(timezone.utc) - timedelta(days=5)
    done = client.post(LOGS, json={'printer_id': 9, 'task_id': 101, 'task_name': 'Synthetic service', 'performed_at': when.isoformat()})
    assert done.status_code == 200, done.text
    stored = review_db.execute(text('SELECT print_hours_at_service FROM maintenance_logs WHERE id=:i'), {'i': done.json()['id']}).scalar_one()
    assert abs(stored - 196.0) < 0.05           # 200 - 3 h after - 1 h of the straddling print
    assert abs(_task(client, 101)['hours_since_service'] - 4.0) < 0.11
    review_db.expire_all()   # newest service: 2 completed prints, 4 h since
    prints, hours = review_db.execute(text('SELECT prints_since_maintenance,hours_since_maintenance FROM printers WHERE id=9')).one()
    assert prints == 2 and abs(hours - 4.0) < 0.05


def test_switching_a_task_off_and_on_does_not_alert_again(maintenance_client, review_db, checker):
    _hours(review_db, 95); checker()
    _hours(review_db, 101); checker()
    count = len(_alerts(review_db))
    assert count
    review_db.execute(text('UPDATE maintenance_tasks SET is_active=0 WHERE id=101')); review_db.commit()
    checker()
    assert _state(review_db).get('9:101') == 'overdue'
    review_db.execute(text('UPDATE maintenance_tasks SET is_active=1 WHERE id=101')); review_db.commit()
    checker()
    assert len(_alerts(review_db)) == count


def test_corrupt_state_values_are_ignored(maintenance_client, review_db, checker):
    _hours(review_db, 95); checker()
    review_db.execute(text("UPDATE system_config SET value=:v WHERE key='maintenance_alert_state'"),
                      {'v': json.dumps({'9:101': ['overdue'], '9:999': {'a': 1}})})
    review_db.commit()
    checker()                                   # must not raise
    assert _state(review_db)['9:101'] == 'ok'


def test_state_is_dropped_for_tasks_that_no_longer_exist(maintenance_client, review_db, checker):
    client, _ = maintenance_client
    _hours(review_db, 95)
    checker()
    assert '9:101' in _state(review_db)
    assert client.delete('/api/v1/maintenance/tasks/101').status_code == 204
    checker()
    assert '9:101' not in _state(review_db)
    assert '9:default' in _state(review_db)


# ---------------------------------------------------------------- back-dated logs

def test_future_and_pre_2020_service_dates_are_refused(maintenance_client, review_db):
    client, _ = maintenance_client
    before = review_db.execute(text('SELECT count(*) FROM maintenance_logs')).scalar_one()
    base = {'printer_id': 9, 'task_id': 101, 'task_name': 'Synthetic service'}
    future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    assert client.post(LOGS, json={**base, 'performed_at': future}).status_code == 422
    assert client.post(LOGS, json={**base, 'performed_at': '2019-12-31T23:59:59'}).status_code == 422
    assert client.post(LOGS, json={**base, 'performed_at': 'yesterday'}).status_code == 422
    assert review_db.execute(text('SELECT count(*) FROM maintenance_logs')).scalar_one() == before


def test_back_dated_log_anchors_status_and_sets_counters_only_when_newest(maintenance_client, review_db):
    client, _ = maintenance_client
    review_db.execute(text("UPDATE maintenance_logs SET performed_at='2020-02-01 00:00:00' WHERE printer_id=9"))
    review_db.execute(text('UPDATE printers SET prints_since_maintenance=7, hours_since_maintenance=12 WHERE id=9'))
    review_db.commit()
    when = datetime.now(timezone.utc) - timedelta(days=10, minutes=1)
    done = client.post(LOGS, json={'printer_id': 9, 'task_id': 101, 'task_name': 'Synthetic service', 'performed_at': when.isoformat()})
    assert done.status_code == 200, done.text
    assert _task(client, 101)['days_since_service'] == 10
    stored = review_db.execute(text('SELECT performed_at FROM maintenance_logs WHERE id=:i'), {'i': done.json()['id']}).scalar_one()
    assert str(stored).startswith(when.strftime('%Y-%m-%d %H:%M'))
    review_db.expire_all()
    # Now the newest service: the counters become what was printed since (nothing here).
    assert tuple(review_db.execute(text('SELECT prints_since_maintenance,hours_since_maintenance FROM printers WHERE id=9')).one()) == (0, 0)
    review_db.execute(text('UPDATE printers SET prints_since_maintenance=7, hours_since_maintenance=12 WHERE id=9')); review_db.commit()
    # An older back-dated entry does not move the anchor: the latest performed_at wins.
    older = datetime.now(timezone.utc) - timedelta(days=20)
    assert client.post(LOGS, json={'printer_id': 9, 'task_id': 101, 'task_name': 'Synthetic service', 'performed_at': older.isoformat()}).status_code == 200
    assert _task(client, 101)['days_since_service'] == 10
    review_db.expire_all()   # an older entry is not the newest service: counters untouched
    assert tuple(review_db.execute(text('SELECT prints_since_maintenance,hours_since_maintenance FROM printers WHERE id=9')).one()) == (7, 12)


def test_date_only_service_date_is_accepted(maintenance_client, review_db):
    client, _ = maintenance_client
    review_db.execute(text("UPDATE maintenance_logs SET performed_at='2020-02-01 00:00:00' WHERE printer_id=9")); review_db.commit()
    day = (datetime.now(timezone.utc) - timedelta(days=3)).date().isoformat()
    assert client.post(LOGS, json={'printer_id': 9, 'task_id': 101, 'task_name': 'Synthetic service', 'performed_at': day}).status_code == 200
    assert _task(client, 101)['days_since_service'] == 3


def test_log_without_performed_at_is_unchanged_and_resets_counters(maintenance_client, review_db):
    client, _ = maintenance_client
    review_db.execute(text('UPDATE printers SET prints_since_maintenance=7, hours_since_maintenance=12 WHERE id=9')); review_db.commit()
    assert client.post(LOGS, json={'printer_id': 9, 'task_id': 101, 'task_name': 'Synthetic service'}).status_code == 200
    review_db.expire_all()
    assert tuple(review_db.execute(text('SELECT prints_since_maintenance,hours_since_maintenance FROM printers WHERE id=9')).one()) == (0, 0)


def test_dry_run_previews_performed_at_and_writes_nothing(maintenance_client, review_db):
    client, _ = maintenance_client
    before = review_db.execute(text('SELECT count(*) FROM maintenance_logs')).scalar_one()
    day = (datetime.now(timezone.utc) - timedelta(days=3)).date().isoformat()
    preview = client.post(LOGS, headers={'X-Dry-Run': 'true'},
                          json={'printer_id': 9, 'task_id': 101, 'task_name': 'Synthetic service', 'performed_at': day})
    assert preview.status_code == 200, preview.text
    assert preview.json()['would_execute']['performed_at'].startswith(day)
    assert review_db.execute(text('SELECT count(*) FROM maintenance_logs')).scalar_one() == before


# ---------------------------------------------------------------- implicit 2000 h task

def test_implicit_default_task_only_while_the_printer_has_no_task(maintenance_client, review_db):
    client, _ = maintenance_client
    assert [t['task_id'] for t in _printer(client)['tasks']] == [101]
    assert client.delete('/api/v1/maintenance/tasks/101').status_code == 204
    (task,) = _printer(client)['tasks']
    assert (task['task_id'], task['task_name'], task['interval_print_hours'], task['is_default']) == (None, 'General service', 2000.0, True)
    rows_before = review_db.execute(text('SELECT count(*) FROM maintenance_tasks')).scalar_one()
    assert rows_before == 0                     # no rows are written for it

    # A template for another model does not apply to this P1S, so the default stays.
    review_db.execute(text("INSERT INTO maintenance_tasks(id,name,printer_model_filter,interval_days,is_active) VALUES(500,'X1 only','X1',30,1)")); review_db.commit()
    assert [t['is_default'] for t in _printer(client)['tasks']] == [True]

    # Any applicable task, even a deactivated one, replaces it.
    review_db.execute(text("INSERT INTO maintenance_tasks(id,name,interval_days,is_active) VALUES(501,'Switched off',30,0)")); review_db.commit()
    assert _printer(client)['tasks'] == []
    review_db.execute(text('DELETE FROM maintenance_tasks WHERE id=501')); review_db.commit()
    created = client.post('/api/v1/maintenance/tasks', json={'name': 'Real task', 'interval_days': 30})
    assert created.status_code == 200
    assert [t['task_id'] for t in _printer(client)['tasks']] == [created.json()['id']]


@pytest.mark.parametrize('hours,expected', [(1500, 'ok'), (1700, 'due_soon'), (2000, 'overdue')])
def test_implicit_default_task_thresholds(maintenance_client, review_db, hours, expected):
    client, _ = maintenance_client
    client.delete('/api/v1/maintenance/tasks/101')
    _hours(review_db, hours)
    task = _printer(client)['tasks'][0]
    assert task['is_default'] and task['status'] == expected and task['hours_since_service'] == hours


def test_logging_general_service_resets_the_default_but_other_logs_do_not(maintenance_client, review_db):
    client, _ = maintenance_client
    client.delete('/api/v1/maintenance/tasks/101')
    _hours(review_db, 2100)
    assert client.post(LOGS, json={'printer_id': 9, 'task_name': 'Nozzle change'}).status_code == 200   # ad-hoc
    assert _printer(client)['tasks'][0]['status'] == 'overdue'
    assert client.post(LOGS, json={'printer_id': 9, 'task_name': 'General service'}).status_code == 200
    task = _printer(client)['tasks'][0]
    assert (task['status'], task['hours_since_service']) == ('ok', 0)


def test_checker_alerts_on_the_default_task(maintenance_client, review_db, checker):
    client, _ = maintenance_client
    client.delete('/api/v1/maintenance/tasks/101')
    _hours(review_db, 2100)
    checker()                                   # first run: the new default task never alerts
    assert _alerts(review_db) == [] and _state(review_db)['9:default'] == 'overdue'
    review_db.execute(text("UPDATE system_config SET value='{}' WHERE key='maintenance_alert_state'")); review_db.commit()
    checker()                                   # any later run: it does
    alerts = _alerts(review_db)
    assert alerts and all(a['meta']['default'] is True and a['meta']['stage'] == 'overdue' for a in alerts)
    assert 'General service (every 2000 print hours; 2100 since last service)' in alerts[0]['message']
    assert _state(review_db)['9:default'] == 'overdue'
    checker()
    assert len(_alerts(review_db)) == len(alerts)


# ---------------------------------------------------------------- failure rate

def _job(review_db, status, days_ago, error_code=None, ended=True, printer_id=9):
    moment = (datetime.now(timezone.utc) - timedelta(days=days_ago)).replace(tzinfo=None)
    review_db.execute(text("INSERT INTO print_jobs(printer_id,filename,job_name,started_at,ended_at,status,error_code) "
                           "VALUES(:p,'a.3mf','a',:s,:e,:st,:ec)"),
                      {'p': printer_id, 's': moment - timedelta(hours=1), 'e': moment if ended else None, 'st': status, 'ec': error_code})


def test_failure_rate_windows_and_exclusions(maintenance_client, review_db):
    from core.physical_print import CLOSED_UNKNOWN
    client, _ = maintenance_client
    for _i in range(3):
        _job(review_db, 'completed', 5)
    _job(review_db, 'failed', 5)
    for _i in range(2):
        _job(review_db, 'cancelled', 5)                         # cancelled: excluded
    _job(review_db, 'failed', 5, error_code=CLOSED_UNKNOWN)     # retired by a new physical start: excluded
    _job(review_db, 'running', 0, ended=False)                  # not finished: excluded
    for _i in range(2):
        _job(review_db, 'failed', 60)                           # 90-day window only
    for _i in range(5):
        _job(review_db, 'completed', 100)                       # outside both windows
    review_db.commit()
    rate = _printer(client)['failure_rate']
    assert rate['last_30_days'] == {'completed': 3, 'failed': 1, 'rate_percent': 25.0}
    assert rate['last_90_days'] == {'completed': 3, 'failed': 3, 'rate_percent': 50.0}
    assert _printer(client, 12)['failure_rate'] == {
        'last_30_days': {'completed': 0, 'failed': 0, 'rate_percent': None},
        'last_90_days': {'completed': 0, 'failed': 0, 'rate_percent': None}}


def test_failure_rate_is_per_printer(maintenance_client, review_db):
    client, _ = maintenance_client
    _job(review_db, 'failed', 2, printer_id=12)
    _job(review_db, 'completed', 2, printer_id=9)
    review_db.commit()
    assert _printer(client, 12)['failure_rate']['last_30_days'] == {'completed': 0, 'failed': 1, 'rate_percent': 100.0}
    assert _printer(client, 9)['failure_rate']['last_30_days'] == {'completed': 1, 'failed': 0, 'rate_percent': 0.0}


# ---------------------------------------------------------------- RBAC unchanged

def test_status_scope_and_write_roles_unchanged(maintenance_client, review_db):
    client, user = maintenance_client
    user['role'] = 'viewer'
    response = client.get(STATUS)
    assert response.status_code == 200
    ids = {p['printer_id'] for p in response.json()}
    assert 9 in ids and 11 not in ids           # foreign-org printer stays hidden
    before = review_db.execute(text('SELECT count(*) FROM maintenance_logs')).scalar_one()
    day = (datetime.now(timezone.utc) - timedelta(days=2)).date().isoformat()
    assert client.post(LOGS, json={'printer_id': 9, 'task_name': 'Denied', 'performed_at': day}).status_code == 403
    assert review_db.execute(text('SELECT count(*) FROM maintenance_logs')).scalar_one() == before
