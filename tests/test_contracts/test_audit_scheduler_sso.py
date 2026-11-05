"""Automatic dispatch provenance and SSO identity reporting/erasure."""
import csv
import io
import json

import pytest
from sqlalchemy import insert, text
from sqlalchemy.orm import Session

from tests.test_contracts.test_audit_completeness import client, review_db, rows
from tests.test_contracts.test_audit_completeness_oidc import seed_code, callback

pytestmark = pytest.mark.usefixtures('enterprise_license')


@pytest.mark.parametrize('interactive', [False, True])
def test_dispatch_outcome_labels_automatic_source(review_db, interactive):
    from core import dispatch_claims
    from core.audit_actor import RequestAuditScope, bind_verified_actor
    from core.audit_actor_handoff import capture_request_actor
    review_db.execute(text("INSERT INTO jobs(id,item_name,printer_id,status,submitted_by,approved_by) VALUES(1,'Owned scheduled job',9,'scheduled',1,2)"))
    review_db.execute(insert(dispatch_claims.TABLE).values(printer_id=9, job_id=1,
        physical_key='a'*64, configuration_key='b'*64, payload_sha256='c'*64,
        token='a'*32, state='adapter_entered'))
    review_db.commit()
    binder = None
    if interactive:
        RequestAuditScope('192.0.2.1').register(review_db)
        bind_verified_actor(review_db, {'id': 3, 'username': 'admin'}, 'bearer_jwt')
        binder = capture_request_actor(review_db)
    assert dispatch_claims.record_outcome(review_db.get_bind(), 'a'*32, 'accepted', audit_actor=binder)
    row = rows(review_db, 'job.dispatched')[0]
    expected = {'printer_id': 9}
    if not interactive:
        expected.update(trigger='scheduler', queued_by_user_id=1, approved_by_user_id=2)
    assert json.loads(row.details) == expected
    assert row.actor_user_id == (3 if interactive else None)


def test_sso_success_csv_and_erasure(client, review_db):
    from modules.organizations.erasure_audit import erase_sso_login_history, erase_password_login_history
    code = seed_code(review_db, client)
    assert client.post('/auth/oidc/exchange', json={'code': code}).status_code == 200
    response = client.get('/export/audit-logs')
    assert response.status_code == 200
    login = next(row for row in csv.DictReader(io.StringIO(response.text)) if row['Action'] == 'auth.login')
    assert (login['User ID'], login['Username'], login['Authentication']) == ('3', 'admin', '')
    review_db.execute(text('UPDATE users SET is_active=0 WHERE id=3')); review_db.commit()
    client.cookies.clear()
    code = seed_code(review_db, client)
    assert client.post('/auth/oidc/exchange', json={'code': code}).status_code == 401
    assert json.loads(rows(review_db, 'auth.login_failed')[0].details)['username'] == 'admin'
    erase_sso_login_history(review_db, 3)
    erase_password_login_history(review_db, 'admin')
    review_db.commit()
    for action in ('auth.login', 'auth.login_failed'):
        row = rows(review_db, action)[0]
        assert json.loads(row.details)['username'] == '[deleted]' and row.ip_address is None
    assert rows(review_db, 'auth.login')[0].entity_id == 3


def test_inactive_sso_callback_retains_resolved_username(client, review_db, callback):
    review_db.execute(text("UPDATE users SET oidc_issuer='https://issuer.example',oidc_subject='subject-1',is_active=0 WHERE id=1"))
    review_db.commit()
    response = client.get('/auth/oidc/callback', params=callback, follow_redirects=False)
    assert response.status_code == 302
    row = rows(review_db, 'auth.login_failed')[0]
    assert json.loads(row.details) == {'method': 'oidc', 'reason': 'user_inactive', 'username': 'student'}


def test_wait_dispatch_keeps_one_interactive_acceptance(client, review_db, monkeypatch):
    from core import dispatch_claims
    from modules.printers import dispatch
    review_db.execute(text("INSERT INTO jobs(id,item_name,printer_id,status,submitted_by) VALUES(900,'Owned interactive job',9,'scheduled',1)"))
    review_db.execute(insert(dispatch_claims.TABLE).values(printer_id=9, job_id=900,
        physical_key='a'*64, configuration_key='b'*64, payload_sha256='c'*64,
        token='b'*32, state='adapter_entered'))
    review_db.commit()
    def accepted(printer_id, job_id):
        assert (printer_id, job_id) == (9, 900)
        assert dispatch_claims.record_outcome(review_db.get_bind(), 'b'*32, 'accepted')
        return True, 'Owned acceptance'
    monkeypatch.setattr(dispatch, 'dispatch_job', accepted)
    response = client.post('/jobs/900/dispatch?wait=true')
    assert response.status_code == 200, response.text
    found = rows(review_db, 'job.dispatched')
    assert len(found) == 1 and json.loads(found[0].details) == {'printer_id': 9}
    assert found[0].actor_user_id == 3 and found[0].actor_username == 'admin'
