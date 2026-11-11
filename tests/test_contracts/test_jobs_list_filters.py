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

NOW = datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None)


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
    return bom_client


def ids(response):
    assert response.status_code == 200, response.text
    return [job['id'] for job in response.json()]


def test_active_statuses_are_not_cut_off_by_old_jobs(many_jobs):
    active = ids(many_jobs.get('/jobs', params=[('status', 'pending'), ('status', 'scheduled')]))
    assert set(range(6000, 6012)) | set(range(6100, 6103)) <= set(active)
    assert all(identifier < 7000 and identifier >= 6000 for identifier in active)


def test_a_single_status_still_works(many_jobs):
    assert set(ids(many_jobs.get('/jobs', params={'status': 'scheduled'}))) >= {6100, 6101, 6102}


def test_finished_since_returns_todays_ends_newest_first(many_jobs):
    since = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    today = ids(many_jobs.get('/jobs', params={'finished_since': since, 'order': 'recent'}))
    assert today[:4] == [6200, 6201, 6202, 6203]  # 30, 31, 32, 33 minutes ago: newest first
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
