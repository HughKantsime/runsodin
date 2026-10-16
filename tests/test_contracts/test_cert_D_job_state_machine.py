"""cert-D F04-4.1: one job state machine is the only path that writes jobs.status.

1. Census (static, per call site; tests/test_contracts/job_census.py): no code other than
   modules/jobs/state_machine.py writes jobs.status -- raw SQL (aliases, quoted names,
   upserts, REPLACE, dynamic SET lists), ORM attribute writes and setattr on any
   Job-bound name (including Job-typed parameters), update(Job)/sa_update/__table__.update,
   query(Job).update, bulk_update_mappings.
2. Census (runtime): an autouse fixture (conftest.py, every contract test) traces every
   SQLite statement and Job.status assignment and fails a test whose product code
   writes jobs.status outside the state machine.
3. Regression tests for the writers that used to bypass the guards.
"""
import re
import json
from pathlib import Path

import pytest
from sqlalchemy import event, text

from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401
from tests.test_contracts.test_license_user_limits import signed_license  # noqa: F401
from tests.test_contracts.test_cert_D_jobsB_scheduler import fleet, specific_client  # noqa: F401

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / 'backend'
STATE_MACHINE = 'backend/modules/jobs/state_machine.py'
from tests.test_contracts import job_census

# Call sites outside the state machine that the census sees but that never write
# jobs.status, each with its reason and a check that keeps the reason true.
def _patch_refuses_status_before_copying_fields():
    """update_job raises when 'status' was sent, before its setattr loop runs."""
    import ast
    tree = ast.parse((BACKEND / 'modules/jobs/routes/jobs_crud.py').read_text())
    function = next(n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == 'update_job')
    guard = next(n for n in ast.walk(function) if isinstance(n, ast.If)
                 and ast.unparse(n.test) == "'status' in updates.model_fields_set")
    copy = next(n for n in ast.walk(function) if isinstance(n, ast.For) and 'setattr(job' in ast.unparse(n))
    return guard.lineno < copy.lineno and any(isinstance(n, ast.Raise) for n in guard.body)


def _priced_restore_never_targets_jobs():
    import ast
    from core import priced_estimate_schema as schema
    source = (BACKEND / 'core/priced_estimate_restore.py').read_text()
    calls = [n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Call) and ast.unparse(n.func) == '_cas']
    tables = {getattr(schema, ast.unparse(c.args[1]).split('.')[-1]) for c in calls}
    return calls and 'jobs' not in tables


ALLOWED_WRITES = {
    ('backend/modules/jobs/routes/jobs_crud.py', 'update_job', 'setattr'):
        ('PATCH copies JobUpdate fields only after refusing a sent status (422)', _patch_refuses_status_before_copying_fields),
    ('backend/modules/jobs/routes/jobs_lifecycle.py', 'update_job_failure', 'sql update (dynamic SET)'):
        ('the SET list is built only from the literals fail_reason/fail_notes/updated_at', lambda: True),
    ('backend/core/power_target_operations.py', '_stage_transition', 'sql update (dynamic table)'):
        ('power-operation state CAS on its own table', lambda: job_census.resolve(
            'backend/core/power_target_operations.py', 'schema.TABLE') != 'jobs'),
    ('backend/core/priced_estimate_restore.py', '_cas', 'sql update (dynamic table)'):
        ('restore CAS of the priced-estimate tables only', _priced_restore_never_targets_jobs),
    ('backend/core/schema/migrations/001_legacy_columns.py', 'apply', 'sql update (dynamic table)'):
        ('bootstrap repair of legacy enum casing (status=LOWER(status) within the same value set); not a transition',
         lambda: True),
}


def _violations(source: str):
    """Status writes in one snippet (planted-example checks)."""
    writes, _, _ = job_census.scan({'backend/modules/planted.py': source})
    return [(kind, line) for _, _, kind, line in writes]


def test_census_no_module_outside_the_state_machine_writes_jobs_status():
    writes, _, _ = job_census.scan()
    offenders = [w for w in writes if w[0] != STATE_MACHINE and w[:3] not in ALLOWED_WRITES]
    assert offenders == [], offenders
    for site, (reason, still_true) in ALLOWED_WRITES.items():
        assert still_true(), (site, reason)
    sites = {w[:3] for w in writes}
    assert set(ALLOWED_WRITES) <= sites, set(ALLOWED_WRITES) - sites  # no stale allowance


