"""Production ops acceptance: local HTTP/SQLite, no printer or Square writes."""
import json
import pytest
from sqlalchemy import text
from tests.test_contracts.test_outside_print_binding import outside, batch, shop, review_db
from tests.test_contracts.test_batch_acceptance import body


@pytest.fixture
def qualification(outside):
    batch, pid, _ = outside
    # The shared fixture's CURRENT_TIMESTAMP end truncates subsecond starts.
    # Give qualification a valid, deterministic observed interval.
    batch[3].execute(text("UPDATE print_jobs SET started_at='2026-10-27 10:00:00',ended_at='2026-10-27 11:00:00' WHERE id=:id"), {'id':pid})
    batch[3].execute(text('''INSERT INTO print_files
        (id,filename,original_filename,model_id,org_id,file_hash,plate_count)
        VALUES(301,'cabinet lock v3.1.3mf','cabinet lock v3.1.3mf',301,1,:hash,2)'''), {'hash':'a'*64})
    batch[3].commit()
    data = {'printer_id':301,'plate_index':0,'name':'First outside plate','materials':'PLA black',
            'qualification_confirmed':True,'outputs':[{'product_id':101,'kind':'main','expected':5}],
            'attestation':{'started_outside_odin':True,'file_revision_confirmed':True}}
    return batch, pid, data


def qualify(fixture):
    batch,pid,data = fixture
    return batch[0].post(f'/products/production/outside-prints/{pid}/qualify-and-bind',json=data)


def test_outside_qualification_evidence_audit_and_normal_inspection(qualification):
    batch,pid,_ = qualification
    response = qualify(qualification)
    assert response.status_code == 200, response.text
    binding = response.json()
    from pathlib import Path
    fixture = json.loads((Path(__file__).parents[1]/'accessibility/fixtures/common.json').read_text())['POST /api/products/production/outside-prints/120/qualify-and-bind']
    assert all(key in binding and type(binding[key]) is type(value) for key,value in fixture.items())
    recipe = binding['recipe_revision']
    assert recipe['qualified_from_outside_print'] is True
    assert recipe['outside_print_evidence']['actor_user_id'] == batch[1]['id']
    assert recipe['outside_print_evidence']['print_job_id'] == pid
    assert recipe['outside_print_evidence']['confirmed_at']
    assert recipe['outside_print_evidence']['matched_by'] == 'filename'
    assert recipe['outside_print_evidence']['observed_filename'] == 'cabinet lock v3.1.3mf'
    assert recipe['payload']['print_file_id'] == 301
    saved = json.loads(batch[3].execute(text('SELECT definition FROM production_recipes WHERE id=:id'), {'id':recipe['id']}).scalar_one())
    assert saved['qualified_from_outside_print'] is True
    assert saved['outside_print_evidence'] == recipe['outside_print_evidence']
    audit = json.loads(batch[3].execute(text("SELECT details FROM audit_logs WHERE action='production.recipe_qualified_from_outside_print'")).scalar_one())
    assert audit['recipe_id'] == recipe['id'] and audit['print_job_id'] == pid
    assert audit['matched_by'] == 'filename'
    assert audit['observed_filename'] == 'cabinet lock v3.1.3mf'
    assert qualify(qualification).status_code == 409
    request = body(batch,recipe); request.pop('job_id'); request['print_job_id'] = pid
    response = batch[0].post('/products/production/inspections',json=request)
    assert response.status_code == 200, response.text
    assert response.json()['stock_status'] == 'prepared'


