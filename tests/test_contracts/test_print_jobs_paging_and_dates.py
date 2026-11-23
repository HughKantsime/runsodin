"""Date ranges and stable paging through the authenticated print history routes."""
import pytest
from sqlalchemy import text
from tests.test_contracts.test_api_token_delegation_behavior import token_client, review_db, interactive_headers


@pytest.fixture
def history_client(token_client, review_db):
    from modules.jobs.routes import presets
    client, _ = token_client
    client.app.include_router(presets.router)
    rows = [
        (901, '2026-11-01 00:00:00', '2026-11-01 01:00:00', 'completed'),
        (902, '2026-11-01T01:00:00Z', '2026-11-01T02:00:00Z', 'completed'),
        (903, '2026-11-01T05:00:00+03:00', '2026-11-01T06:00:00+03:00', 'failed'),
        (904, '2026-11-01 02:00:00', '2026-11-01 04:00:00', 'completed'),
        (905, '2026-11-01T03:00:00', None, 'running'),
    ]
    for identifier, start, end, status in rows:
        review_db.execute(text("INSERT INTO print_jobs(id,printer_id,job_name,status,started_at,ended_at) "
                               "VALUES(:id,9,'Example.gcode',:status,:start,:end)"),
                          dict(id=identifier, status=status, start=start, end=end))
    review_db.commit()
    return client


def read(client, **params):
    response = client.get('/print-jobs', params=params, headers=interactive_headers())
    assert response.status_code == 200, response.text
    return response


@pytest.mark.parametrize('params,expected', [
    ({'started_after':'2026-11-01T02:00:00Z'}, [905,904,903]),
    ({'started_before':'2026-11-01T02:00:00Z'}, [902,901]),
    ({'ended_after':'2026-11-01T03:00:00Z'}, [904,903]),
    ({'ended_before':'2026-11-01T03:00:00Z'}, [902,901]),
    ({'started_after':'2026-11-01', 'started_before':'2026-11-02',
      'ended_after':'2026-11-01T02:00:00', 'ended_before':'2026-11-01T04:00:00'}, [903,902]),
    ({'started_after':'2026-11-01T05:00:00+03:00', 'started_before':'2026-11-01T03:00:00Z'}, [904,903]),
    ({'ended_after':'2026-11-01'}, [904,903,902,901]),
    ({'ended_before':'2026-11-02'}, [904,903,902,901]),
    ({'started_after':'2026-11-01', 'started_before':'2026-11-01'}, []),
    ({'ended_after':'2026-11-01', 'ended_before':'2026-11-01'}, []),
    ({'status':'completed'}, [904,902,901]),
    ({'status':'Completed'}, []),
    ({'printer_id':10}, []),
    ({'job_id':903}, [903]),
])
def test_filters_by_instant_and_half_open_edges(history_client, params, expected):
    response = read(history_client, **params)
    assert [row['id'] for row in response.json()] == expected
    assert response.headers['X-Total-Count'] == str(len(expected))


def test_days_requires_an_explicit_date_filter(history_client):
    response = history_client.get('/print-jobs', params={'days': 7}, headers=interactive_headers())
    assert response.status_code == 422 and 'ended_after' in response.json()['detail']


def test_pages_are_stable_and_total_is_before_paging(history_client):
    pages = [read(history_client, limit=2, offset=offset) for offset in (0,2,4,6)]
    assert [[row['id'] for row in page.json()] for page in pages] == [[905,904],[903,902],[901],[]]
    assert all(page.headers['X-Total-Count'] == '5' for page in pages)
    assert len(read(history_client, limit=1000).json()) == 5
    filtered = read(history_client, ended_after='2026-11-01', limit=1, offset=1)
    assert filtered.headers['X-Total-Count'] == '4' and filtered.json()[0]['id'] == 903


@pytest.mark.parametrize('name', ['started_after','started_before','ended_after','ended_before'])
def test_bad_date_refusal(history_client, name):
    response = history_client.get('/print-jobs', params={name:'not-a-date'}, headers=interactive_headers())
    assert response.status_code == 422
    assert response.json()['detail'] == f'{name} must be a date or date-time, for example 2026-11-01 or 2026-11-01T00:00:00Z.'


@pytest.mark.parametrize('prefix', ['started','ended'])
def test_reversed_range_refusal(history_client, prefix):
    response = history_client.get('/print-jobs', params={prefix+'_after':'2026-11-02', prefix+'_before':'2026-11-01'}, headers=interactive_headers())
    assert response.status_code == 422
    assert response.json()['detail'] == f'{prefix}_after must not be later than {prefix}_before.'


@pytest.mark.parametrize('params,message', [
    ({'limit':0}, 'limit must be between 1 and 1000.'),
    ({'limit':-1}, 'limit must be between 1 and 1000.'),
    ({'limit':1001}, 'limit must be between 1 and 1000.'),
    ({'offset':-1}, 'offset must be 0 or more.'),
])
def test_paging_refusals(history_client, params, message):
    response = history_client.get('/print-jobs', params=params, headers=interactive_headers())
    assert response.status_code == 422 and response.json()['detail'] == message