PLANTED = [
    "cur.execute(\"UPDATE jobs SET status = 'completed' WHERE id = ?\", (1,))",
    "db.execute(text(f\"UPDATE jobs SET status = '{target}', actual_end = {sql.now()} WHERE id = :id\"))",
    "q = ('UPDATE jobs SET printer_id=NULL,' \n     ' status=:s WHERE id=:id')",
    "q = 'UPDATE jobs SET printer_id=NULL, ' + 'status=:s WHERE id=:id'",
    "q = 'UPDATE \"jobs\" SET status=:s WHERE id=:id'",
    "q = 'UPDATE jobs AS j SET j.status=:s WHERE j.id=:id'",
    "q = 'UPDATE jobs j SET status=:s FROM printers p WHERE p.id=j.printer_id'",
    "q = 'INSERT INTO jobs (id, status) VALUES (:i, :s) ON CONFLICT (id) DO UPDATE SET status=excluded.status'",
    "q = 'INSERT OR REPLACE INTO jobs (id, status) VALUES (1, 2)'",
    "q = f'UPDATE jobs SET {assignments} WHERE id=:id'",
    "q = f'UPDATE {table} SET status=:s WHERE id=:id'",
    "def f(job):\n    job.status = JobStatus.FAILED",
    "def f(target: Job):\n    target.status = 'failed'",
    "def f(target: 'Optional[Job]'):\n    target.status += 'x'",
    "def f(db):\n    db.execute(update(Job).where(Job.id == 1).values(status='failed'))",
    "def f(db):\n    db.execute(sa_update(Job).values(status='failed'))",
    "def f(db):\n    db.execute(Job.__table__.update().values(status='failed'))",
    "def f(db):\n    db.execute(insert(Job).values(id=1).on_conflict_do_update(index_elements=['id'], set_={'status': 'x'}))",
    "def f(db):\n    row = db.query(Job).filter(Job.id == 1).first()\n    row.status = JobStatus.FAILED",
    "def f(db):\n    for entry in db.query(Job).all():\n        entry.status = 'cancelled'",
    "def f(db):\n    current = db.get(Job, 5)\n    current.status = 'printing'",
    "def f(db):\n    db.query(Job).filter(Job.id == 1).update({'status': 'failed'})",
    "def f(db, rows):\n    db.bulk_update_mappings(Job, rows)",
    "def f(db):\n    row = db.get(Job, 5)\n    setattr(row, 'status', 'failed')",
    "def f(db, field, value):\n    row = db.get(Job, 5)\n    setattr(row, field, value)",
    "q = 'UPDATE public.jobs SET status=:s WHERE id=:id'",
    "def f(db):\n    (db.query(Job)\n       .filter(Job.id == 1)\n       .update({'status': 'failed'}))",
    "def f(db):\n    jobs = Job.__table__\n    db.execute(jobs.update().values(status='x'))",
    "def f(db):\n    found = get_job(db, 5)\n    found.status = 'failed'",
]


@pytest.mark.parametrize('planted', PLANTED)
def test_census_scanner_detects_each_bypass_shape(planted):
    assert _violations(planted), planted


def test_census_scanner_ignores_lock_noops_reads_and_other_tables():
    assert not _violations("db.execute(text('UPDATE jobs SET status=status WHERE id=:id'))")
    assert not _violations("rows = db.execute(text(\"SELECT id FROM jobs WHERE status = 'pending'\"))")
    assert not _violations("db.execute(text(\"UPDATE print_jobs SET status='completed' WHERE id=1\"))")
    assert not _violations("def f(print_job):\n    print_job.status = 'completed'")
    assert not _violations("def f(order):\n    order.status = 'shipped'")
    assert not _violations("def f(db):\n    row = db.get(Job, 5)\n    setattr(row, 'notes', 'x')")


def test_census_reports_each_call_site_not_each_file():
    source = ("def a(cur):\n    cur.execute(\"UPDATE jobs SET status='x' WHERE id=1\")\n"
              "def b(cur):\n    cur.execute(\"UPDATE jobs SET status='y' WHERE id=1\")\n")
    writes, _, _ = job_census.scan({'backend/modules/planted.py': source})
    assert [(site, line) for _, site, _, line in writes] == [('a', 2), ('b', 4)]


