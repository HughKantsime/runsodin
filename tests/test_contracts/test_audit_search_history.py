"""Server-side literal history search and matching bounded CSV filters."""
import csv
import io
import json
import pytest
from sqlalchemy import text
from tests.test_contracts.test_audit_compliance_claim_acceptance import audit_client
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_api_token_delegation_behavior import token_client

# Exercises Pro/Enterprise-gated routes (cert-fix-G tier gates): run under a real signed license.
pytestmark = pytest.mark.usefixtures("enterprise_license")


def test_history_search_precedes_pagination_and_csv_matches(audit_client, review_db):
    client, _ = audit_client
    for identifier in range(9800, 9860):
        review_db.execute(text("INSERT INTO audit_logs(id,timestamp,action,entity_type,details) "
                               "VALUES(:id,'2026-10-13 10:00:00','update','job',:details)"),
                          {'id':identifier, 'details':json.dumps({'marker':'Only oldest target' if identifier == 9800 else 'Other owned entry'})})
    review_db.commit()
    assert 9800 not in [row['id'] for row in client.get('/audit-logs').json()['logs']]
    params = {'q':'only oldest TARGET', 'date_from':'2026-10-13', 'date_to':'2026-10-13', 'entity_type':'job'}
    response = client.get('/audit-logs', params={**params, 'limit':1})
    assert response.status_code == 200
    assert response.json()['total'] == 1
    assert [row['id'] for row in response.json()['logs']] == [9800]
    exported = client.get('/export/audit-logs', params=params)
    assert exported.status_code == 200
    assert [row['ID'] for row in csv.DictReader(io.StringIO(exported.text))] == ['9800']


@pytest.mark.parametrize('literal', ['%', '_', 'slash\\own'])
def test_literal_metacharacters_do_not_expand_search(audit_client, review_db, literal):
    client, _ = audit_client
    review_db.execute(text("INSERT INTO audit_logs(id,timestamp,action,entity_type) VALUES(9800,'2026-10-13 10:00:00',:action,'job')"), {'action':'owned '+literal})
    review_db.commit()
    response = client.get('/audit-logs', params={'q':literal})
    assert response.status_code == 200
    assert response.json()['total'] == 1
    assert [row['id'] for row in response.json()['logs']] == [9800]


def test_date_end_includes_last_microsecond_and_excludes_next_day(audit_client, review_db):
    client, _ = audit_client
    for identifier, timestamp in [(9800,'2026-10-13 23:59:59.999999'), (9801,'2026-10-14 00:00:00')]:
        review_db.execute(text("INSERT INTO audit_logs(id,timestamp,action) VALUES(:id,:when,'owned_boundary')"), {'id':identifier, 'when':timestamp})
    review_db.commit()
    params = {'action':'owned_boundary', 'date_from':'2026-10-13', 'date_to':'2026-10-13'}
    assert [row['id'] for row in client.get('/audit-logs', params=params).json()['logs']] == [9800]
    assert [row['ID'] for row in csv.DictReader(io.StringIO(client.get('/export/audit-logs', params=params).text))] == ['9800']


@pytest.mark.parametrize('params', [{'limit':0}, {'offset':-1}, {'date_from':'not-a-date'},
    {'date_from':'2026-10-14','date_to':'2026-10-13'}, {'date_to':'9999-12-31'}, {'q':'x'*201}])
def test_history_rejects_invalid_bounds(audit_client, params):
    client, _ = audit_client
    assert client.get('/audit-logs', params=params).status_code == 422


@pytest.mark.parametrize('role,org', [('operator',None), ('viewer',None), ('admin',1)])
def test_search_and_export_retain_platform_admin_boundary(audit_client, role, org):
    client, principal = audit_client
    principal.update(role=role, group_id=org)
    assert client.get('/audit-logs', params={'q':'owned'}).status_code == 403
    assert client.get('/export/audit-logs', params={'q':'owned'}).status_code == 403


@pytest.mark.parametrize('scopes,expected', [(['read:jobs'],403), (['read:models'],403), (['read'],200), (['admin'],200)])
def test_actual_scoped_token_has_no_implicit_cross_resource_audit_access(token_client, review_db, scopes, expected):
    from modules.reporting.routes import exports
    client, issue = token_client
    client.app.include_router(exports.router)
    review_db.execute(text("UPDATE users SET role='admin',group_id=NULL WHERE id=3"))
    review_db.commit()
    headers = issue(scopes)
    listed = client.get('/audit-logs', headers=headers)
    exported = client.get('/export/audit-logs', headers=headers)
    assert listed.status_code == expected
    assert exported.status_code == expected


@pytest.mark.parametrize('formula', ['=1+1', '+owned', '-owned', '@owned', '\towned', '\rowned'])
def test_csv_escapes_untrusted_formula_cells_without_changing_history(audit_client, review_db, formula):
    client, _ = audit_client
    review_db.execute(text("INSERT INTO audit_logs(id,timestamp,action,entity_type,entity_id,details) "
                           "VALUES(9800,'2026-10-13 10:00:00',:action,'owned_formula',0,:details)"),
                      {'action':formula, 'details':json.dumps(formula)})
    review_db.commit()
    exported = client.get('/export/audit-logs', params={'entity_type':'owned_formula'})
    assert exported.status_code == 200
    record = list(csv.DictReader(io.StringIO(exported.text)))[0]
    assert record['Action'] == "'" + formula and record['Details'] == "'" + formula
    assert record['Entity ID'] == '0'
    listed = client.get('/audit-logs', params={'entity_type':'owned_formula'}).json()['logs'][0]
    assert listed['action'] == formula and listed['details'] == formula


def test_recorded_actor_projection_search_and_csv_do_not_join_current_user(audit_client, review_db):
    client, _ = audit_client
    review_db.execute(text("INSERT INTO audit_logs(id,timestamp,action,actor_user_id,actor_username,actor_auth_kind) "
                           "VALUES(9800,'2026-10-13 10:00:00','owned_actor',2147483647,'=Recorded_% actor','bearer_jwt')"))
    review_db.execute(text("INSERT INTO audit_logs(id,timestamp,action,actor_auth_kind) "
                           "VALUES(9801,'2026-10-13 10:00:00','owned_service','legacy_global_api_key')"))
    review_db.commit()
    response = client.get('/audit-logs', params={'q':'Recorded_% actor'})
    assert response.status_code == 200
    page = response.json()
    assert page['total'] == 1
    row = page['logs'][0]
    assert (row['user_id'], row['username'], row['auth_kind']) == (2147483647, '=Recorded_% actor', 'bearer_jwt')
    exported = client.get('/export/audit-logs', params={'q':'Recorded_% actor'})
    assert exported.status_code == 200
    record = list(csv.DictReader(io.StringIO(exported.text)))[0]
    assert record['User ID'] == '2147483647' and record['Username'] == "'=Recorded_% actor" and record['Authentication'] == 'bearer_jwt'
    service = client.get('/audit-logs', params={'action':'owned_service'}).json()['logs'][0]
    assert service['user_id'] is None and service['username'] is None and service['auth_kind'] == 'legacy_global_api_key'


@pytest.mark.parametrize('field', ['date_from', 'date_to'])
@pytest.mark.parametrize('value', ['2026-10-13T00:00:00', '0', '20261013', '2026-1-3', '2026-10-13 ', ' 2026-10-13'])
def test_audit_dates_refuse_datetime_numeric_and_noncanonical_coercion(audit_client, field, value):
    client, _ = audit_client
    assert client.get('/audit-logs', params={field:value}).status_code == 422
    assert client.get('/export/audit-logs', params={field:value}).status_code == 422
