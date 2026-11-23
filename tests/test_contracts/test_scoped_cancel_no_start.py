"""A reconciled no-start receipt grants no printer authority."""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from modules.jobs.no_start import ACTION, NoStartRequest, no_start_states, record_no_start, router
from tests.test_contracts.test_dispatch_recovery_slot_change import slot_engine, farm, pg_engine
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_production_ops import cancel_client, batch, shop, token_client


@pytest.mark.parametrize('evidence',['reconciled','unreconciled','older_audit','observed','later_receipt','wrong_receipt'])
def test_scoped_cancel_requires_no_start_for_every_receipt(review_db,evidence):
    from modules.jobs.scoped_cancel import assert_cancel_allowed
    from core.errors import OdinError
    from core.registry import registry
    from modules.organizations.services import EducationPolicyService
    registry.register_provider("EducationPolicyProvider", EducationPolicyService())
    review_db.execute(text("INSERT INTO jobs(id,item_name,status,hold) VALUES(8070,'Never started','pending',1)"))
    review_db.execute(text("INSERT INTO dispatch_attempt_receipts(claim_token,printer_id,job_id,configuration_key,prepared_power_key,observed_filename_key,created_at,observed_print_job_id) VALUES(:token,9,8070,:key,:key,:key,'2026-10-25',:observed)"),{'token':'a'*32,'key':'b'*64,'observed':123 if evidence=='observed' else None})
    if evidence!='unreconciled':
        review_db.execute(text("INSERT INTO audit_logs(action,entity_type,entity_id,timestamp,details) VALUES('operator.dispatch_no_start_reconciled','job',8070,:stamp,:details)"),{'stamp':'2026-10-24' if evidence=='older_audit' else '2026-10-26','details':json.dumps({'job_left_held':True,'gate_refusal':'ams_read_failed','claim_token':'c'*32 if evidence=='wrong_receipt' else 'a'*32})})
    if evidence=='later_receipt':
        review_db.execute(text("INSERT INTO dispatch_attempt_receipts(claim_token,printer_id,job_id,configuration_key,prepared_power_key,observed_filename_key,created_at) VALUES(:token,9,8070,:key,:key,:key,'2026-10-27')"),{'token':'c'*32,'key':'b'*64})
    job=SimpleNamespace(id=8070,status='pending',hold=True)
    user={'_token_scopes':['write:jobs'],'_auth_method':'api_token','role':'operator'}
    # Match the token marker used by the production RBAC helper.
    user['_is_api_token']=True
    if evidence=='reconciled':assert_cancel_allowed(review_db,job,user)
    else:
        with pytest.raises(OdinError):assert_cancel_allowed(review_db,job,user)


URL = '/jobs/401/dispatch-attempts/no-start'
BODY = {'attestation': {'version': 1, 'printer_checked': True, 'job_did_not_start': True}}


def receipt(db, token, *, job=401, printer=301, age=60):
    from core.auth import SECRET_KEY
    from core.dispatch_receipts import filename_fingerprint
    stamp = datetime.now(timezone.utc) - timedelta(minutes=age)
    fingerprint = filename_fingerprint(SECRET_KEY.encode(), token, f'odin-dispatch-{token}.3mf')
    db.execute(text('INSERT INTO dispatch_attempt_receipts(claim_token,printer_id,job_id,configuration_key,'
                    'prepared_power_key,observed_filename_key,created_at) VALUES(:token,:pid,:jid,:key,:key,:fp,:stamp)'),
               dict(token=token, pid=printer, jid=job, key='b'*64, fp=fingerprint, stamp=stamp))


@pytest.fixture
def held(cancel_client):
    client, actor, _, db, app, *_ = cancel_client
    app.include_router(router)
    db.execute(text("UPDATE printers SET is_active=true WHERE id=301"))
    db.execute(text("UPDATE jobs SET hold=true, printer_id=301 WHERE id=401"))
    receipt(db, 'a'*32, age=63)
    receipt(db, 'c'*32)
    db.commit()
    return client, actor, db


def audits(db):
    return [json.loads(row) for row in db.execute(text("SELECT details FROM audit_logs WHERE action=:action "
                                                       "AND entity_id=401 ORDER BY id"), {'action': ACTION}).scalars()]