@pytest.fixture
def status_write_hook(review_db):
    """Record every jobs.status write that reaches the database or the ORM during a test."""
    from modules.jobs.models import Job
    engine = review_db.get_bind()
    sql_writes, orm_writes = [], []

    def before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        if job_census.sql_status_writes(statement):  # any jobs.status write that reaches the database
            sql_writes.append(statement)

    def on_set(target, value, oldvalue, initiator):
        from sqlalchemy import inspect as orm_inspect
        if orm_inspect(target).persistent:  # a new Job's initial status is creation, not a transition
            orm_writes.append((target.id, value))
    event.listen(engine, 'before_cursor_execute', before_cursor_execute)
    event.listen(Job.status, 'set', on_set)
    try:
        yield sql_writes, orm_writes
    finally:
        event.remove(engine, 'before_cursor_execute', before_cursor_execute)
        event.remove(Job.status, 'set', on_set)


def _pin_client(fleet, signed_license, monkeypatch):
    from modules.jobs.routes import jobs_lifecycle, jobs_agent
    client = specific_client(fleet, signed_license, monkeypatch)
    client.app.include_router(jobs_lifecycle.router, prefix='/api/v1')
    client.app.include_router(jobs_agent.router, prefix='/api/v1')
    return client


def _status(db, job_id):
    db.expire_all()
    return db.execute(text('SELECT status FROM jobs WHERE id=:id'), {'id': job_id}).scalar_one()


def _job(db, job_id, status, **columns):
    fields = {'printer_id': 9, 'queue_position': None, **columns}
    db.execute(text("INSERT INTO jobs(id,item_name,status,charged_to_org_id,charged_to_user_id,submitted_by,quantity,"
                    "duration_hours,hold,priority,printer_id,queue_position) VALUES(:id,'SM job',:s,1,3,3,1,1,FALSE,3,"
                    ":printer_id,:queue_position)"), {'id': job_id, 's': status, **fields})
    db.commit()


def test_runtime_hook_lifecycle_writes_only_through_the_state_machine(fleet, signed_license, monkeypatch, status_write_hook):
    sql_writes, orm_writes = status_write_hook
    client = _pin_client(fleet, signed_license, monkeypatch)
    job = client.post('/api/v1/jobs', json={'item_name': 'SM runtime', 'duration_hours': 1}).json()['id']
    for route in ('start', 'complete', 'fail', 'complete', 'reset'):
        assert client.post(f'/api/v1/jobs/{job}/{route}').status_code == 200, route
    from tests.test_contracts.test_cert_D_jobsB_scheduler import run
    run(fleet)
    assert client.post('/api/v1/jobs/bulk-update', json={'job_ids': [job], 'action': 'reschedule'}).status_code == 200
    assert client.post(f'/api/v1/jobs/{job}/cancel').status_code == 200
    # The state machine's statement shape (named params compile to qmark on SQLite).
    assert sql_writes and all(re.match(r'\s*UPDATE jobs SET status=(\?|:sm_to|%\(sm_to\)s)', s) for s in sql_writes), sql_writes
    assert orm_writes == [], orm_writes  # nothing assigned Job.status on an ORM instance


@pytest.mark.parametrize('source', ['submitted', 'rejected', 'printing', 'pending', 'scheduled'])
def test_reset_only_from_a_finished_outcome(fleet, signed_license, monkeypatch, source):
    client = _pin_client(fleet, signed_license, monkeypatch)
    _job(fleet, 701, source)
    assert client.post('/api/v1/jobs/701/reset').status_code == 409
    assert _status(fleet, 701) == source


@pytest.mark.parametrize('source', ['completed', 'failed', 'cancelled'])
def test_reset_from_finished_outcome_bumps_the_print_attempt(fleet, signed_license, monkeypatch, source):
    from core.print_attempts import attempt_seq, sqlalchemy_runner
    client = _pin_client(fleet, signed_license, monkeypatch)
    _job(fleet, 702, source)
    assert client.post('/api/v1/jobs/702/reset').status_code == 200
    assert _status(fleet, 702) == 'pending'
    assert attempt_seq(sqlalchemy_runner(fleet), 702) == 1


@pytest.mark.parametrize('job_status,print_status', [
    ('cancelled', 'completed'), ('submitted', 'completed'), ('rejected', 'completed'),
    ('failed', 'completed'), ('completed', 'failed'), ('cancelled', 'printing')])