def test_detail_uses_same_fields_without_query_defaults(history_client):
    detail = history_client.get('/print-jobs/903', headers=interactive_headers())
    assert detail.status_code == 200
    assert detail.json() == read(history_client, job_id=903).json()[0]
    assert detail.json()['duration_minutes'] == 60
    assert detail.json()['display_name'] == 'Example'
    assert history_client.get('/print-jobs/999999', headers=interactive_headers()).status_code == 404


def test_default_limit_keeps_array(history_client, review_db):
    for identifier in range(910,965):
        review_db.execute(text("INSERT INTO print_jobs(id,printer_id,status,started_at) VALUES(:id,9,'running','2026-11-02')"), {'id':identifier})
    review_db.commit()
    response = read(history_client)
    assert isinstance(response.json(), list) and len(response.json()) == 50
    assert response.headers['X-Total-Count'] == '60'


def test_org_shared_printer_and_controlled_visibility(history_client, review_db):
    review_db.execute(text("INSERT INTO printers(id,name,org_id,shared) VALUES(91,'Foreign',2,0),(92,'Shared',2,1),(93,'Global',NULL,0)"))
    for identifier in (91,92,93):
        review_db.execute(text("INSERT INTO print_jobs(id,printer_id,status,started_at) VALUES(:id,:printer,'completed','2026-11-02')"), {'id':identifier+900,'printer':identifier})
    review_db.execute(text("INSERT INTO jobs(id,item_name,status,charged_to_org_id) VALUES(901,'Restricted','completed',1)"))
    review_db.execute(text('UPDATE print_jobs SET scheduled_job_id=901 WHERE id=905'))
    review_db.execute(text("INSERT INTO controlled_audiences(id,name) VALUES(901,'Restricted audience')"))
    review_db.execute(text("INSERT INTO controlled_jobs(job_id,label,audience_id,printer_tag,retention_days,reason,changed_by,marked_at) VALUES(901,'Restricted',901,'Secure',7,'Synthetic test',3,'2026-11-01T00:00:00Z')"))
    review_db.commit()
    response = read(history_client, org_id=1, limit=3)
    assert response.headers['X-Total-Count'] == '6'
    assert [row['id'] for row in response.json()] == [993,992,904]
    assert [row['id'] for row in read(history_client, offset=3).json()] == [903,902,901]
    for hidden in (991,905):
        assert history_client.get(f'/print-jobs/{hidden}', headers=interactive_headers()).status_code == 404
    review_db.execute(text('INSERT INTO controlled_audience_members(audience_id,user_id) VALUES(901,3)'));review_db.commit()
    assert read(history_client).headers['X-Total-Count'] == '7'
    assert history_client.get('/print-jobs/905', headers=interactive_headers()).status_code == 200


def test_openapi_documents_dates_and_count(history_client):
    operation = history_client.app.openapi()['paths']['/print-jobs']['get']
    assert 'X-Total-Count' in operation['responses']['200']['headers']
    parameters = {p['name']:p for p in operation['parameters']}
    assert 'days' not in parameters
    assert all(parameters[name]['description'] for name in ('started_after','started_before','ended_after','ended_before','limit','offset'))


@pytest.fixture
def controlled_history(history_client, review_db):
    db = review_db
    db.execute(text("INSERT INTO controlled_audiences(id,name) VALUES(901,'History readers')"))
    db.execute(text('INSERT INTO controlled_audience_members(audience_id,user_id) VALUES(901,1)'))
    db.execute(text("INSERT INTO print_files(id,filename,org_id) VALUES(901,'Marked.3mf',1)"))
    for identifier in (901, 902, 903, 904):
        db.execute(text("INSERT INTO jobs(id,item_name,status,charged_to_org_id,print_file_id) "
                        "VALUES(:id,'History job','completed',1,:file)"),
                   {'id': identifier, 'file': 901 if identifier == 902 else None})
        db.execute(text('UPDATE print_jobs SET scheduled_job_id=:id WHERE id=:id'), {'id': identifier})
    for table, column, identifier in (('controlled_jobs', 'job_id', 901),
                                     ('controlled_jobs', 'job_id', 903),
                                     ('controlled_files', 'file_id', 901)):
        db.execute(text(f"INSERT INTO {table}({column},label,audience_id,printer_tag,retention_days,reason,changed_by,marked_at) "
                        "VALUES(:id,'Restricted',901,'Secure',7,'Synthetic test',3,'2026-11-01T00:00:00Z')"),
                   {'id': identifier})
    # Exercise the real deletion snapshot, retaining the print's durable job link.
    from core.controlled_deletion import redact_deleted_job_history
    redact_deleted_job_history(db, 903)
    db.execute(text('DELETE FROM controlled_jobs WHERE job_id=903'))
    db.execute(text('DELETE FROM jobs WHERE id=903'))
    db.commit()
    assert db.execute(text('SELECT job_id FROM controlled_job_tombstones')).scalars().all() == [903]
    return history_client