def test_reconciled_receipts_can_cancel_through_scoped_http_route(held):
    client, actor, db = held
    rows = 'SELECT * FROM dispatch_attempt_receipts WHERE job_id=401 ORDER BY claim_token'
    before = db.execute(text(rows)).all()
    hint_job = SimpleNamespace(id=401, hold=True, status='pending')
    assert no_start_states(db, [hint_job]) == {401: 'needs_check'}
    refusal = client.post('/jobs/401/cancel')
    assert refusal.status_code == 403 and 'record that the attempt did not start' in refusal.text
    assert client.post(URL, json=BODY).json() == {'recorded': 2}
    assert client.post(URL, json=BODY).json() == {'recorded': 0}
    assert no_start_states(db, [hint_job]) == {401: 'recorded'}
    from modules.jobs.ams_mapping import detail_view
    from modules.jobs.models import Job
    assert detail_view(db, db.get(Job, 401), actor)['no_start_state'] == 'recorded'
    assert db.execute(text(rows)).all() == before
    assert db.execute(text('SELECT status,hold FROM jobs WHERE id=401')).one() == ('pending', True)
    entries = audits(db)
    assert {a['claim_token'] for a in entries} == {'a'*32, 'c'*32} and len(entries) == 2
    for a in entries:
        assert a['job_left_held'] is True and a['observed_print_job_id'] is None
        assert a['claim_token'] == a['attempt_id'] and a['attestation'] == BODY['attestation']
        assert a['actor_user_id'] == actor['id'] and a['actor_token_id'] == 77
        assert a['printer_checks'] == {'301': 'operator_check_only'}

    receipt(db, 'd'*32, age=0)
    db.commit()
    assert client.post('/jobs/401/cancel').status_code == 403
    assert client.post(URL, json=BODY).status_code == 409
    assert no_start_states(db, [hint_job]) == {401: 'needs_check'}
    # Advance the waiting period for just the new attempt.
    db.execute(text('UPDATE dispatch_attempt_receipts SET created_at=:old WHERE claim_token=:token'),
               {'old': datetime.now(timezone.utc).replace(microsecond=0) - timedelta(minutes=10), 'token': 'd'*32})
    db.commit()
    assert client.post(URL, json=BODY).json() == {'recorded': 1}
    response = client.post('/jobs/401/cancel')
    assert response.status_code == 200, response.text
    assert db.execute(text('SELECT status FROM jobs WHERE id=401')).scalar_one() == 'cancelled'


@pytest.mark.parametrize(('case', 'expected', 'reason'), [
    ('unheld', 409, 'on hold'), ('printing', 409, 'pending or scheduled'),
    ('claim', 409, 'Dispatch recovery panel'), ('bound', 409, 'linked to this job'),
    ('sender', 409, 'sender is still recorded'), ('observed', 409, 'observed a print'),
    ('young', 409, '10 minutes'), ('empty', 409, 'no recorded start'),
    ('same_file', 409, 'matches this job'), ('fresh_observation', 409, 'matches this job'), ('wire_name', 409, 'matches this job'),
    ('uploaded_name', 409, 'matches this job'),
    ('education', 409, 'Education'), ('viewer', 403, 'permissions'),
    ('scope', 403, 'scope'), ('foreign', 404, 'Job not found'), ('foreign_printer', 404, 'Printer not found'),
])
def test_action_refuses_without_writing(held, case, expected, reason):
    client, actor, db = held
    queries = {
        'unheld': 'UPDATE jobs SET hold=false WHERE id=401',
        'printing': "UPDATE jobs SET status='printing' WHERE id=401",
        'claim': "INSERT INTO printer_dispatch_claims(printer_id,job_id,physical_key,configuration_key,payload_sha256,"
                 "token,owner_org_id,state) VALUES(301,401,:key,:key,:key,:token,1,'uncertain')",
        'bound': 'UPDATE print_jobs SET scheduled_job_id=401 WHERE scheduled_job_id=302',
        'sender': "INSERT INTO dispatch_sender_registrations(claim_token,printer_id,job_id,physical_key,configuration_key,"
                  "payload_sha256,owner_org_id,origin_key,runtime_incarnation,registration_key,state) "
                  "VALUES(:token,301,401,:key,:key,:key,1,:token,:token,:token,'active')",
        'observed': 'UPDATE dispatch_attempt_receipts SET observed_print_job_id=123 WHERE claim_token=:first',
        'young': 'UPDATE dispatch_attempt_receipts SET created_at=CURRENT_TIMESTAMP WHERE claim_token=:first',
        'empty': 'DELETE FROM dispatch_attempt_receipts WHERE job_id=401',
        'education': 'UPDATE education_submissions SET job_id=401 WHERE id=14',
        'foreign': 'UPDATE jobs SET charged_to_org_id=2 WHERE id=401',
        'foreign_printer': 'UPDATE printers SET org_id=2,shared=false WHERE id=301',
    }
    if case in queries:
        db.execute(text(queries[case]), {'key': 'e'*64, 'token': 'e'*32, 'first': 'a'*32})
    elif case in ('same_file', 'wire_name', 'fresh_observation', 'uploaded_name'):
        # Test the original target even if the job is subsequently reassigned.
        db.execute(text("UPDATE jobs SET printer_id=9 WHERE id=401"))
        if case != 'fresh_observation':
            db.execute(text("UPDATE printers SET last_seen=CURRENT_TIMESTAMP,gcode_state='RUNNING' WHERE id=301"))
        name = 'Never sent' if case == 'same_file' else 'odin-dispatch-' + 'a'*32
        if case == 'uploaded_name':
            db.execute(text("INSERT INTO print_files(id,original_filename) VALUES(941,'sample-part.gcode.3mf')"))
            db.execute(text("UPDATE jobs SET print_file_id=941 WHERE id=401"))
            name = 'sample-part'
        db.execute(text("UPDATE print_jobs SET status='running',ended_at=NULL,observed_received_at=CURRENT_TIMESTAMP,job_name=:name WHERE scheduled_job_id=302"),
                   {'name': name})
    elif case == 'viewer':
        actor['role'] = 'viewer'
    elif case == 'scope':
        actor['_token_scopes'] = ['read:jobs']
    db.commit()
    response = client.post(URL, json=BODY)
    assert response.status_code == expected and reason in response.text, response.text
    assert audits(db) == []
    if case in ('claim', 'observed', 'empty', 'bound', 'sender', 'education'):
        assert not no_start_states(db, [SimpleNamespace(id=401, hold=True, status='pending')])