def test_link_print_never_moves_refused_or_terminal_jobs(fleet, signed_license, monkeypatch, job_status, print_status):
    client = _pin_client(fleet, signed_license, monkeypatch)
    _job(fleet, 703, job_status)
    fleet.execute(text("INSERT INTO print_jobs(id,printer_id,filename,job_name,started_at,status) "
                       "VALUES(7703,9,'x.3mf','x',CURRENT_TIMESTAMP,:s)"), {'s': print_status})
    fleet.commit()
    response = client.post('/api/v1/jobs/703/link-print', params={'print_job_id': 7703})
    assert response.status_code == 200, response.text
    assert response.json()['status_applied'] is False
    assert _status(fleet, 703) == job_status


def test_link_print_moves_a_live_job(fleet, signed_license, monkeypatch):
    client = _pin_client(fleet, signed_license, monkeypatch)
    _job(fleet, 704, 'printing', queue_position=3)
    fleet.execute(text("INSERT INTO print_jobs(id,printer_id,filename,job_name,started_at,status) "
                       "VALUES(7704,9,'x.3mf','x',CURRENT_TIMESTAMP,'completed')"))
    fleet.commit()
    assert client.post('/api/v1/jobs/704/link-print', params={'print_job_id': 7704}).json()['status_applied'] is True
    assert _status(fleet, 704) == 'completed'
    assert fleet.execute(text('SELECT queue_position FROM jobs WHERE id=704')).scalar_one() is None


@pytest.fixture
def monitor_boundary(review_db, monkeypatch):
    from core import db as core_db, db_utils
    from modules.notifications import job_events
    import core.committed_terminal_events as committed
    engine = review_db.get_bind()
    monkeypatch.setattr(db_utils, 'engine', engine)
    monkeypatch.setattr(core_db, 'engine', engine)
    monkeypatch.setattr(job_events, 'dispatch_alert', lambda **kwargs: None)
    monkeypatch.setattr(job_events, '_start_bed_cooled_monitor', lambda *args: None)
    published = []
    monkeypatch.setattr(committed, 'publish_committed_completion', lambda *a, **k: published.append(a))
    return published


@pytest.mark.parametrize('job_status', ['cancelled', 'submitted', 'rejected', 'failed'])
def test_prusalink_elegoo_print_end_never_overwrites_but_still_publishes_the_physical_print(review_db, monitor_boundary, job_status):
    """Physical-print effects (archive, energy, auto power-off) still run, keyed by the print;
    job-scoped effects do not (the event carries no scheduled job)."""
    from modules.notifications import job_events
    _job(review_db, 705, job_status)
    review_db.execute(text("INSERT INTO print_jobs(id,printer_id,filename,job_name,started_at,status,scheduled_job_id) "
                           "VALUES(7705,9,'x.3mf','x',CURRENT_TIMESTAMP,'running',705)"))
    review_db.commit()
    job_events.job_completed(9, 7705, success=True, duration_seconds=60)
    assert _status(review_db, 705) == job_status
    assert [args[:3] for args in monitor_boundary] == [(9, 7705, None)]


def test_prusalink_elegoo_print_end_completes_a_live_job_and_publishes(review_db, monitor_boundary):
    from modules.notifications import job_events
    _job(review_db, 706, 'printing')
    review_db.execute(text("INSERT INTO print_jobs(id,printer_id,filename,job_name,started_at,status,scheduled_job_id) "
                           "VALUES(7706,9,'x.3mf','x',CURRENT_TIMESTAMP,'running',706)"))
    review_db.commit()
    job_events.job_completed(9, 7706, success=True, duration_seconds=60)
    assert _status(review_db, 706) == 'completed'
    assert [args[:3] for args in monitor_boundary] == [(9, 7706, 706)]


@pytest.mark.parametrize('job_status', ['cancelled', 'completed', 'submitted'])
def test_hms_failure_never_overwrites_a_non_live_job(review_db, monitor_boundary, monkeypatch, job_status):
    from modules.notifications import error_handling
    monkeypatch.setattr(error_handling, 'dispatch_alert', lambda **kwargs: None, raising=False)
    _job(review_db, 707, job_status)
    review_db.execute(text("INSERT INTO print_jobs(id,printer_id,filename,job_name,started_at,status,scheduled_job_id) "
                           "VALUES(7707,9,'x.3mf','x',CURRENT_TIMESTAMP,'running',707)"))
    review_db.commit()
    error_handling._fail_active_job_for_hms(9, '0300_4000', 'Synthetic HMS stop', observed_filename='x.3mf')
    assert _status(review_db, 707) == job_status


