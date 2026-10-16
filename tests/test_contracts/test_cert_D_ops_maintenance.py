"""cert-D ops: F17 Maintenance through the actual maintenance router, real
canonical SQLite schema and the real monitor completion path for care counters."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import text

from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_maintenance_behavior import maintenance_client
from tests.test_contracts.cert_D_capture import publish

FIXTURE = Path(__file__).resolve().parents[2] / 'frontend/src/test-fixtures/certDops-maintenance-status-wire.json'


def _status_task(client, printer_id, task_id):
    state = next(p for p in client.get('/api/v1/maintenance/status').json() if p['printer_id'] == printer_id)
    return state, next(t for t in state['tasks'] if t['task_id'] == task_id)


def test_recurring_interval_task_is_one_template_applied_to_each_printer(maintenance_client, review_db):
    """Tasks are global templates (optionally model-filtered) evaluated per printer,
    not per-printer task rows with stored due dates."""
    client, _ = maintenance_client
    created = client.post('/api/v1/maintenance/tasks', json={'name': 'certDops recurring', 'interval_print_hours': 50, 'interval_days': 30})
    assert created.status_code == 200, created.text
    task_id = created.json()['id']
    status = client.get('/api/v1/maintenance/status').json()
    printers_with_task = {p['printer_id'] for p in status if any(t['task_id'] == task_id for t in p['tasks'])}
    assert {9, 10, 12} <= printers_with_task
    columns = {row[1] for row in review_db.execute(text('PRAGMA table_info(maintenance_tasks)')).all()}
    assert 'printer_id' not in columns and 'due_date' not in columns and 'next_due' not in columns


def test_completing_recurring_task_resets_its_counter_without_creating_next_instance(maintenance_client, review_db):
    client, _ = maintenance_client
    review_db.execute(text("UPDATE maintenance_tasks SET interval_print_hours=10, interval_days=30 WHERE id=101"))
    review_db.execute(text("UPDATE maintenance_logs SET performed_at=:t WHERE id=101"), {'t': (datetime.now(timezone.utc) - timedelta(days=40)).replace(tzinfo=None)})
    review_db.commit()
    _, before = _status_task(client, 9, 101)
    assert before['status'] == 'overdue' and before['days_since_service'] == 40
    tasks_before = review_db.execute(text('SELECT COUNT(*) FROM maintenance_tasks')).scalar_one()
    done = client.post('/api/v1/maintenance/logs', json={'printer_id': 9, 'task_id': 101, 'task_name': 'Synthetic service'})
    assert done.status_code == 200, done.text
    _, after = _status_task(client, 9, 101)
    assert (after['status'], after['hours_since_service'], after['days_since_service'], after['progress_percent']) == ('ok', 0, 0, 0)
    # The same template recurs by interval; no "next instance" row is created.
    assert review_db.execute(text('SELECT COUNT(*) FROM maintenance_tasks')).scalar_one() == tasks_before
    # Recurrence: the reset counter climbs back to due_soon/overdue by interval.
    review_db.execute(text('UPDATE printers SET total_print_hours=108 WHERE id=9')); review_db.commit()
    assert _status_task(client, 9, 101)[1]['status'] == 'due_soon'
    review_db.execute(text('UPDATE printers SET total_print_hours=110 WHERE id=9')); review_db.commit()
    assert _status_task(client, 9, 101)[1]['status'] == 'overdue'


def test_completing_maintenance_resets_printer_prints_since_maintenance(maintenance_client, review_db):
    client, _ = maintenance_client
    review_db.execute(text('UPDATE printers SET prints_since_maintenance=7, hours_since_maintenance=12.5 WHERE id=9'))
    review_db.commit()
    done = client.post('/api/v1/maintenance/logs', json={'printer_id': 9, 'task_id': 101, 'task_name': 'Synthetic service'})
    assert done.status_code == 200, done.text
    review_db.expire_all()
    row = review_db.execute(text('SELECT prints_since_maintenance, hours_since_maintenance FROM printers WHERE id=9')).one()
    assert tuple(row) == (0, 0)


def test_successful_print_completion_increments_care_counters(review_db, monkeypatch):
    """Real monitor completion path (job_events.job_completed) on the canonical schema."""
    from core import db as core_db, db_utils
    from modules.notifications import job_events
    import core.committed_terminal_events as committed
    engine = review_db.get_bind()
    monkeypatch.setattr(db_utils, 'engine', engine)
    monkeypatch.setattr(core_db, 'engine', engine)
    monkeypatch.setattr(job_events, 'dispatch_alert', lambda **kwargs: None)  # notification boundary
    monkeypatch.setattr(job_events, '_start_bed_cooled_monitor', lambda *args: None)  # physical bed boundary
    monkeypatch.setattr(committed, 'publish_committed_completion', lambda *a, **k: None)  # push boundary
    review_db.execute(text('UPDATE printers SET total_print_hours=10, total_print_count=4, hours_since_maintenance=1, prints_since_maintenance=2 WHERE id=9'))
    review_db.execute(text("INSERT INTO print_jobs(id,printer_id,filename,job_name,started_at,status) VALUES(7701,9,'certdops.3mf','certdops',CURRENT_TIMESTAMP,'running')"))
    review_db.commit()
    job_events.job_completed(9, 7701, success=True, duration_seconds=5400)
    review_db.expire_all()
    row = review_db.execute(text('SELECT total_print_hours,total_print_count,hours_since_maintenance,prints_since_maintenance FROM printers WHERE id=9')).one()
    assert tuple(row) == (11.5, 5, 2.5, 3)
    assert review_db.execute(text('SELECT status FROM print_jobs WHERE id=7701')).scalar_one() == 'completed'


def test_capture_maintenance_status_wire_for_dashboard_widget(maintenance_client, review_db):
    client, _ = maintenance_client
    now = datetime.now(timezone.utc).replace(tzinfo=None)  # canonical naive UTC like server_default
    review_db.execute(text("UPDATE printers SET total_print_hours=118, total_print_count=40, prints_since_maintenance=7, hours_since_maintenance=28 WHERE id=9"))
    review_db.execute(text("INSERT INTO maintenance_tasks(id,name,interval_print_hours,interval_days,is_active) VALUES(301,'certDops due soon',None,10,1)".replace('None', 'NULL')))
    review_db.execute(text("INSERT INTO maintenance_tasks(id,name,interval_print_hours,interval_days,is_active) VALUES(302,'certDops fine',1000,NULL,1)"))
    review_db.execute(text("INSERT INTO maintenance_logs(id,printer_id,task_id,task_name,performed_at,print_hours_at_service) VALUES(301,9,301,'x',:t,118)"), {'t': now - timedelta(days=8, minutes=1)})
    review_db.execute(text("INSERT INTO maintenance_logs(id,printer_id,task_id,task_name,performed_at,print_hours_at_service) VALUES(302,9,302,'x',:t,118)"), {'t': now - timedelta(days=1)})
    review_db.commit()
    response = client.get('/api/v1/maintenance/status')
    assert response.status_code == 200
    printer = next(p for p in response.json() if p['printer_id'] == 9)
    by_id = {t['task_id']: t for t in printer['tasks']}
    assert by_id[101]['status'] == 'overdue' and by_id[101]['hours_since_service'] == 28
    assert by_id[301]['status'] == 'due_soon' and by_id[301]['progress_percent'] == 80
    assert by_id[302]['status'] == 'ok' and by_id[302]['progress_percent'] == 0
    assert printer['total_print_hours'] == 118 and printer['overall_status'] == 'overdue'
    assert (printer['total_print_count'], printer['prints_since_maintenance'], printer['hours_since_maintenance']) == (40, 7, 28)
    # Next-due handling: hour interval measured from the last service (90h) and day interval from performed_at.
    assert (by_id[101]['next_due_print_hours'], by_id[101]['hours_until_due']) == (100, -18)
    assert by_id[301]['next_due_date'].startswith((now - timedelta(days=8, minutes=1) + timedelta(days=10)).isoformat()[:16])
    printer['tasks'] = [t for t in printer['tasks'] if t['task_id'] in (101, 301, 302)]
    # Exact body Maintenance.tsx LogMaintenanceModal sends for "Log Service" on task 101.
    ui_body = {'printer_id': 9, 'task_id': 101, 'task_name': 'Synthetic service', 'performed_by': None,
               'cost': 0, 'downtime_minutes': 0, 'notes': None}
    done = client.post('/api/v1/maintenance/logs', json=ui_body)
    assert done.status_code == 200, done.text
    after = next(p for p in client.get('/api/v1/maintenance/status').json() if p['printer_id'] == 9)
    after_task = next(t for t in after['tasks'] if t['task_id'] == 101)
    assert (after_task['status'], after_task['hours_since_service']) == ('ok', 0)
    assert (after_task['next_due_print_hours'], after_task['hours_until_due']) == (128, 10)
    assert (after['prints_since_maintenance'], after['hours_since_maintenance'], after['total_print_count']) == (0, 0, 40)
    after['tasks'] = [t for t in after['tasks'] if t['task_id'] in (101, 301, 302)]
    fixture = {'scope': 'certDops owned actual GET /api/v1/maintenance/status (one printer, three task states)',
               'status': [printer], 'complete_request': ui_body,
               'complete_response': {k: done.json()[k] for k in ('id', 'message', 'print_hours_at_service')},
               'status_after': [after]}
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    publish(FIXTURE, fixture)


def test_admin_maintenance_mutations_leave_no_audit_history(maintenance_client, review_db):
    """Evidence for F19-19.1-01 wording: not every admin action is audited."""
    client, _ = maintenance_client
    review_db.execute(text('DELETE FROM audit_logs')); review_db.commit()
    assert client.post('/api/v1/maintenance/seed-defaults').status_code == 200
    assert client.delete('/api/v1/maintenance/tasks/101').status_code == 204
    assert client.delete('/api/v1/maintenance/logs/101').status_code == 204
    assert review_db.execute(text('SELECT COUNT(*) FROM audit_logs')).scalar_one() == 0



def test_dry_run_describes_the_counter_reset_and_writes_nothing(maintenance_client, review_db):
    client, _ = maintenance_client
    review_db.execute(text('UPDATE printers SET prints_since_maintenance=5, hours_since_maintenance=3 WHERE id=9'))
    review_db.commit()
    before = review_db.execute(text('SELECT COUNT(*) FROM maintenance_logs')).scalar_one()
    preview = client.post('/api/v1/maintenance/logs', headers={'X-Dry-Run': 'true'},
                          json={'printer_id': 9, 'task_id': 101, 'task_name': 'Synthetic service'})
    assert preview.status_code == 200, preview.text
    assert 'reset' in str(preview.json()).lower() and 'care counters' in str(preview.json())
    review_db.expire_all()
    assert review_db.execute(text('SELECT COUNT(*) FROM maintenance_logs')).scalar_one() == before
    assert tuple(review_db.execute(text('SELECT prints_since_maintenance,hours_since_maintenance FROM printers WHERE id=9')).one()) == (5, 3)
