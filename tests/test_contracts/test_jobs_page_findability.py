"""GET /jobs findability: q search, printer/held filters, X-Total-Count, and cancelling queued work.

Regression: the Jobs page asked for /jobs with no limit, so on a farm with 250 jobs the first 100
(queue order) were all it knew about, search ran on those 100 in the browser, and job numbers
appeared nowhere. Search is now a server predicate layered on the list's visibility rules.
"""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import Response
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.requests import Request

from modules.organizations.routes_controlled_parts import change_mark
from tests.test_contracts.test_controlled_parts import actor, database, mark  # noqa: F401
from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401
from tests.test_contracts.test_product_bom_integrity import bom_client  # noqa: F401

OPERATOR = {'id': 3, 'role': 'operator', 'group_id': 1, 'is_active': True}
ACTIVE = ['submitted', 'rejected', 'pending', 'scheduled', 'printing', 'paused']
HISTORY = ['completed', 'failed', 'cancelled']


def stamp(moment):
    return moment.strftime('%Y-%m-%d %H:%M:%S')


def insert(connection, identifier, status, name, *, created, printer=None, hold=0, actual_end=None, order_item=None):
    connection.execute(text(
        "INSERT INTO jobs(id,item_name,status,charged_to_org_id,quantity,hold,is_locked,priority,created_at,updated_at,"
        "actual_end,printer_id,order_item_id) VALUES(:id,:name,:status,1,1,:hold,0,3,:created,:created,:end,:printer,:item)"),
        dict(id=identifier, name=name, status=status, hold=hold, created=stamp(created),
             end=stamp(actual_end) if actual_end else None, printer=printer, item=order_item))


@pytest.fixture
def farm(bom_client, review_db, monkeypatch):  # noqa: F811
    """130 pending + 2 held + 3 submitted, 150 finished, and a few jobs with names worth searching."""
    now = datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None)
    from core.registry import registry
    from modules.organizations.services import EducationPolicyService
    from modules.jobs.routes import jobs_crud
    monkeypatch.setitem(registry._providers, 'EducationPolicyProvider', EducationPolicyService())
    bom_client.app.include_router(jobs_crud.router)
    with review_db.get_bind().begin() as c:
        for index in range(130):
            insert(c, 7000 + index, 'pending', 'Bulk filler', created=now - timedelta(hours=1, minutes=index))
        for index in range(2):
            insert(c, 7200 + index, 'pending', 'Bulk held filler', created=now - timedelta(minutes=index), hold=1)
        for index in range(3):
            insert(c, 7250 + index, 'submitted', 'Bulk approval filler', created=now - timedelta(minutes=index))
        for index in range(150):
            insert(c, 8000 + index, 'completed', 'Bulk finished filler', created=now - timedelta(days=30, minutes=index),
                   actual_end=now - timedelta(days=10, minutes=index))
        # Names carry no digits, so a number in q can only mean a job id (or an order number).
        insert(c, 218, 'pending', 'Bracket Alpha', created=now - timedelta(minutes=5), printer=9)
        insert(c, 2180, 'pending', 'Bracket Beta', created=now - timedelta(minutes=6))
        c.execute(text("UPDATE printers SET nickname='Workhorse' WHERE id=9"))
        c.execute(text("INSERT INTO orders(id,order_number,status,org_id) VALUES(31,'ETSY-4417','pending',1)"))
        c.execute(text("INSERT INTO order_items(id,order_id,product_id,quantity) VALUES(31,31,101,1)"))
        insert(c, 2181, 'pending', 'Gadget Gamma', created=now - timedelta(minutes=7), order_item=31)
        insert(c, 2182, 'pending', '100% PLA_mix', created=now - timedelta(minutes=8))
        insert(c, 2183, 'pending', 'Plain', created=now - timedelta(minutes=9))
        insert(c, 7300, 'pending', 'Cancel me pending', created=now)
        insert(c, 7301, 'submitted', 'Cancel me submitted', created=now)
        insert(c, 7302, 'printing', 'Do not cancel printing', created=now, printer=9)
        insert(c, 7303, 'submitted', 'Bulk cancel me', created=now)
    return bom_client


def ids(response):
    assert response.status_code == 200, response.text
    return [job['id'] for job in response.json()]


def total(response):
    return int(response.headers['X-Total-Count'])


# ---- Default view, paging, total ----