def test_reject_clears_queue_position(review_db):
    from modules.jobs.state_machine import transition_job
    _job(review_db, 708, 'submitted', queue_position=2)
    assert transition_job(review_db, 708, 'rejected', 'approval') is True
    review_db.commit()
    assert tuple(review_db.execute(text('SELECT status,queue_position FROM jobs WHERE id=708')).one()) == ('rejected', None)


def test_transition_table_refuses_widening_and_unknown_moves(review_db):
    from modules.jobs.state_machine import transition_job, TransitionError
    _job(review_db, 709, 'pending')
    with pytest.raises(TransitionError):
        transition_job(review_db, 709, 'completed', 'monitor', from_states=('cancelled',))
    with pytest.raises(TransitionError):
        transition_job(review_db, 709, 'printing', 'approval')
    assert _status(review_db, 709) == 'pending'


def test_plate_cleared_releases_by_priority_then_position_through_the_state_machine(fleet, signed_license, monkeypatch):
    from modules.printers import routes_controls
    client = _pin_client(fleet, signed_license, monkeypatch)
    client.app.include_router(routes_controls.router, prefix='/api/v1')
    _job(fleet, 710, 'waiting_plate_clear', queue_position=0)          # placed, normal priority
    _job(fleet, 711, 'waiting_plate_clear', queue_position=None)       # unplaced ...
    fleet.execute(text('UPDATE jobs SET priority=1 WHERE id=711')); fleet.commit()  # ... but urgent
    response = client.post('/api/v1/printers/9/plate-cleared')
    assert response.status_code == 200, response.text
    assert (_status(fleet, 711), _status(fleet, 710)) == ('pending', 'waiting_plate_clear')



@pytest.mark.parametrize('job_status,expected_job', [('failed', None), ('printing', 712)])
def test_mqtt_print_end_publishes_the_physical_print_and_the_job_only_when_it_applied(review_db, monkeypatch, job_status, expected_job):
    from contextlib import contextmanager
    from core.db_utils import _ConnectionAdapter
    import modules.printers.committed_terminal_events as committed
    from modules.printers.monitors import mqtt_job_lifecycle
    from modules.notifications import alert_dispatch
    engine = review_db.get_bind()

    @contextmanager
    def isolated_db():
        connection = engine.raw_connection()
        try:
            yield _ConnectionAdapter(connection, False)
        finally:
            connection.close()
    monkeypatch.setattr(mqtt_job_lifecycle, 'get_db', isolated_db)
    published = []
    monkeypatch.setattr(committed, 'publish_committed_completion', lambda *a, **k: published.append(a))
    monkeypatch.setattr(alert_dispatch, 'dispatch_alert', lambda **kwargs: None)
    monkeypatch.setattr(alert_dispatch, '_start_bed_cooled_monitor', lambda *args: None)
    _job(review_db, 712, job_status)
    review_db.execute(text("INSERT INTO print_jobs(id,printer_id,filename,job_name,started_at,status,scheduled_job_id) "
                           "VALUES(7712,9,'x.3mf','x',CURRENT_TIMESTAMP,'running',712)"))
    review_db.commit()
    mqtt_job_lifecycle.record_job_ended(9, 'P', 7712, 712, 'completed', {'subtask_name': 'x'}, {}, lambda **k: None)
    assert _status(review_db, 712) == ('failed' if job_status == 'failed' else 'completed')
    assert [args[:3] for args in published] == [(9, 7712, expected_job)]


