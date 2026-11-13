"""Job datetimes: one wire form for the apps, one storage form from Moonraker.

Regression: a finished Kobra/Moonraker print stored its end time with a timezone, the API
sent "…T…Z" for it, and the iPhone/iPad Queue decoder (no-zone ISO only) failed the whole list.
"""
import logging
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_product_bom_integrity import bom_client

WIRE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d$")
STORED = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d(\.\d{1,6})?$")  # naive UTC, no zone; fractions kept
JOB_DATETIMES = ('scheduled_start', 'scheduled_end', 'actual_start', 'actual_end',
                 'due_date', 'approved_at', 'created_at', 'updated_at')

INSTANT = '2026-11-08T04:41:45'
STORED_VARIANTS = {
    'space': '2026-11-08 04:41:45',
    'iso': '2026-11-08T04:41:45',
    'offset': '2026-11-08T04:41:45+00:00',
    'zulu': '2026-11-08T04:41:45Z',
    'fraction': '2026-11-08T04:41:45.599830',
    'fraction_offset': '2026-11-08T04:41:45.599830+00:00',
    'fraction_zulu': '2026-11-08T04:41:45.599830Z',
}


# ---------- helper unit contracts ----------

@pytest.mark.parametrize('stored', list(STORED_VARIANTS.values()) + [
    '2026-11-08 04:41:45.599830+00:00',
    '2026-11-08T04:41:45.5',                      # short fraction
    '2026-11-08T06:41:45+02:00',                  # another zone converts to UTC
    datetime(2026, 11, 8, 4, 41, 45, 599830, tzinfo=timezone.utc),
    datetime(2026, 11, 8, 4, 41, 45, 599830),     # naive means UTC
    datetime(2026, 11, 8, 6, 41, 45, tzinfo=timezone(timedelta(hours=2))),
])
def test_to_wire_normalizes_every_stored_variant(stored):
    from core.datetime_wire import to_wire
    assert to_wire(stored) == INSTANT


@pytest.mark.parametrize('empty', [None, '', '   '])
def test_to_wire_keeps_missing_values_null(empty):
    from core.datetime_wire import to_wire
    assert to_wire(empty) is None


def test_to_wire_unparseable_is_null_and_logged_once(caplog, monkeypatch):
    from core import datetime_wire
    monkeypatch.setattr(datetime_wire, '_reported', set())
    with caplog.at_level(logging.WARNING, logger='odin.datetime_wire'):
        assert datetime_wire.to_wire('not a date') is None
        assert datetime_wire.to_wire('not a date') is None   # same value: no second log
        assert datetime_wire.to_wire(12345) is None           # different value: logged
    records = [r for r in caplog.records if r.name == 'odin.datetime_wire']
    assert len(records) == 2


def test_to_storage_utc_is_naive_utc_without_fraction():
    from core.datetime_wire import to_storage_utc
    assert to_storage_utc(datetime(2026, 11, 8, 4, 41, 45, 599830, tzinfo=timezone.utc)) == '2026-11-08 04:41:45.599830'
    assert to_storage_utc(datetime(2026, 11, 8, 6, 41, 45, tzinfo=timezone(timedelta(hours=2)))) == '2026-11-08 04:41:45'
    assert to_storage_utc(datetime(2026, 11, 8, 4, 41, 45)) == '2026-11-08 04:41:45'
    assert to_storage_utc('2026-11-08T04:41:45.599830Z') == '2026-11-08 04:41:45.599830'
    assert to_storage_utc(None) is None
    with pytest.raises(ValueError):
        to_storage_utc('not a date')


# ---------- Moonraker writers ----------

def test_terminal_history_reports_the_storage_form():
    from modules.printers.monitors.moonraker_history import terminal_history
    end = datetime.now(timezone.utc)
    printer = SimpleNamespace(get_job_history=lambda limit: [dict(
        filename='part.gcode', status='completed', start_time=(end - timedelta(hours=1)).timestamp(),
        end_time=end.timestamp(), print_duration=3500)])
    history = terminal_history(printer, 'part.gcode', end.timestamp())
    assert STORED.match(history['started_at']) and STORED.match(history['ended_at'])


def _moonraker(review_db, monkeypatch):
    from modules.printers.monitors import moonraker_monitor as moon
    from modules.archives import archive
    from core.registry import registry
    import modules.notifications.event_dispatcher as events
    import modules.printers.committed_terminal_events as committed
    monkeypatch.setattr(moon, 'engine', review_db.get_bind())
    monkeypatch.setattr(registry, 'get_provider', lambda name: None)
    monkeypatch.setattr(archive, 'create_print_archive', lambda **kwargs: None)
    monkeypatch.setattr(events, 'dispatch_alert', lambda **kwargs: None)
    monkeypatch.setattr(events, 'record_error', lambda **kwargs: None)
    monkeypatch.setattr(events, 'increment_care_counters', lambda *args, **kwargs: None)
    monkeypatch.setattr(committed, 'publish_committed_completion', lambda *args, **kwargs: None)
    return moon