def test_active_view_returns_every_active_job_beyond_100(farm):
    response = farm.get('/jobs', params=[('status', s) for s in ACTIVE] + [('limit', 500)])
    got = set(ids(response))
    assert set(range(7000, 7130)) | {7200, 7201, 7250, 7251, 7252} <= got  # 135+ active jobs, all of them
    assert not any(8000 <= identifier < 8150 for identifier in got)  # no finished job in the queue view
    assert total(response) == len(response.json()) >= 135


def test_history_is_newest_first_and_pages_with_a_total(farm):
    params = [('status', s) for s in HISTORY] + [('order', 'recent'), ('limit', 50)]
    first = farm.get('/jobs', params=params + [('offset', 0)])
    second = farm.get('/jobs', params=params + [('offset', 50)])
    one, two = ids(first), ids(second)
    assert len(one) == 50 and len(two) == 50 and not set(one) & set(two)
    assert total(first) == total(second) >= 150
    synthetic = [identifier for identifier in one + two if 8000 <= identifier < 8150]
    assert synthetic == sorted(synthetic)  # 8000 ended most recently, so it comes first


def test_a_cut_off_list_says_how_long_it_really_is(farm):
    response = farm.get('/jobs', params={'status': 'pending', 'limit': 10})
    assert len(response.json()) == 10
    assert total(response) >= 130  # the page shows "Showing 10 of N" from this


def test_calls_without_new_params_keep_the_old_shape(farm):
    response = farm.get('/jobs')
    body = response.json()
    assert isinstance(body, list) and len(body) == 100  # same default limit, same plain list
    assert {'id', 'item_name', 'status'} <= set(body[0])
    assert total(response) > len(body)
    assert ids(response) == ids(farm.get('/jobs', params={'order': 'queue'}))


# ---- Search ----

@pytest.mark.parametrize('q', ['218', '#218', 'job 218', 'Job #218', '  #218  '])
def test_job_number_forms_find_exactly_that_job(farm, q):
    assert ids(farm.get('/jobs', params={'q': q})) == [218]  # not 2180


def test_hash_and_job_forms_match_the_id_only(farm):
    # "#4417" is a job number; the order ETSY-4417 is found by its text.
    assert ids(farm.get('/jobs', params={'q': '#4417'})) == []
    assert ids(farm.get('/jobs', params={'q': 'etsy-4417'})) == [2181]


def test_a_bare_number_also_matches_an_order_number_as_text(farm):
    assert ids(farm.get('/jobs', params={'q': '4417'})) == [2181]


def test_name_search_is_case_insensitive_substring(farm):
    assert set(ids(farm.get('/jobs', params={'q': 'bracket'}))) == {218, 2180}
    assert ids(farm.get('/jobs', params={'q': 'BRACKET ALPHA'})) == [218]
    assert ids(farm.get('/jobs', params={'q': 'acket alp'})) == [218]


def test_printer_name_and_nickname_search(farm):
    assert 218 in ids(farm.get('/jobs', params={'q': 'p1s', 'limit': 500}))  # printer 9 is "Test P1S"
    assert 218 in ids(farm.get('/jobs', params={'q': 'WORKHORSE'}))
    assert 2180 not in ids(farm.get('/jobs', params={'q': 'workhorse'}))  # no printer


def test_order_number_search(farm):
    assert ids(farm.get('/jobs', params={'q': 'ETSY-44'})) == [2181]


def test_like_wildcards_in_q_are_literal(farm):
    for q in ('%', '_', '100%', 'PLA_'):
        found = ids(farm.get('/jobs', params={'q': q, 'limit': 500}))
        assert 2182 in found, q
        assert 2183 not in found and 218 not in found, q  # an unescaped % or _ would match them


def test_q_is_bound_not_interpolated(farm):
    response = farm.get('/jobs', params={'q': "x') OR 1=1 --"})
    assert ids(response) == []


def test_blank_q_is_no_filter(farm):
    assert total(farm.get('/jobs', params={'q': '   '})) == total(farm.get('/jobs'))


# ---- Visibility: search never matches what the caller cannot see ----

@pytest.fixture
def marked(database, tier_license):  # noqa: F811
    tier_license('enterprise')
    change_mark(database, 'files', 1, mark(), actor())  # job 1 ("Part", file secret.3mf) is controlled
    return database