def test_reconcile_dispatch_denial_rolls_back_when_the_job_already_finished():
    """The submission is only reset when its job transition applies (both or neither)."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from modules.organizations.education_policy import reconcile_dispatch_denial
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    with engine.begin() as c:
        c.execute(text("CREATE TABLE jobs (id INTEGER PRIMARY KEY, charged_to_org_id INTEGER, status TEXT, printer_id INTEGER, notes TEXT)"))
        c.execute(text("CREATE TABLE education_submissions (id INTEGER PRIMARY KEY, org_id INTEGER, cost_center_id INTEGER, job_id INTEGER, status TEXT, lifecycle_revision INTEGER, approved_printer_id INTEGER, approved_by INTEGER, compatibility_engine_version TEXT, updated_at TEXT)"))
        c.execute(text("INSERT INTO jobs VALUES (72, 2, 'completed', 9, NULL)"))
        c.execute(text("INSERT INTO education_submissions VALUES (4, 2, 7, 72, 'scheduled', 3, 9, 5, 'e', NULL)"))
    with Session(engine) as db:
        assert reconcile_dispatch_denial(db, submission_id=4, job_id=72, expected_revision=3, reason='drift') is False
    with engine.connect() as c:
        assert tuple(c.execute(text('SELECT status,lifecycle_revision FROM education_submissions WHERE id=4')).one()) == ('scheduled', 3)
        assert c.execute(text('SELECT status FROM jobs WHERE id=72')).scalar_one() == 'completed'


def test_complete_on_a_legacy_null_status_row_is_allowed_as_before(fleet, signed_license, monkeypatch):
    """Decision: legacy/hand-written rows with NULL status may still be completed by an operator
    (kept from the pre-state-machine behaviour); every other actor and transition refuses NULL."""
    from modules.jobs.state_machine import transition_job
    client = _pin_client(fleet, signed_license, monkeypatch)
    fleet.execute(text("INSERT INTO jobs(id,item_name,status,charged_to_org_id,quantity,priority,hold,is_locked) "
                       "VALUES(713,'Legacy null',NULL,1,1,3,0,0)"))
    fleet.commit()
    assert transition_job(fleet, 713, 'failed', 'operator') is False
    fleet.rollback()
    response = client.post('/api/v1/jobs/713/complete')
    assert response.status_code == 200, response.text
    assert _status(fleet, 713) == 'completed'


# ---------------- round 5: job-scoped alerts/events only when the job transition applied ----------------

@pytest.fixture
def alert_capture(monitor_boundary, monkeypatch):
    from types import SimpleNamespace
    from modules.notifications import job_events
    alerts, events = [], []
    monkeypatch.setattr(job_events, 'dispatch_alert', lambda **kwargs: alerts.append(kwargs))
    monkeypatch.setattr(job_events, 'get_event_bus', lambda: SimpleNamespace(publish=events.append))
    return alerts, events


@pytest.mark.parametrize('job_status,expected_job', [('cancelled', None), ('printing', 720)])
@pytest.mark.parametrize('success', [True, False])
def test_prusalink_elegoo_alerts_and_failed_event_name_the_job_only_when_it_applied(
        review_db, alert_capture, job_status, expected_job, success):
    from modules.notifications import job_events
    alerts, events = alert_capture
    _job(review_db, 720, job_status)
    review_db.execute(text("INSERT INTO print_jobs(id,printer_id,filename,job_name,started_at,status,scheduled_job_id) "
                           "VALUES(7720,9,'x.3mf','x',CURRENT_TIMESTAMP,'running',720)"))
    review_db.commit()
    job_events.job_completed(9, 7720, success=success, duration_seconds=60)
    assert [(a['alert_type'], a['printer_id'], a['job_id']) for a in alerts] == [
        ('print_complete' if success else 'print_failed', 9, expected_job)]
    from core import events as ev
    assert [e.data['scheduled_job_id'] for e in events if e.event_type == ev.JOB_FAILED] == ([] if success else [expected_job])


@pytest.mark.parametrize('job_status,expected_job', [('completed', None), ('printing', 721)])
def test_prusalink_elegoo_cancel_alert_and_event_name_the_job_only_when_it_applied(
        review_db, alert_capture, job_status, expected_job):
    from modules.notifications import job_events
    alerts, events = alert_capture
    _job(review_db, 721, job_status)
    review_db.execute(text("INSERT INTO print_jobs(id,printer_id,filename,job_name,started_at,status,scheduled_job_id) "
                           "VALUES(7721,9,'x.3mf','x',CURRENT_TIMESTAMP,'running',721)"))
    review_db.commit()
    job_events.job_cancelled(9, 7721)
    assert _status(review_db, 721) == ('cancelled' if job_status == 'printing' else job_status)
    assert [(a['alert_type'], a['job_id']) for a in alerts] == [('print_cancelled', expected_job)]
    assert [e.data['scheduled_job_id'] for e in events] == [expected_job]


@pytest.mark.parametrize('job_status,expected_job', [('failed', None), ('printing', 722)])
@pytest.mark.parametrize('end_status', ['completed', 'failed'])
def test_mqtt_alerts_name_the_job_only_when_it_applied(review_db, monkeypatch, job_status, expected_job, end_status):
    from contextlib import contextmanager
    from core.db_utils import _ConnectionAdapter
    import modules.printers.committed_terminal_events as committed
    from modules.printers.monitors import mqtt_job_lifecycle
    from modules.notifications import alert_dispatch
    engine = review_db.get_bind()

    @contextmanager
    def isolated_db():
        connection = engine.raw_connection()
        try:
            yield _ConnectionAdapter(connection, False)
        finally:
            connection.close()
    monkeypatch.setattr(mqtt_job_lifecycle, 'get_db', isolated_db)
    monkeypatch.setattr(committed, 'publish_committed_completion', lambda *a, **k: None)
    alerts = []
    monkeypatch.setattr(alert_dispatch, 'dispatch_alert', lambda **kwargs: alerts.append(kwargs))
    monkeypatch.setattr(alert_dispatch, '_start_bed_cooled_monitor', lambda *args: None)
    _job(review_db, 722, job_status)
    review_db.execute(text("INSERT INTO print_jobs(id,printer_id,filename,job_name,started_at,status,scheduled_job_id) "
                           "VALUES(7722,9,'x.3mf','x',CURRENT_TIMESTAMP,'running',722)"))
    review_db.commit()
    mqtt_job_lifecycle.record_job_ended(9, 'P', 7722, 722, end_status, {'subtask_name': 'x'}, {}, lambda **k: None)
    assert [(a['alert_type'], a['job_id']) for a in alerts] == [
        ('print_complete' if end_status == 'completed' else 'print_failed', expected_job)]


from tests.test_contracts.test_education_monitor_policy import admitted_monitor  # noqa: E402,F401
from tests.test_contracts.test_education_dispatch_receipt_binding import admitted_education  # noqa: E402,F401
from tests.test_contracts.test_education_dispatch_policy import actual_education_dispatch  # noqa: E402,F401


def test_education_print_whose_job_was_cancelled_still_ends_physically_publishes_and_archives(admitted_monitor, monkeypatch):
    """Education refused the job transition (operator cancelled mid-print): the printer's own
    print still ends, its physical completion is published without the job, and it is archived."""
    from contextlib import contextmanager
    from core.db_utils import _ConnectionAdapter
    from core.monitor_bootstrap import bootstrap_monitor_authority
    import modules.printers.committed_terminal_events as committed
    from modules.archives import archive
    from modules.printers.monitors import mqtt_job_lifecycle
    bootstrap_monitor_authority()
    monitor_db, uploaded, reservation = admitted_monitor
    engine = monitor_db.get_bind()

    @contextmanager
    def owned_db():
        raw = engine.raw_connection()
        try:
            yield _ConnectionAdapter(raw, False)
        finally:
            raw.close()
    monkeypatch.setattr(mqtt_job_lifecycle, 'get_db', owned_db)
    monkeypatch.setattr(archive, 'get_db', owned_db)
    published = []
    monkeypatch.setattr(committed, 'publish_committed_completion', lambda *a, **k: published.append(a))
    raw_name = reservation["remote_filename"].removesuffix(".3mf")
    state = {"subtask_name": raw_name, "gcode_file": "Metadata/plate_1.gcode",
             "job_id": "owned-hardware-observation", "total_layer_num": 10}
    print_job_id, linked_job_id, weights = mqtt_job_lifecycle.record_job_started(
        printer_id=9, printer_name="Owned A1", state=state, dispatch_alert_fn=lambda **kw: None,
        trigger_reschedule_fn=lambda: None)
    assert linked_job_id == uploaded["job_id"]
    from core.print_attempts import bound_print_attempt, sqlalchemy_runner
    with engine.connect() as conn:  # the Education link bound the print to the job's current attempt
        assert bound_print_attempt(sqlalchemy_runner(conn), print_job_id, uploaded["job_id"]) == 0
    with engine.begin() as conn:  # the operator cancels the school job while it prints
        conn.execute(text("UPDATE jobs SET status='cancelled' WHERE id=:j"), {"j": uploaded["job_id"]})
    mqtt_job_lifecycle.record_job_ended(
        printer_id=9, printer_name="Owned A1", current_job_id=print_job_id, linked_job_id=linked_job_id,
        status="completed", state=state, start_spool_weights=weights, dispatch_alert_fn=lambda **kw: None)
    monitor_db.expire_all()
    assert monitor_db.execute(text("SELECT status FROM jobs WHERE id=:j"), {"j": uploaded["job_id"]}).scalar_one() == "cancelled"
    assert monitor_db.execute(text("SELECT status FROM education_submissions WHERE id=:s"), {"s": uploaded["id"]}).scalar_one() == "printing"
    assert monitor_db.execute(text("SELECT status FROM print_jobs WHERE id=:p"), {"p": print_job_id}).scalar_one() == "completed"
    assert [args[:3] for args in published] == [(9, print_job_id, None)]
    assert monitor_db.execute(text("SELECT COUNT(*) FROM print_archives WHERE print_job_id=:p"), {"p": print_job_id}).scalar_one() == 1


def test_education_forged_observation_still_ends_nothing(admitted_monitor):
    """A token-digest mismatch is not the printer's print: no physical end is recorded."""
    from modules.organizations.education_policy import terminal_monitor_observation
    monitor_db, uploaded, reservation = admitted_monitor
    pid = monitor_db.execute(text("INSERT INTO print_jobs(printer_id,filename,job_name,started_at,status,scheduled_job_id) "
                                  "VALUES(9,:f,:f,CURRENT_TIMESTAMP,'running',:j) RETURNING id"),
                             {"f": reservation["remote_filename"], "j": uploaded["job_id"]}).scalar_one()
    result = terminal_monitor_observation(monitor_db, print_job_id=pid, printer_id=9, terminal_status="completed",
                                          observed_filename="odin-" + "e" * 32 + ".3mf")
    assert result["authorized"] is False and "physical_ended" not in result
    assert monitor_db.execute(text("SELECT status FROM print_jobs WHERE id=:p"), {"p": pid}).scalar_one() == "running"