@pytest.mark.parametrize('with_history', [False, True])
def test_moonraker_completion_writes_the_naive_form(review_db, monkeypatch, with_history):
    from modules.printers.monitors.moonraker_history import terminal_history
    moon = _moonraker(review_db, monkeypatch)
    start = datetime.now(timezone.utc) - timedelta(hours=1)
    end = datetime.now(timezone.utc)
    # A legacy row whose start carries a zone: reading it must still work, and it is not rewritten.
    row_start = '2026-03-14 00:00:00' if with_history else start.isoformat()
    job_id = review_db.execute(text("INSERT INTO jobs(item_name,status,charged_to_org_id,quantity,hold,is_locked) "
                                    "VALUES('Synthetic moonraker job','printing',1,1,0,1) RETURNING id")).scalar_one()
    print_id = review_db.execute(text("INSERT INTO print_jobs(printer_id,filename,job_name,status,started_at,scheduled_job_id) "
                                      "VALUES(9,'part.gcode','part.gcode','running',:start,:job) RETURNING id"),
                                 {'start': row_start, 'job': job_id}).scalar_one()
    review_db.commit()
    status = SimpleNamespace(filename='part.gcode', raw_print_state='standby', error_message='', filament_used_mm=0)
    monitor = moon.MoonrakerMonitor(9, 'Owned', '127.0.0.1')
    monitor._current_job_db_id = print_id
    monitor._last_filename = 'part.gcode'
    history = None
    if with_history:
        printer = SimpleNamespace(get_job_history=lambda limit: [dict(
            filename='part.gcode', status='completed', start_time=start.timestamp(),
            end_time=end.timestamp(), print_duration=3500)])
        history = terminal_history(printer, 'part.gcode', end.timestamp())
        assert history is not None
    monitor._job_ended('completed', status, history=history)
    with review_db.get_bind().connect() as connection:
        printed = connection.execute(text('SELECT status,started_at,ended_at FROM print_jobs WHERE id=:id'),
                                     {'id': print_id}).one()
        job = connection.execute(text('SELECT status,actual_end FROM jobs WHERE id=:id'), {'id': job_id}).one()
    assert printed.status == 'completed' and job.status == 'completed'
    assert STORED.match(printed.ended_at), printed.ended_at
    assert STORED.match(job.actual_end), job.actual_end
    if with_history:
        assert STORED.match(printed.started_at), printed.started_at
    else:
        assert printed.started_at == row_start  # existing rows are never rewritten


# ---------- GET /jobs contract ----------

def _insert(connection, identifier, **stamps):
    values = {name: '2026-11-08 03:10:02' for name in JOB_DATETIMES}
    values.update(stamps)
    connection.execute(text(
        "INSERT INTO jobs(id,item_name,status,charged_to_org_id,quantity,hold,is_locked,printer_id,"
        + ",".join(JOB_DATETIMES) + ") VALUES(:id,:name,'completed',1,1,0,1,9,"
        + ",".join(':' + name for name in JOB_DATETIMES) + ")"),
        dict(id=identifier, name=f'Synthetic wire job {identifier}', **values))


@pytest.fixture
def wire_jobs(bom_client, review_db, monkeypatch):
    from core.registry import registry
    from modules.organizations.services import EducationPolicyService
    from modules.jobs.routes import jobs_crud
    monkeypatch.setitem(registry._providers, 'EducationPolicyProvider', EducationPolicyService())
    bom_client.app.include_router(jobs_crud.router)
    with review_db.get_bind().begin() as connection:
        for index, stored in enumerate(STORED_VARIANTS.values()):
            _insert(connection, 300 + index, **{name: stored for name in JOB_DATETIMES})
        # The two real rows from the bug: Kobra S1 over Moonraker, end time stored with a zone.
        _insert(connection, 131, actual_end='2026-11-08T04:41:45.599830Z')
        _insert(connection, 192, actual_end='2026-11-09 01:02:03.123456+00:00')
        connection.execute(text("INSERT INTO jobs(item_name,status,charged_to_org_id,quantity,hold,is_locked) "
                                "VALUES('Synthetic job with no times','pending',1,1,0,0)"))
    return bom_client


def test_jobs_list_sends_one_datetime_form(wire_jobs, review_db):
    response = wire_jobs.get('/jobs')
    assert response.status_code == 200, response.text
    jobs = {job['id']: job for job in response.json()}
    assert {131, 192} <= set(jobs)
    for job in jobs.values():
        for name in JOB_DATETIMES:
            value = job[name]
            assert value is None or WIRE.match(value), (job['id'], name, value)
    assert jobs[131]['actual_end'] == '2026-11-08T04:41:45'
    assert jobs[192]['actual_end'] == '2026-11-09T01:02:03'
    for index in range(len(STORED_VARIANTS)):
        assert all(jobs[300 + index][name] == INSTANT for name in JOB_DATETIMES), jobs[300 + index]
    # Read-time only: the stored rows are untouched.
    with review_db.get_bind().connect() as connection:
        assert connection.execute(text('SELECT actual_end FROM jobs WHERE id=131')).scalar_one() == '2026-11-08T04:41:45.599830Z'


@pytest.mark.parametrize('identifier', [131, 192, 303])
def test_job_detail_sends_one_datetime_form(wire_jobs, identifier):
    response = wire_jobs.get(f'/jobs/{identifier}')
    assert response.status_code == 200, response.text
    body = response.json()
    for name in JOB_DATETIMES:
        assert body[name] is None or WIRE.match(body[name]), (name, body[name])
    assert WIRE.match(body['actual_end'])


def test_print_history_duration_across_the_upgrade(bom_client, review_db):
    """A Moonraker print that started before 2.1.1 (zoned start) and ended after it (naive end)."""
    from modules.jobs.routes import presets
    bom_client.app.include_router(presets.router)
    with review_db.get_bind().begin() as connection:
        connection.execute(text(
            "INSERT INTO print_jobs(id,printer_id,job_name,filename,status,started_at,ended_at) "
            "VALUES(9101,9,'Synthetic upgrade print','upgrade.gcode','completed',"
            "'2026-11-11T10:00:00+00:00','2026-11-11 11:30:00')"))
    response = bom_client.get('/print-jobs', params={'job_id': 9101})
    assert response.status_code == 200, response.text
    rows = response.json()
    assert len(rows) == 1 and rows[0]['duration_minutes'] == 90.0
