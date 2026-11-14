"""GET /jobs: several statuses, finished_since and order=recent (roadmap "iOS queue accuracy").

Regression: the apps called GET /jobs with no filter. The default limit is 100 in queue order,
so a farm with 210 jobs showed 4 of its 12 pending jobs, and "Completed Today" was every
terminal job in that first page, not today's.
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401
from tests.test_contracts.test_product_bom_integrity import bom_client  # noqa: F401



def stamp(moment):
    return moment.strftime('%Y-%m-%d %H:%M:%S')


def _insert(connection, identifier, status, *, created, actual_end=None):
    connection.execute(text(
        "INSERT INTO jobs(id,item_name,status,charged_to_org_id,quantity,hold,is_locked,priority,created_at,updated_at,actual_end) "
        "VALUES(:id,:name,:status,1,1,0,0,3,:created,:created,:end)"),
        dict(id=identifier, name=f'Synthetic list job {identifier}', status=status,
             created=stamp(created), end=stamp(actual_end) if actual_end else None))


@pytest.fixture
def many_jobs(bom_client, review_db, monkeypatch):  # noqa: F811
    """150 old completed jobs ahead of 12 pending and 3 scheduled ones, and 4 finished today."""
    NOW = datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None)  # at fixture time, not import time
    from core.registry import registry
    from modules.organizations.services import EducationPolicyService
    from modules.jobs.routes import jobs_crud
    monkeypatch.setitem(registry._providers, 'EducationPolicyProvider', EducationPolicyService())
    bom_client.app.include_router(jobs_crud.router)
    with review_db.get_bind().begin() as connection:
        for index in range(150):
            _insert(connection, 5000 + index, 'completed', created=NOW - timedelta(days=30, minutes=index),
                    actual_end=NOW - timedelta(days=29, minutes=index))
        for index in range(12):
            _insert(connection, 6000 + index, 'pending', created=NOW - timedelta(hours=1, minutes=index))
        for index in range(3):
            _insert(connection, 6100 + index, 'scheduled', created=NOW - timedelta(hours=2, minutes=index))
        for index, status in enumerate(['completed', 'completed', 'failed', 'cancelled']):
            _insert(connection, 6200 + index, status, created=NOW - timedelta(hours=5),
                    actual_end=NOW - timedelta(minutes=30 + index))
        # Cancelled while still queued today (no actual_end), and one cancelled last month.
        _insert(connection, 6300, 'cancelled', created=NOW - timedelta(minutes=20))
        _insert(connection, 6301, 'cancelled', created=NOW - timedelta(days=30))
    return bom_client


def ids(response):
    assert response.status_code == 200, response.text
    return [job['id'] for job in response.json()]


def test_active_statuses_are_not_cut_off_by_old_jobs(many_jobs):
    active = ids(many_jobs.get('/jobs', params=[('status', 'pending'), ('status', 'scheduled')]))
    assert set(range(6000, 6012)) | set(range(6100, 6103)) <= set(active)
    assert not any(5000 <= identifier < 5150 for identifier in active)  # no old completed job


def test_a_single_status_still_works(many_jobs):
    assert set(ids(many_jobs.get('/jobs', params={'status': 'scheduled'}))) >= {6100, 6101, 6102}


def test_finished_since_returns_todays_ends_newest_first(many_jobs):
    since = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    today = ids(many_jobs.get('/jobs', params={'finished_since': since, 'order': 'recent'}))
    synthetic = [identifier for identifier in today if 6200 <= identifier < 6204]
    assert synthetic == [6200, 6201, 6202, 6203]  # 30, 31, 32, 33 minutes ago: newest first
    assert not any(5000 <= identifier < 5150 for identifier in today)  # last month's ends are excluded


def test_finished_since_accepts_a_zoned_time(many_jobs):
    eastern = timezone(timedelta(hours=-5))
    since = (datetime.now(timezone.utc) - timedelta(hours=3)).astimezone(eastern).isoformat()
    assert {6200, 6201, 6202, 6203} <= set(ids(many_jobs.get('/jobs', params={'finished_since': since})))


def test_default_order_is_unchanged(many_jobs):
    # Queue order (priority, position, age) stays the default; recent is opt-in.
    default = ids(many_jobs.get('/jobs', params={'limit': 500}))
    assert default == ids(many_jobs.get('/jobs', params={'limit': 500, 'order': 'queue'}))
    synthetic = [identifier for identifier in default if 5000 <= identifier < 5150]
    assert synthetic[0] == 5149  # oldest created first among equal priorities


def test_an_unknown_order_is_refused(many_jobs):
    assert many_jobs.get('/jobs', params={'order': 'oldest'}).status_code == 422


def test_finished_since_includes_a_job_cancelled_before_it_started(many_jobs):
    since = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    today = ids(many_jobs.get('/jobs', params={'finished_since': since, 'order': 'recent'}))
    assert 6300 in today and 6301 not in today
    assert not set(range(6000, 6012)) & set(today)  # still-pending jobs never count as finished


def test_a_cancellation_dates_a_job_that_never_started(many_jobs, review_db):  # noqa: F811
    """Regression (Codex sign-off 11-12): the state machine's raw UPDATE left updated_at alone, so a
    job created days ago and cancelled today was missing from finished_since=today."""
    from modules.jobs.state_machine import transition_job
    old = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=2)
    with review_db.get_bind().begin() as connection:
        _insert(connection, 6400, 'pending', created=old)
        _insert(connection, 6401, 'submitted', created=old)
        assert transition_job(connection, 6400, 'cancelled', 'operator')
        assert transition_job(connection, 6401, 'rejected', 'approval')
    since = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    today = ids(many_jobs.get('/jobs', params={'finished_since': since, 'order': 'recent', 'limit': 500}))
    assert 6400 in today and 6401 in today


def test_recent_order_and_finished_since_treat_older_iso_times_and_naive_times_as_the_same_clock(many_jobs, review_db):
    # Rows written by older versions hold ISO times with a 'T' and an offset; newer ones hold naive
    # UTC with a space. As text a 'T' sorts after a space, so an older ISO row of the same day
    # ranked above a newer naive one, and passed a finished_since it was actually before.
    now = datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None)
    iso = lambda moment: moment.strftime('%Y-%m-%dT%H:%M:%S') + '+00:00'
    with review_db.get_bind().begin() as connection:
        for identifier, end in ((7001, iso(now - timedelta(minutes=10))),                    # older, ISO shape
                                (7002, stamp(now - timedelta(minutes=5))),                   # newest, naive shape
                                (7003, iso(now - timedelta(hours=6)))):                      # before the window, ISO
            connection.execute(text(
                "INSERT INTO jobs(id,item_name,status,charged_to_org_id,quantity,hold,is_locked,priority,created_at,updated_at,actual_end) "
                "VALUES(:id,'Mixed time job','completed',1,1,0,0,3,:created,:created,:end)"),
                dict(id=identifier, created=stamp(now - timedelta(days=1)), end=end))
    mine = [i for i in ids(many_jobs.get('/jobs', params={'order': 'recent', 'limit': 100})) if i in (7001, 7002, 7003)]
    assert mine == [7002, 7001, 7003]
    since = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    today = ids(many_jobs.get('/jobs', params={'finished_since': since, 'order': 'recent'}))
    assert [i for i in today if i in (7001, 7002)] == [7002, 7001] and 7003 not in today


def test_recent_order_and_finished_since_keep_fractions_of_a_second(many_jobs, review_db):
    # Operator actions and Moonraker store microseconds. Two jobs ending in the same second must
    # still come newest first, and a cut-off inside that second must split them.
    now = datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None)
    second = now - timedelta(minutes=2)
    shapes = {7101: second.strftime('%Y-%m-%d %H:%M:%S') + '.900000',                 # newest, naive
              7102: second.strftime('%Y-%m-%dT%H:%M:%S') + '.100000+00:00',           # older, ISO
              7103: second.strftime('%Y-%m-%d %H:%M:%S')}                             # oldest, no fraction
    with review_db.get_bind().begin() as connection:
        for identifier, end in shapes.items():
            connection.execute(text(
                "INSERT INTO jobs(id,item_name,status,charged_to_org_id,quantity,hold,is_locked,priority,created_at,updated_at,actual_end) "
                "VALUES(:id,'Same second job','completed',1,1,0,0,3,:created,:created,:end)"),
                dict(id=identifier, created=stamp(now - timedelta(days=1)), end=end))
    mine = [i for i in ids(many_jobs.get('/jobs', params={'order': 'recent', 'limit': 100})) if i in shapes]
    assert mine == [7101, 7102, 7103]
    cutoff = second.replace(microsecond=500000, tzinfo=timezone.utc).isoformat()
    after = [i for i in ids(many_jobs.get('/jobs', params={'finished_since': cutoff, 'order': 'recent'})) if i in shapes]
    assert after == [7101]


def test_a_stored_time_with_an_offset_is_ordered_by_its_utc_instant(many_jobs, review_db):
    now = datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None)
    with review_db.get_bind().begin() as connection:
        for identifier, end in ((7201, stamp(now - timedelta(minutes=8))),
                                (7202, (now - timedelta(minutes=9) + timedelta(hours=2)).strftime('%Y-%m-%dT%H:%M:%S') + '+02:00')):
            connection.execute(text(
                "INSERT INTO jobs(id,item_name,status,charged_to_org_id,quantity,hold,is_locked,priority,created_at,updated_at,actual_end) "
                "VALUES(:id,'Offset job','completed',1,1,0,0,3,:created,:created,:end)"),
                dict(id=identifier, created=stamp(now - timedelta(days=1)), end=end))
    mine = [i for i in ids(many_jobs.get('/jobs', params={'order': 'recent', 'limit': 100})) if i in (7201, 7202)]
    assert mine == [7201, 7202]