def test_runtime_census_catches_a_product_write_outside_the_state_machine(review_db, _job_status_runtime_census):
    """Code compiled under backend/ (so its frames are product frames) bypassing the state machine."""
    violations = _job_status_runtime_census
    _job(review_db, 730, 'pending')
    planted = compile("db.execute(text(\"UPDATE jobs SET status='failed' WHERE id=730\"))\n"
                      "row = db.get(Job, 730)\nrow.status = 'cancelled'\ndb.flush()\n",
                      str(BACKEND / 'modules' / 'planted_runtime_bypass.py'), 'exec')
    from modules.jobs.models import Job
    exec(planted, {'db': review_db, 'text': text, 'Job': Job})
    review_db.rollback()
    kinds = [entry[1] for entry in violations]
    assert any(k.startswith('UPDATE jobs SET status') for k in kinds), violations
    assert any(k.startswith("Job.status = 'cancelled'") for k in kinds), violations
    assert all('planted_runtime_bypass.py' in entry[0] for entry in violations)
    violations.clear()  # the bypass was planted by this test
    _job(review_db, 731, 'pending')
    from modules.jobs.state_machine import transition_job
    assert transition_job(review_db, 731, 'cancelled', 'operator')       # the state machine is fine
    review_db.commit()
    assert violations == []