def search(db, user, q):
    from modules.jobs.routes.jobs_crud import list_jobs
    out = Response()
    rows = list_jobs(status=None, printer_id=None, finished_since=None, order='queue', org_id=None, limit=100, offset=0,
                     q=q, held=None, unassigned=False, response=out, current_user=user, _agent_scope={}, db=db)
    return [row.id for row in rows], int(out.headers['X-Total-Count'])


@pytest.mark.parametrize('q', ['Part', 'part', 'PAR', '1', '#1', 'job 1'])
def test_a_controlled_job_is_not_found_by_name_or_number_for_a_non_audience_caller(marked, q):
    # actor(2) is outside the audience. By name or by id the answer is the same empty result,
    # so a search cannot tell a hidden job from a missing one.
    assert search(marked, actor(2), q) == ([], 0)


def test_the_controlled_job_is_found_by_an_audience_member(marked):
    assert search(marked, actor(1), 'Part')[0] == [1]
    assert search(marked, actor(1), '#1')[0] == [1]


def test_uncontrolled_jobs_are_still_found_with_controlled_data_present(marked):
    assert search(marked, actor(2), 'public')[0] == [2]


def test_a_model_name_from_another_org_is_not_matched(marked):
    marked.execute(text("INSERT INTO models(id,name,org_id) VALUES(5,'Foreign secret name',2)"))
    marked.execute(text("INSERT INTO jobs(id,item_name,model_id,status,charged_to_org_id) VALUES(3,'Local label',5,'pending',1)"))
    marked.commit()
    assert search(marked, actor(1), 'foreign secret')[0] == []  # org 1 cannot see an org 2 model's name
    assert search(marked, actor(1), 'local label')[0] == [3]
    assert search(marked, actor(2), 'foreign secret')[0] == [3]  # a superadmin can


# ---- Filters ----

def test_printer_filter_and_unassigned(farm):
    on_nine = ids(farm.get('/jobs', params={'printer_id': 9, 'limit': 500}))
    assert 218 in on_nine and 7000 not in on_nine
    unassigned = ids(farm.get('/jobs', params={'unassigned': 'true', 'status': 'pending', 'limit': 500}))
    assert 7000 in unassigned and 218 not in unassigned


def test_held_filter(farm):
    held = ids(farm.get('/jobs', params={'held': 'true', 'limit': 500}))
    assert {7200, 7201} <= set(held) and 7000 not in held
    not_held = ids(farm.get('/jobs', params={'held': 'false', 'limit': 500}))
    assert 7000 in not_held and not {7200, 7201} & set(not_held)


def test_filters_combine_with_search(farm):
    assert ids(farm.get('/jobs', params={'q': 'bracket', 'printer_id': 9})) == [218]
    assert ids(farm.get('/jobs', params={'q': 'bracket', 'unassigned': 'true'})) == [2180]


# ---- Cancel from the row: same transition as bulk cancel ----

def cancel(db, job_id):
    from modules.jobs.routes import jobs_agent
    request = Request({'type': 'http', 'method': 'POST', 'path': f'/api/jobs/{job_id}/cancel', 'headers': [],
                       'path_params': {'job_id': job_id}})
    return jobs_agent.cancel_job(job_id=job_id, request=request, current_user=OPERATOR, _agent_scope={}, db=db)


@pytest.mark.parametrize('job_id', [7300, 7301])
def test_row_cancel_works_for_pending_and_submitted(farm, review_db, job_id):  # noqa: F811
    with Session(review_db.get_bind()) as db:
        assert cancel(db, job_id)['status'] == 'cancelled'
    with Session(review_db.get_bind()) as db:
        assert db.execute(text('SELECT status FROM jobs WHERE id=:id'), {'id': job_id}).scalar_one() == 'cancelled'


def test_row_cancel_still_refuses_a_printing_job(farm, review_db):  # noqa: F811
    from core.errors import OdinError
    with Session(review_db.get_bind()) as db, pytest.raises(OdinError):
        cancel(db, 7302)
    with Session(review_db.get_bind()) as db:
        assert db.execute(text('SELECT status FROM jobs WHERE id=7302')).scalar_one() == 'printing'


def test_bulk_cancel_and_row_cancel_reach_the_same_state(farm, review_db):  # noqa: F811
    from modules.jobs.bulk_actions import apply_bulk_action
    with Session(review_db.get_bind()) as db:
        assert apply_bulk_action(db, OPERATOR, 'cancel', [7303], [7303], 3) == 1
        db.commit()
        assert db.execute(text('SELECT status FROM jobs WHERE id=7303')).scalar_one() == 'cancelled'