@pytest.mark.parametrize('reason',['running','dispatch','linked_receipt','owner','printer','token','operator','attestation','qualification','plate','foreign_file','education','education_model','missing_hash'])
def test_outside_qualification_refusals_are_atomic(qualification,reason):
    batch,pid,data = qualification
    db = batch[3]
    if reason == 'running': db.execute(text("UPDATE print_jobs SET status='running' WHERE id=:id"),{'id':pid})
    elif reason == 'dispatch': db.execute(text('UPDATE dispatch_attempt_receipts SET observed_print_job_id=:id WHERE job_id=302'),{'id':pid})
    elif reason == 'linked_receipt': db.execute(text('UPDATE print_jobs SET scheduled_job_id=302 WHERE id=:id'),{'id':pid})
    elif reason == 'owner': batch[1]['group_id'] = 2
    elif reason == 'printer': data['printer_id'] = 999
    elif reason == 'token': batch[1].update(_auth_kind='user_api_token',_token_scopes=['admin','write:production'],_token_id=77)
    elif reason == 'operator': batch[1]['role'] = 'operator'
    elif reason == 'attestation': data['attestation']['started_outside_odin'] = False
    elif reason == 'qualification': data['qualification_confirmed'] = False
    elif reason == 'plate': data['plate_index'] = 2
    elif reason == 'foreign_file':
        data['print_file_id'] = 301
        db.execute(text('UPDATE print_files SET org_id=2 WHERE id=301'))
    elif reason == 'education_model': db.execute(text('UPDATE print_files SET model_id=12 WHERE id=301'))
    elif reason == 'missing_hash': db.execute(text('UPDATE print_files SET file_hash=NULL WHERE id=301'))
    elif reason == 'education': db.execute(text("UPDATE print_jobs SET filename='odin-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' WHERE id=:id"),{'id':pid})
    db.commit()
    before = db.execute(text('SELECT COUNT(*) FROM production_recipes')).scalar_one()
    response = qualify(qualification)
    assert response.status_code in (403,404,409), response.text
    assert db.execute(text('SELECT COUNT(*) FROM production_recipes')).scalar_one() == before
    assert db.execute(text("SELECT COUNT(*) FROM system_config WHERE key LIKE 'outside_print_binding:%'")).scalar_one() == 0


def test_ambiguous_filename_requires_explicit_file(qualification):
    batch,_,data = qualification
    batch[3].execute(text('''INSERT INTO print_files (id,filename,original_filename,model_id,org_id,file_hash,plate_count)
        SELECT 302,filename,original_filename,model_id,org_id,file_hash,plate_count FROM print_files WHERE id=301'''))
    batch[3].commit()
    assert qualify(qualification).status_code == 409
    data['print_file_id'] = 301
    response = qualify(qualification)
    assert response.status_code == 200
    evidence = response.json()['recipe_revision']['outside_print_evidence']
    assert evidence['matched_by'] == 'explicit'
    assert evidence['observed_filename'] == 'cabinet lock v3.1.3mf'


@pytest.mark.parametrize('name_matches', [False, True])
def test_explicit_file_cannot_override_observed_name_candidates(qualification, name_matches):
    batch, _, data = qualification
    db = batch[3]
    db.execute(text('''INSERT INTO print_files (id,filename,original_filename,model_id,org_id,file_hash,plate_count)
        VALUES(303,'different.3mf','different.3mf',301,1,:hash,1)'''), {'hash':'c'*64})
    if not name_matches:
        db.execute(text("UPDATE print_files SET filename='unknown.3mf',original_filename='unknown.3mf' WHERE id=301"))
    db.commit()
    data['print_file_id'] = 303
    before = db.execute(text('SELECT COUNT(*) FROM production_recipes')).scalar_one()
    response = qualify(qualification)
    if name_matches:
        assert response.status_code == 409, response.text
        assert db.execute(text('SELECT COUNT(*) FROM production_recipes')).scalar_one() == before
        assert db.execute(text("SELECT COUNT(*) FROM system_config WHERE key LIKE 'outside_print_binding:%'")).scalar_one() == 0
    else:
        assert response.status_code == 200, response.text
        evidence = response.json()['recipe_revision']['outside_print_evidence']
        assert evidence['matched_by'] == 'explicit'
        assert evidence['observed_filename'] == 'cabinet lock v3.1.3mf'


@pytest.mark.parametrize('created_at,same_printer,blocked', [
    ('2026-10-27 10:00:00', True, True),
    ('2026-10-27 10:30:00', True, True),
    ('2026-10-27 11:00:00', True, True),
    ('2026-10-27 09:59:59', True, False),
    ('2026-10-27 11:00:01', True, False),
    ('2026-10-27 10:30:00', False, False),
])
def test_uncorrelated_receipt_in_print_window(qualification, created_at, same_printer, blocked):
    batch, pid, _ = qualification
    db = batch[3]
    db.execute(text("UPDATE print_jobs SET started_at='2026-10-27 10:00:00',ended_at='2026-10-27 11:00:00' WHERE id=:id"), {'id':pid})
    db.execute(text('''UPDATE dispatch_attempt_receipts SET observed_print_job_id=NULL,
        created_at=:created,printer_id=:printer WHERE job_id=302'''),
        {'created':created_at,'printer':301 if same_printer else 999})
    db.commit()
    before = db.execute(text('SELECT COUNT(*) FROM production_recipes')).scalar_one()
    response = qualify(qualification)
    assert response.status_code == (409 if blocked else 200), response.text
    if blocked:
        assert 'uncorrelated dispatch receipt' in response.json()['detail']
        assert db.execute(text('SELECT COUNT(*) FROM production_recipes')).scalar_one() == before
        assert db.execute(text("SELECT COUNT(*) FROM system_config WHERE key LIKE 'outside_print_binding:%'")).scalar_one() == 0