def _warm_engine(tmp_path):
    """An engine whose pooled connection exists before any per-test hook runs."""
    from sqlalchemy import create_engine
    engine = create_engine(f"sqlite:///{tmp_path / 'warm.db'}")
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE jobs (id INTEGER PRIMARY KEY, status TEXT)")
        conn.exec_driver_sql("INSERT INTO jobs VALUES (1, 'pending')")
    return engine


def test_runtime_census_traces_pooled_connections_and_detaches_at_teardown(tmp_path, _job_status_runtime_census):
    """A connection pooled before the hook started is traced on checkout; teardown removes the trace."""
    import tests.test_contracts.conftest as contract_conftest
    engine = _warm_engine(tmp_path)                       # pooled before the hook below starts
    hook = contract_conftest._job_status_runtime_census.__wrapped__(None)
    violations = next(hook)
    planted = compile("with engine.begin() as conn:\n"
                      "    conn.exec_driver_sql(\"UPDATE jobs SET status='failed' WHERE id=1\")\n",
                      str(BACKEND / 'modules' / 'planted_pooled_bypass.py'), 'exec')
    exec(planted, {'engine': engine})
    assert [entry[0].split(':')[0].endswith('planted_pooled_bypass.py') for entry in violations] == [True], violations
    violations.clear()
    with pytest.raises(StopIteration):
        next(hook)                                        # teardown: listeners and traces removed
    exec(planted, {'engine': engine})                     # no longer traced by the finished hook
    assert violations == []
    _job_status_runtime_census.clear()                    # this test's own hook saw the planted writes too
    engine.dispose()
