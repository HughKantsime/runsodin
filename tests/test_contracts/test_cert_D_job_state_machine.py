"""cert-D F04-4.1: one job state machine is the only path that writes jobs.status.

1. Census (static): no module other than modules/jobs/state_machine.py contains a raw
   ``UPDATE jobs SET ... status=...`` (constants, f-strings or concatenations), an ORM
   ``<job>.status = ...`` assignment or an ``update(Job)`` statement.
2. Census (runtime): while real routes run, a SQLAlchemy cursor hook and a Job.status
   attribute hook see every jobs.status write and require it to be the state machine's.
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
_UPDATE = re.compile(r'UPDATE\s+jobs\s+SET\b', re.I)
_ORM = re.compile(r'^\s*\w*job\w*\.status\s*=(?!=)', re.M)


def _violations(source: str):
    found = []
    for match in _UPDATE.finditer(source):
        set_clause = re.split(r'\bWHERE\b', source[match.end():match.end() + 600], maxsplit=1, flags=re.I)[0]
        if re.search(r'\bstatus\s*=\s*(?!status\b)', set_clause, re.I):
            found.append(('sql', source[:match.start()].count('\n') + 1))
    for match in _ORM.finditer(source):
        line = source[:match.start()].count('\n') + 1
        if 'print_job' not in match.group(0):
            found.append(('orm', line))
    for match in re.finditer(r'\bupdate\(\s*Job\s*\)', source):
        found.append(('update(Job)', source[:match.start()].count('\n') + 1))
    for match in re.finditer(r'query\(\s*Job\s*\)[^\n]*\.update\(', source):
        found.append(('query(Job).update', source[:match.start()].count('\n') + 1))
    # Any name bound to a Job instance in the same source (whatever it is called).
    bound = set(re.findall(r'^\s*(\w+)\s*=\s*[^\n]*(?:query\(\s*Job\s*\)|\.get\(\s*Job\b|\bJob\()', source, re.M))
    bound |= set(re.findall(r'^\s*for\s+(\w+)\s+in\s+[^\n]*query\(\s*Job\s*\)', source, re.M))
    for name in bound:
        for match in re.finditer(rf'^\s*{re.escape(name)}\.status\s*=(?!=)', source, re.M):
            entry = ('orm', source[:match.start()].count('\n') + 1)
            if entry not in found:
                found.append(entry)
    return found


def test_census_no_module_outside_the_state_machine_writes_jobs_status():
    offenders = []
    for path in BACKEND.rglob('*.py'):
        rel = path.relative_to(ROOT).as_posix()
        if 'preserved027' in rel or rel == STATE_MACHINE:
            continue  # the frozen released bundle is not live code
        offenders += [(rel, kind, line) for kind, line in _violations(path.read_text())]
    assert offenders == [], offenders


@pytest.mark.parametrize('planted', [
    "cur.execute(\"UPDATE jobs SET status = 'completed' WHERE id = ?\", (1,))",
    "db.execute(text(f\"UPDATE jobs SET status = '{target}', actual_end = {sql.now()} WHERE id = :id\"))",
    "q = ('UPDATE jobs SET printer_id=NULL,' \n     ' status=:s WHERE id=:id')",
    "    job.status = JobStatus.FAILED",
    "db.execute(update(Job).where(Job.id == 1).values(status='failed'))",
    "row = db.query(Job).filter(Job.id == 1).first()\nrow.status = JobStatus.FAILED",
    "for entry in db.query(Job).all():\n    entry.status = 'cancelled'",
    "current = db.get(Job, 5)\ncurrent.status = 'printing'",
    "db.query(Job).filter(Job.id == 1).update({'status': 'failed'})",
])
def test_census_scanner_detects_each_bypass_shape(planted):
    assert _violations(planted)


def test_census_scanner_ignores_lock_noops_and_reads():
    assert not _violations("db.execute(text('UPDATE jobs SET status=status WHERE id=:id'))")
    assert not _violations("rows = db.execute(text(\"SELECT id FROM jobs WHERE status = 'pending'\"))")


@pytest.fixture
def status_write_hook(review_db):
    """Record every jobs.status write that reaches the database or the ORM during a test."""
    from modules.jobs.models import Job
    engine = review_db.get_bind()
    sql_writes, orm_writes = [], []

    def before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        if _violations(statement):  # any jobs.status write that reaches the database
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