def test_qualification_rolls_back_on_binding_audit_failure(qualification,monkeypatch):
    from modules.orders import outside_prints
    original = outside_prints.audit
    def audit(db,action,*args):
        if action == 'production.outside_print_bound': raise RuntimeError('synthetic failure')
        return original(db,action,*args)
    monkeypatch.setattr(outside_prints,'audit',audit)
    before = qualification[0][3].execute(text('SELECT COUNT(*) FROM production_recipes')).scalar_one()
    with pytest.raises(RuntimeError,match='synthetic failure'): qualify(qualification)
    db = qualification[0][3]
    assert db.execute(text('SELECT COUNT(*) FROM production_recipes')).scalar_one() == before
    assert db.execute(text("SELECT COUNT(*) FROM audit_logs WHERE action='production.recipe_qualified_from_outside_print'")).scalar_one() == 0


@pytest.fixture
def cancel_client(batch):
    from modules.jobs.routes.jobs_agent import router
    from core.app_errors import register_error_handlers
    register_error_handlers(batch[4])
    batch[4].middleware_stack = None
    batch[4].include_router(router)
    batch[1].update(role='operator',_auth_kind='user_api_token',_token_scopes=['write:jobs'],_token_id=77)
    batch[3].execute(text("INSERT INTO jobs(id,item_name,status,charged_to_org_id,hold) VALUES(401,'Never sent','pending',1,false)"))
    batch[3].commit()
    return batch


@pytest.mark.parametrize('held',[False,True])
def test_scoped_cancel_and_token_audit(cancel_client,held):
    client,user,_,db,*_ = cancel_client
    if held:
        db.execute(text("UPDATE jobs SET status='scheduled',hold=true WHERE id=401")); db.commit()
    response = client.post('/jobs/401/cancel')
    assert response.status_code == 200, response.text
    assert response.json()['status'] == 'cancelled'
    assert json.loads(db.execute(text("SELECT details FROM audit_logs WHERE action='job.cancelled'")).scalar_one())['actor_token_id'] == 77


@pytest.mark.parametrize('reason',['receipt','claim','bound','education','scheduled','printing','foreign','read_scope','viewer'])
def test_scoped_cancel_refusals(cancel_client,reason):
    client,user,_,db,*_ = cancel_client
    if reason == 'receipt': db.execute(text('UPDATE dispatch_attempt_receipts SET job_id=401 WHERE job_id=302'))
    elif reason == 'claim': db.execute(text("INSERT INTO printer_dispatch_claims(printer_id,job_id,physical_key,configuration_key,payload_sha256,token,owner_org_id,state) VALUES(301,401,:key,:key,:key,:token,1,'uncertain')"),{'key':'f'*64,'token':'e'*32})
    elif reason == 'bound': db.execute(text('UPDATE print_jobs SET scheduled_job_id=401 WHERE scheduled_job_id=302'))
    elif reason == 'education': db.execute(text('UPDATE education_submissions SET job_id=401 WHERE id=14'))
    elif reason in ('scheduled','printing'): db.execute(text('UPDATE jobs SET status=:state WHERE id=401'),{'state':reason})
    elif reason == 'foreign': db.execute(text('UPDATE jobs SET charged_to_org_id=2 WHERE id=401'))
    elif reason == 'read_scope': user['_token_scopes'] = ['read:jobs']
    elif reason == 'viewer': user['role'] = 'viewer'
    db.commit()
    response = client.post('/jobs/401/cancel')
    assert response.status_code in ((400,) if reason=='printing' else (409,) if reason=='education' else (403,404)), response.text
    assert db.execute(text('SELECT status FROM jobs WHERE id=401')).scalar_one() != 'cancelled'
    if reason in ('receipt', 'claim', 'bound', 'scheduled'):
        assert response.json()['error']['code'] == 'permission_denied'
        assert response.json()['error']['retriable'] is False