@pytest.mark.parametrize('user_id,expected', [(1, [905,904,903,902,901]), (4, [905,904])])
def test_controlled_pages_equal_item_policy(controlled_history, review_db, user_id, expected, monkeypatch):
    from core.auth import create_access_token
    from core.controlled_parts import allowed
    from core import display_names
    actor = {'id': user_id, 'role': 'viewer', 'group_id': 1}
    admitted = [identifier for identifier in range(905, 900, -1)
                if allowed(review_db, 'print_job', identifier, actor)]
    assert admitted == expected
    original = display_names.name_payloads

    def checked_names(db, kind, rows, user):
        named = original(db, kind, rows, user)
        assert [row['id'] for row in named] == [row['id'] for row in rows]
        return named

    monkeypatch.setattr(display_names, 'name_payloads', checked_names)
    username = 'student' if user_id == 1 else 'outsider'
    headers = {'Authorization': 'Bearer ' + create_access_token(data={
        'sub': username, 'user_id': user_id, 'role': 'viewer', 'group_id': 1})}
    actual = []
    for offset in range(0, len(admitted) + 2, 2):
        response = controlled_history.get('/print-jobs', params={'limit': 2, 'offset': offset}, headers=headers)
        assert response.status_code == 200, response.text
        page = [row['id'] for row in response.json()]
        assert page == admitted[offset:offset + 2]
        assert response.headers['X-Total-Count'] == str(len(admitted))
        actual.extend(page)
    assert actual == admitted


def test_controlled_full_page_query_count_does_not_grow_with_history(controlled_history, review_db):
    from sqlalchemy import event
    queries = []

    def count_query(*args):
        queries.append(args[2])

    engine = review_db.get_bind()
    event.listen(engine, 'before_cursor_execute', count_query)
    try:
        first = read(controlled_history, limit=2)
        initial_count = len(queries)
        # More hidden history must not add per-record policy queries to a page read.
        review_db.execute(text("INSERT INTO print_jobs(id,printer_id,scheduled_job_id,status,started_at) "
                               "VALUES(:id,9,901,'completed','2026-10-01')"),
                          [{'id': identifier} for identifier in range(1000, 1150)])
        review_db.commit()
        queries.clear()
        second = read(controlled_history, limit=2)
        assert [row['id'] for row in first.json()] == [row['id'] for row in second.json()] == [905,904]
        assert first.headers['X-Total-Count'] == second.headers['X-Total-Count'] == '2'
        assert len(queries) == initial_count
        assert len(queries) < 150  # Includes authentication, naming and failure annotations.
    finally:
        event.remove(engine, 'before_cursor_execute', count_query)


def test_retained_orphan_job_mark_is_filtered_before_counting(controlled_history, review_db):
    from core.controlled_parts import allowed
    # Older databases can retain orphan marks; item checks still enforce them.
    review_db.commit()
    review_db.execute(text('PRAGMA foreign_keys=OFF'))
    review_db.execute(text("INSERT INTO controlled_jobs(job_id,label,audience_id,printer_tag,retention_days,reason,marked_at) "
                           "VALUES(999,'Restricted',901,'Secure',7,'Legacy test','2026-11-01')"))
    review_db.execute(text('UPDATE print_jobs SET scheduled_job_id=999 WHERE id=905'))
    review_db.commit()
    review_db.execute(text('PRAGMA foreign_keys=ON'))
    assert not allowed(review_db, 'print_job', 905, {'id': 3, 'role': 'admin', 'group_id': 1})
    response = read(controlled_history, limit=1)
    assert response.headers['X-Total-Count'] == '1'
    assert [row['id'] for row in response.json()] == [904]


def test_postgres_date_bind_values_are_aware_utc():
    # print_jobs times are 'timestamp with time zone' on PostgreSQL: a naive bound would be read in
    # the session time zone (proved on a real server by test_print_jobs_paging_postgres.py).
    from datetime import datetime, timezone
    from unittest.mock import MagicMock
    from modules.jobs.routes.presets import _print_date, _print_jobs
    db = MagicMock()
    db.info = {}
    db.get_bind.return_value.dialect.name = 'postgresql'
    db.execute.return_value.first.return_value = None
    db.execute.return_value.scalar_one.return_value = 0
    db.execute.return_value.fetchall.return_value = []
    dates = {name: _print_date('2026-11-01T03:00:00+03:00', name) for name in
             ('started_after', 'started_before', 'ended_after', 'ended_before')}
    assert _print_jobs(db, {'id': 1, 'role': 'viewer', 'group_id': 1}, dates=dates) == ([], 0)
    bindings = [call.args[1] for call in db.execute.call_args_list
                if len(call.args) > 1 and 'started_after' in call.args[1]]
    assert len(bindings) == 2  # Count and page use the same UTC bounds.
    for parameters in bindings:
        assert all(parameters[name] == datetime(2026, 11, 1, tzinfo=timezone.utc)
                   and parameters[name].utcoffset().total_seconds() == 0 for name in dates)
