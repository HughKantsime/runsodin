"""Real auth/schema/dispatch receipts, with only physical transport substituted."""
import pytest
from sqlalchemy import text
from tests.test_contracts.test_api_token_delegation_behavior import token_client, review_db
from tests.test_contracts.test_dispatch_observation_binding_behavior import observed
from core.dispatch_observations import bind_generic_observation

@pytest.fixture
def status_client(token_client, observed):
    from modules.jobs.routes.dispatch_status import router
    client, issue = token_client
    client.app.include_router(router)
    return client, issue(['read:jobs'])


def test_accepted_is_not_layer_proof(status_client):
    client, headers = status_client
    body = client.get('/jobs/53/dispatch-status', headers=headers).json()
    assert body == {'job_id':53, 'phase':'start_accepted', 'retry_blocked':True, 'observation':None}


def test_only_exact_bound_observation_supplies_layers(status_client, observed, review_db):
    client, headers = status_client
    engine, key = observed
    review_db.execute(text('UPDATE print_jobs SET current_layer=4,total_layers=54,progress_percent=7 WHERE id=70'))
    review_db.commit()
    assert client.get('/jobs/53/dispatch-status',headers=headers).json()['observation'] is None
    assert bind_generic_observation(engine, printer_id=59, print_job_id=70, key=key) == 'matched'
    body = client.get('/jobs/53/dispatch-status',headers=headers).json()
    assert body['phase'] == 'layer_observed'
    assert body['observation']['current_layer'] == 4
    assert body['observation']['start_observation_age_seconds'] >= 0
    assert set(body['observation']) == {'current_layer','total_layers','progress_percent','status','start_observation_age_seconds'}
    review_db.execute(text("UPDATE print_jobs SET filename='wrong',job_name='wrong' WHERE id=70")); review_db.commit()
    assert client.get('/jobs/53/dispatch-status',headers=headers).json()['observation'] is None


def test_job_and_printer_org_checks_are_independent(status_client, review_db):
    client, headers = status_client
    review_db.execute(text('UPDATE jobs SET charged_to_org_id=2 WHERE id=53')); review_db.commit()
    assert client.get('/jobs/53/dispatch-status',headers=headers).status_code == 404
    review_db.execute(text('UPDATE jobs SET charged_to_org_id=1 WHERE id=53'))
    review_db.execute(text('UPDATE printers SET org_id=2,shared=FALSE WHERE id=59')); review_db.commit()
    assert client.get('/jobs/53/dispatch-status',headers=headers).status_code == 404


def test_other_job_is_generic_busy_and_missing_claim_inconclusive(status_client, review_db):
    client, headers = status_client
    body = client.get('/jobs/54/dispatch-status',headers=headers).json()
    assert body == {'job_id':54,'phase':'target_busy','retry_blocked':True,'observation':None}
    review_db.execute(text('DELETE FROM printer_dispatch_claims WHERE printer_id=59')); review_db.commit()
    body = client.get('/jobs/53/dispatch-status',headers=headers).json()
    assert body['phase'] == 'unconfirmed' and body['retry_blocked'] is True


def test_status_requires_read_scope(status_client, token_client):
    client, _ = status_client
    _, issue = token_client
    assert client.get('/jobs/53/dispatch-status',headers=issue(['write:jobs'])).status_code == 403
    assert client.get('/jobs/53/dispatch-status').status_code == 401


def test_shared_target_uses_printer_owner_and_exact_job(status_client, review_db):
    client, headers = status_client
    review_db.execute(text('UPDATE printers SET org_id=2,shared=TRUE WHERE id=59'))
    review_db.execute(text('UPDATE printer_dispatch_claims SET owner_org_id=2 WHERE printer_id=59')); review_db.commit()
    assert client.get('/jobs/53/dispatch-status',headers=headers).json() == {'job_id':53,'phase':'start_accepted','retry_blocked':True,'observation':None}
    assert client.get('/jobs/54/dispatch-status',headers=headers).json() == {'job_id':54,'phase':'target_busy','retry_blocked':True,'observation':None}


def test_ungrouped_admin_can_read_matching_unowned_attempt(status_client, review_db):
    client, headers = status_client
    review_db.execute(text('UPDATE users SET group_id=NULL WHERE id=3'))
    review_db.execute(text('UPDATE jobs SET charged_to_org_id=NULL WHERE id=53'))
    review_db.execute(text('UPDATE printers SET org_id=NULL WHERE id=59'))
    review_db.execute(text('UPDATE printer_dispatch_claims SET owner_org_id=NULL WHERE printer_id=59')); review_db.commit()
    response = client.get('/jobs/53/dispatch-status',headers=headers)
    assert response.status_code == 200 and response.json()['phase'] == 'start_accepted'


def test_status_handler_performs_only_reads(status_client, review_db):
    from sqlalchemy import event
    from modules.jobs.routes.dispatch_status import dispatch_status
    statements = []
    engine = review_db.get_bind()
    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.lstrip().split()[0].upper())
    event.listen(engine, 'before_cursor_execute', capture)
    try:
        result = dispatch_status(53, {'role':'admin','group_id':1}, review_db)
        assert result['phase'] == 'start_accepted'
    finally:
        event.remove(engine, 'before_cursor_execute', capture)
    assert statements and set(statements) == {'SELECT'}
