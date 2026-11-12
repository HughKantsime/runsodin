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