@pytest.mark.parametrize('role', ['admin', 'operator'])
@pytest.mark.parametrize('scopes', [None, ['agent:write'], ['agent:write', 'write:jobs'], ['admin']])
def test_existing_cancel_authority_preserves_scheduled_unheld_jobs(cancel_client, role, scopes):
    client, user, _, db, *_ = cancel_client
    user.update(role=role, _auth_kind='jwt' if scopes is None else 'user_api_token', _token_scopes=scopes)
    if scopes is None:
        user.pop('_token_id')
    db.execute(text("UPDATE jobs SET status='scheduled',hold=false WHERE id=401"))
    # Existing authority also permits a scheduled job with dispatch evidence.
    db.execute(text('UPDATE dispatch_attempt_receipts SET job_id=401 WHERE job_id=302'))
    db.commit()
    response = client.post('/jobs/401/cancel')
    assert response.status_code == 200, response.text
    assert response.json()['status'] == 'cancelled'


@pytest.mark.parametrize('scopes', [['write:jobs'], ['write']])
def test_admin_owned_write_token_still_has_narrow_cancel_authority(cancel_client, scopes):
    client, user, _, db, *_ = cancel_client
    user.update(role='admin', _token_scopes=scopes)
    db.execute(text("UPDATE jobs SET status='scheduled',hold=false WHERE id=401"))
    db.commit()
    response = client.post('/jobs/401/cancel')
    assert response.status_code == 403, response.text
    assert response.json()['error']['code'] == 'permission_denied'
    assert db.execute(text('SELECT status FROM jobs WHERE id=401')).scalar_one() == 'scheduled'


def test_concurrent_qualification_has_one_recipe_and_binding(qualification):
    from concurrent.futures import ThreadPoolExecutor
    db = qualification[0][3]
    before = db.execute(text('SELECT COUNT(*) FROM production_recipes')).scalar_one()
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _:qualify(qualification),range(2)))
    assert sorted(r.status_code for r in responses) == [200,409]
    assert db.execute(text('SELECT COUNT(*) FROM production_recipes')).scalar_one() == before+1
    assert db.execute(text("SELECT COUNT(*) FROM audit_logs WHERE action='production.recipe_qualified_from_outside_print'")).scalar_one() == 1


from tests.test_contracts.test_api_token_delegation_behavior import token_client

@pytest.mark.parametrize('scopes',[['write:jobs'],['write']])
def test_real_operator_token_can_cancel_never_dispatched_job(token_client,review_db,monkeypatch,scopes):
    from modules.jobs.routes.jobs_agent import router
    from core.registry import registry
    from modules.organizations.services import EducationPolicyService
    monkeypatch.setitem(registry._providers,'EducationPolicyProvider',EducationPolicyService())
    client,issue = token_client
    client.app.include_router(router)
    review_db.execute(text("UPDATE users SET role='operator' WHERE id=3"))
    review_db.execute(text("INSERT INTO jobs(id,item_name,status,charged_to_org_id,hold) VALUES(401,'Never dispatched','pending',1,false)"))
    review_db.commit()
    response = client.post('/jobs/401/cancel',headers=issue(scopes))
    assert response.status_code == 200, response.text
    audit = json.loads(review_db.execute(text("SELECT details FROM audit_logs WHERE action='job.cancelled'")).scalar_one())
    token_id = review_db.execute(text("SELECT id FROM api_tokens WHERE user_id=3")).scalar_one()
    assert audit['actor_token_id'] == token_id and audit['actor_user_id'] == 3


def test_cancel_rechecks_dispatch_fence_after_parent_locks(cancel_client,monkeypatch):
    from modules.jobs.routes import jobs_agent
    original = jobs_agent.prepare_job_write
    def prepare(db,ids):
        result = original(db,ids)
        db.execute(text('UPDATE dispatch_attempt_receipts SET job_id=401 WHERE job_id=302'))
        return result
    monkeypatch.setattr(jobs_agent,'prepare_job_write',prepare)
    response = cancel_client[0].post('/jobs/401/cancel')
    assert response.status_code == 403
    assert cancel_client[3].execute(text('SELECT status FROM jobs WHERE id=401')).scalar_one() == 'pending'
    assert cancel_client[3].execute(text("SELECT COUNT(*) FROM audit_logs WHERE action='job.cancelled'")).scalar_one() == 0
