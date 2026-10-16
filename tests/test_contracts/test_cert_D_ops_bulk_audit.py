"""cert-D ops: F04-4.3-05 / F19-19.2-01 bulk jobs, F19-19.2-02 bulk printers and
F19-19.1-* audit producer captures through actual HTTP routers on the canonical schema."""
import json
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_audit_compliance_claim_acceptance import audit_client
from tests.test_contracts.test_audit_actor_requests import client as actor_client
from tests.test_contracts.test_api_token_delegation_behavior import token_client
from tests.test_contracts.test_printer_tag_behavior import tag_client, stored
from tests.test_contracts.cert_D_capture import publish

FIXTURES = Path(__file__).resolve().parents[2] / 'frontend/src/test-fixtures'
BULK = FIXTURES / 'certDops-bulk-jobs-wire.json'
AUDIT = FIXTURES / 'certDops-audit-wire.json'

# Exactly what Jobs.tsx toolbar sends via bulkOps.jobs(selected, action, extra)
UI_REQUESTS = {
    'cancel': {'job_ids': [9201, 9202], 'action': 'cancel'},
    'set_priority': {'job_ids': [9201, 9202], 'action': 'set_priority', 'priority': 1},
    'reschedule': {'job_ids': [9201, 9202], 'action': 'reschedule'},
    'delete': {'job_ids': [9201, 9202], 'action': 'delete'},
}


def _merge(path, key, value):
    publish(path, value, key=key)


def _rows(review_db):
    with Session(review_db.get_bind()) as db:
        return {r.id: r for r in db.execute(text(
            'SELECT id,status,priority,printer_id,scheduled_start FROM jobs WHERE id IN (9201,9202,9203)')).all()}


@pytest.mark.parametrize('action', list(UI_REQUESTS))
def test_ui_bulk_payload_changes_every_selected_job_and_leaves_unselected(audit_client, review_db, action):
    client, _ = audit_client
    review_db.execute(text("INSERT INTO jobs(id,item_name,status,charged_to_org_id,quantity,hold,priority) VALUES(9203,'Unselected',  'pending',1,1,FALSE,3)"))
    if action == 'reschedule':
        review_db.execute(text("UPDATE jobs SET status='scheduled',printer_id=9,scheduled_start='2026-10-20 10:00:00',scheduled_end='2026-10-20 11:00:00' WHERE id IN (9201,9202,9203)"))
    review_db.commit()
    listing = client.get('/jobs')
    assert listing.status_code == 200
    response = client.post('/jobs/bulk-update', json=UI_REQUESTS[action])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['affected'] == 2
    rows = _rows(review_db)
    if action == 'delete':
        assert set(rows) == {9203}
    elif action == 'cancel':
        assert [rows[i].status for i in (9201, 9202)] == ['cancelled', 'cancelled'] and rows[9203].status == 'pending'
    elif action == 'set_priority':
        assert [rows[i].priority for i in (9201, 9202)] == [1, 1] and rows[9203].priority == 3
    else:
        assert body['skipped'] == 0
        assert [(rows[i].status, rows[i].printer_id, rows[i].scheduled_start) for i in (9201, 9202)] == [('pending', None, None)] * 2
        assert (rows[9203].status, rows[9203].printer_id) == ('scheduled', 9)
    with Session(review_db.get_bind()) as db:
        assert db.execute(text('SELECT COUNT(*) FROM audit_logs WHERE action=:a'), {'a': 'bulk_' + action}).scalar_one() == 1
    _merge(BULK, action, {'request': UI_REQUESTS[action], 'response': body})
    if action == 'cancel':
        jobs = [j for j in listing.json() if j['id'] in (9201, 9202)]
        assert len(jobs) == 2
        _merge(BULK, 'jobs', jobs)


def test_bulk_printer_enable_activates_every_target(tag_client, review_db):
    client, issue = tag_client
    review_db.execute(text('UPDATE users SET group_id=NULL WHERE id=3'))
    review_db.execute(text('UPDATE printers SET is_active=0 WHERE id IN (399,400)'))
    review_db.commit()
    response = client.post('/printers/bulk-update', headers=issue(['write:printers']), json={'printer_ids': [399, 400], 'action': 'enable'})
    assert response.status_code == 200 and response.json()['affected'] == 2
    assert all(stored(review_db, pid)[1] is True for pid in (399, 400))
    response = client.post('/printers/bulk-update', headers=issue(['write:printers']), json={'printer_ids': [399, 400], 'action': 'add_tag', 'tag': 'certdops'})
    assert response.status_code == 200
    assert all('certdops' in stored(review_db, pid)[0] for pid in (399, 400))


def test_capture_default_permissions_and_authenticated_audit_history(actor_client, review_db, monkeypatch):
    from modules.reporting.routes import exports
    from modules.jobs.routes import jobs_crud
    from modules.organizations import routes_permissions
    from core.registry import registry
    from modules.organizations.services import EducationPolicyService
    monkeypatch.setattr(registry, '_providers', registry.providers | {'EducationPolicyProvider': EducationPolicyService()})
    client, bearer, _, _ = actor_client
    for router in (exports.router, jobs_crud.router, routes_permissions.router):
        client.app.include_router(router)
    review_db.execute(text('UPDATE users SET group_id=NULL WHERE id=3'))
    review_db.execute(text("INSERT INTO jobs(id,item_name,status,quantity,hold,priority) VALUES(9201,'certDops audit batch','pending',1,FALSE,3)"))
    review_db.commit()
    headers = bearer('admin')
    assert client.post('/jobs/bulk-update', headers=headers, json={'job_ids': [9201], 'action': 'cancel'}).status_code == 200
    history = client.get('/audit-logs', headers=headers, params={'action': 'bulk_cancel'})
    assert history.status_code == 200
    page = history.json()
    assert page['total'] == 1
    row = page['logs'][0]
    assert (row['username'], row['ip_address'], row['action']) == ('admin', '198.51.100.7', 'bulk_cancel')
    assert row['timestamp']
    exported = client.get('/export/audit-logs', headers=headers, params={'entity_type': 'jobs', 'action': 'bulk_cancel', 'q': 'admin'})
    assert exported.status_code == 200 and 'text/csv' in exported.headers['content-type']
    lines = exported.text.strip().splitlines()
    assert len(lines) == 2 and 'bulk_cancel' in lines[1] and '198.51.100.7' in lines[1] and ',admin,' in lines[1]
    _merge(AUDIT, 'export_csv', exported.text)
    permissions = client.get('/permissions')
    assert permissions.status_code == 200
    # The /audit viewer's RoleGate key is in the default map, admin-only.
    assert permissions.json()['page_access']['audit'] == ['admin']
    _merge(AUDIT, 'audit_page', page)
    _merge(AUDIT, 'default_permissions', permissions.json())
    _merge(AUDIT, 'me', {'id': 3, 'username': 'admin', 'role': 'admin', 'group_id': None})