@pytest.mark.parametrize('body', [{}, {'attestation': {}},
                                  {'attestation': {**BODY['attestation'], 'printer_checked': 1}},
                                  {'attestation': {**BODY['attestation'], 'job_did_not_start': False}}])
def test_explicit_attestation_required(held, body):
    client, _, db = held
    assert client.post(URL, json=body).status_code == 422
    assert audits(db) == []


def test_reconciliation_transaction_on_each_database(slot_engine, farm, monkeypatch):
    from sqlalchemy.orm import Session
    from core.registry import registry
    from modules.organizations.services import EducationPolicyService
    from modules.jobs import no_start
    from uuid import uuid4
    monkeypatch.setitem(registry._providers, 'EducationPolicyProvider', EducationPolicyService())
    actor, _ = farm
    with Session(slot_engine) as db:
        pid = db.execute(text('INSERT INTO printers(name,org_id) VALUES(:name,:org) RETURNING id'),
                         {'name': uuid4().hex, 'org': actor['group_id']}).scalar_one()
        jid = db.execute(text("INSERT INTO jobs(item_name,status,hold,printer_id,charged_to_org_id) "
                              "VALUES('Held part','scheduled',true,:pid,:org) RETURNING id"),
                         {'pid': pid, 'org': actor['group_id']}).scalar_one()
        for _ in range(2):
            receipt(db, uuid4().hex, printer=pid, job=jid)
        db.commit()
        original = no_start.log_audit
        def fail_second(*args):
            if db.new:
                db.flush()
                raise RuntimeError('second audit failed')
            return original(*args)
        body = NoStartRequest(**BODY)
        with monkeypatch.context() as patch:
            patch.setattr(no_start, 'log_audit', fail_second)
            with pytest.raises(RuntimeError, match='second audit failed'):
                record_no_start(jid, body, actor, db)
        assert db.execute(text("SELECT COUNT(*) FROM audit_logs WHERE action=:action AND entity_id=:jid"),
                          {'action': ACTION, 'jid': jid}).scalar_one() == 0
        assert record_no_start(jid, body, actor, db) == {'recorded': 2}
        assert record_no_start(jid, body, actor, db) == {'recorded': 0}


def test_authenticated_operator_token_records_and_cancels(token_client, review_db, monkeypatch):
    from core.registry import registry
    from modules.organizations.services import EducationPolicyService
    from modules.jobs.routes.jobs_agent import router as cancel_router
    monkeypatch.setitem(registry._providers, 'EducationPolicyProvider', EducationPolicyService())
    client, issue = token_client
    client.app.include_router(router)
    client.app.include_router(cancel_router)
    review_db.execute(text("UPDATE users SET role='operator' WHERE id=3"))
    review_db.execute(text("INSERT INTO jobs(id,item_name,status,charged_to_org_id,hold) VALUES(401,'Part','scheduled',1,true)"))
    receipt(review_db, '1'*32, printer=9)
    receipt(review_db, '2'*32, printer=9)
    review_db.commit()
    headers = issue(['write:jobs'])
    assert client.post(URL, json=BODY, headers=headers).json() == {'recorded': 2}
    assert client.post('/jobs/401/cancel', headers=headers).status_code == 200


def test_concurrent_checks_record_each_receipt_once(held):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    client, _, db = held
    start = Barrier(2)
    def submit():
        start.wait(timeout=5)
        response = client.post(URL, json=BODY)
        assert response.status_code == 200, response.text
        return response.json()['recorded']
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(submit) for _ in range(2)]
        assert sorted(f.result() for f in futures) == [0, 2]
    assert len(audits(db)) == 2
