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


def test_default_limit_and_ignored_days_keep_array(history_client, review_db):
    for identifier in range(910,965):
        review_db.execute(text("INSERT INTO print_jobs(id,printer_id,status,started_at) VALUES(:id,9,'running','2026-11-02')"), {'id':identifier})
    review_db.commit()
    response = read(history_client, days=1)
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
    assert all(parameters[name]['description'] for name in ('started_after','started_before','ended_after','ended_before','limit','offset'))
