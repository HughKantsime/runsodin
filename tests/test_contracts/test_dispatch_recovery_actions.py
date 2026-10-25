"""Recovery test-plan mapping: HTTP, CAS replay, authority, telemetry, audit.
Synthetic peers only: these tests never connect to a printer.
"""
import importlib
import json
import pytest
from sqlalchemy import text
from tests.test_contracts.test_dispatch_recovery_inspection import recovery_client, control_client, token_client, review_db

ATTEST = dict(version=1,printer_checked=True,sender_stopped_and_checked=True,
              transport_and_device_queues_checked=True,no_pending_dispatch_can_arrive=True)

@pytest.fixture
def actions(recovery_client,review_db):
    from modules.printers.routes_dispatch_recovery import configured_identity
    client,issue,calls = recovery_client
    review_db.execute(text("INSERT INTO jobs(id,item_name,printer_id,charged_to_org_id,status,hold) VALUES(953,'Synthetic',299,1,'scheduled',0)"))
    review_db.execute(text("UPDATE printers SET api_type='moonraker',api_host='127.0.0.1:7125',last_seen=CURRENT_TIMESTAMP,gcode_state='IDLE' WHERE id=299"))
    identity = configured_identity(299,review_db.connection())
    assert identity is not None
    review_db.execute(text('UPDATE printer_dispatch_claims SET physical_key=:target,configuration_key=:configuration WHERE printer_id=299'),dict(target=identity.target_key,configuration=identity.configuration_key))
    importlib.import_module('core.schema.migrations.038_dispatch_recovery').apply(review_db.connection())
    review_db.commit()
    def inspect():
        response = client.get('/printers/299/dispatch-recovery',headers=issue(['read:printers']))
        assert response.status_code==200,response.text
        return response.json()
    def post(action, snapshot=None, **extras):
        snapshot = snapshot or inspect()
        body = dict(attempt_handle=snapshot['attempt_handle'],snapshot_handle=snapshot['snapshot_handle'],reason_category='verified_no_pending_dispatch' if action=='release' else 'investigation')
        if action=='release': body['attestation']=ATTEST.copy()
        if action=='reconcile-retain': body['education_action']='none'
        body.update(extras)
        return client.post('/printers/299/dispatch-recovery/'+action,headers=issue(['write:printers']),json=body)
    return client,issue,calls,inspect,post


def test_hold_retain_release_and_replay(actions,review_db):
    client,issue,calls,inspect,post=actions
    initial=inspect()
    held=post('hold',initial)
    assert held.status_code==200,held.text
    assert held.json()['held'] and held.json()['claim_retained']
    assert post('hold',initial).json()['detail']['code']=='snapshot_changed'
    retained=post('reconcile-retain')
    assert retained.status_code==200,retained.text
    snapshot=inspect()
    released=post('release',snapshot)
    assert released.status_code==200,released.text
    assert released.json()['outcome']=='released' and not released.json()['claim_retained']
    assert post('release',snapshot).json()['detail']['code']=='attempt_changed'
    assert review_db.execute(text('SELECT status,hold FROM jobs WHERE id=953')).one()==('scheduled',1)
    assert review_db.execute(text('SELECT count(*) FROM printer_dispatch_claims WHERE printer_id=299')).scalar_one()==0
    history=review_db.execute(text('SELECT facts FROM dispatch_recovery_history ORDER BY id')).scalars().all()
    assert len(history)==3 and json.loads(history[-1])['attestation']==ATTEST
    assert calls==[]


@pytest.mark.parametrize('change,code',[
    ("UPDATE printers SET last_seen=datetime(CURRENT_TIMESTAMP,'-11 seconds') WHERE id=299",'status_unavailable'),
    ("UPDATE printers SET gcode_state='RUNNING' WHERE id=299",'active_observation'),
    ("UPDATE printers SET gcode_state=NULL WHERE id=299",'status_unavailable'),
    ("UPDATE printers SET api_host='127.0.0.2:7125' WHERE id=299",'configuration_changed'),
])
def test_release_uses_passive_fresh_current_telemetry(actions,review_db,change,code):
    *_,inspect,post=actions
    assert post('hold').status_code==200
    review_db.execute(text(change));review_db.commit()
    response=post('release')
    assert response.status_code==409,response.text
    assert response.json()['detail']['code']==code
    assert review_db.execute(text('SELECT count(*) FROM printer_dispatch_claims')).scalar_one()==1


def test_stale_snapshot_terminal_and_replacement_refuse(actions,review_db):
    *_,inspect,post=actions
    assert post('hold').status_code==200
    snapshot=inspect()
    review_db.execute(text("UPDATE jobs SET status='completed',actual_end=CURRENT_TIMESTAMP WHERE id=953"));review_db.commit()
    assert post('release',snapshot).json()['detail']['code']=='snapshot_changed'
    review_db.execute(text("UPDATE printer_dispatch_claims SET token=:token WHERE printer_id=299"),{'token':'e'*32});review_db.commit()
    assert post('release',snapshot).json()['detail']['code']=='attempt_changed'


def test_target_busy_refuses_before_release(actions,review_db):
    from core.dispatch_target_lock import acquire_target_lock
    *_,inspect,post=actions
    assert post('hold').status_code==200
    key=review_db.execute(text('SELECT physical_key FROM printer_dispatch_claims')).scalar_one()
    review_db.rollback()
    lock=acquire_target_lock(review_db.get_bind(),key)
    assert lock.status=='acquired'
    try:
        response=post('release')
        assert response.status_code==409,response.text
        assert response.json()['detail']['code']=='target_busy'
    finally: lock.handle.close()


@pytest.mark.parametrize('attestation', [{},dict(ATTEST,printer_checked=False),dict(ATTEST,printer_checked=1),dict(ATTEST,version=True),dict(ATTEST,extra=True)])
def test_attestation_strict(actions,attestation):
    *_,post=actions
    assert post('release',attestation=attestation).status_code==422


@pytest.mark.parametrize('change',[
    'UPDATE printers SET org_id=2,shared=1 WHERE id=299',
    'UPDATE jobs SET charged_to_org_id=2 WHERE id=953',
    'UPDATE printer_dispatch_claims SET owner_org_id=NULL WHERE printer_id=299',
    'DELETE FROM jobs WHERE id=953',
])
def test_cross_tenant_and_orphan_mutations_hidden(actions,review_db,change):
    *_,inspect,post=actions
    snapshot=inspect()
    review_db.execute(text(change));review_db.commit()
    assert post('hold',snapshot).status_code==404


def test_atomic_history_failure_rolls_back_hold(actions,review_db):
    *_,post=actions
    review_db.execute(text("CREATE TRIGGER synthetic_history_failure BEFORE INSERT ON dispatch_recovery_history BEGIN SELECT RAISE(ABORT,'Synthetic'); END"));review_db.commit()
    response=post('hold')
    assert response.status_code==503,response.text
    assert response.json()['detail']['code']=='recovery_unavailable'
    assert review_db.execute(text('SELECT hold FROM jobs WHERE id=953')).scalar_one()==0


def test_history_is_immutable_and_independent(actions,review_db):
    *_,post=actions
    assert post('hold').status_code==200
    for sql in ('UPDATE dispatch_recovery_history SET decision=\'release\'','DELETE FROM dispatch_recovery_history'):
        with pytest.raises(Exception): review_db.execute(text(sql))
        review_db.rollback()
    review_db.execute(text('DELETE FROM jobs WHERE id=953'));review_db.commit()
    assert review_db.execute(text('SELECT count(*) FROM dispatch_recovery_history')).scalar_one()==1


def test_write_scope_and_roles_are_enforced(actions,review_db):
    client,issue,calls,inspect,post=actions
    snapshot=inspect()
    body=dict(attempt_handle=snapshot['attempt_handle'],snapshot_handle=snapshot['snapshot_handle'],reason_category='investigation')
    assert client.post('/printers/299/dispatch-recovery/hold',headers=issue(['read:printers']),json=body).status_code==403
    review_db.execute(text("UPDATE users SET role='operator' WHERE id=3"));review_db.commit()
    assert client.post('/printers/299/dispatch-recovery/hold',headers=issue(['write:printers']),json=body).status_code==403
    assert calls==[]


def test_hold_required_and_running_observation(actions,review_db):
    *_,inspect,post=actions
    assert post('release').json()['detail']['code']=='hold_required'
    assert post('hold').status_code==200
    review_db.execute(text("INSERT INTO print_jobs(id,printer_id,status,started_at) VALUES(954,299,'running',CURRENT_TIMESTAMP)"));review_db.commit()
    assert post('release').json()['detail']['code']=='active_observation'


@pytest.mark.parametrize('reservation_state',['reserved','awaiting','running'])
def test_claimless_education_reservation_is_inspected_retained_and_never_released(actions,review_db,monkeypatch,reservation_state):
    from core.registry import registry
    from modules.organizations.services import EducationPolicyService
    monkeypatch.setitem(registry._providers,'EducationPolicyProvider',EducationPolicyService())
    client,issue,calls,inspect,post=actions
    review_db.execute(text('DELETE FROM printer_dispatch_claims WHERE printer_id=299'))
    review_db.execute(text('DELETE FROM jobs WHERE id=953'))
    review_db.execute(text("UPDATE jobs SET printer_id=299,status='scheduled' WHERE id=13"))
    review_db.execute(text("UPDATE education_submissions SET approved_printer_id=299,status='scheduled' WHERE id=14"))
    review_db.execute(text("INSERT INTO education_cost_center_printers(org_id,cost_center_id,printer_id,state,granted_by) VALUES(1,7,299,'active',3)"))
    review_db.execute(text("INSERT INTO education_monitor_claims(claim_id,org_id,submission_id,job_id,printer_id,authority_revision,token_digest,state) VALUES('synthetic',1,14,13,299,1,:digest,:state)"),{'digest':'c'*64,'state':reservation_state})
    if reservation_state=='running':
        review_db.execute(text("UPDATE jobs SET status='printing' WHERE id=13"))
        review_db.execute(text("UPDATE education_submissions SET status='printing' WHERE id=14"))
    review_db.commit()
    snapshot=inspect()
    assert snapshot['state']=='education_reservation_retained'
    assert snapshot['education']['reservation_state']==reservation_state
    assert not snapshot['actions']['reconcile_retain']
    # School authority additionally requires an Education write scope.
    body=dict(attempt_handle=snapshot['attempt_handle'],snapshot_handle=snapshot['snapshot_handle'],reason_category='investigation',education_action='none' if reservation_state=='running' else 'request_unbound_cancellation')
    response=client.post('/printers/299/dispatch-recovery/reconcile-retain',headers=issue(['write:printers','write:education']),json=body)
    assert response.status_code==200,response.text
    assert review_db.execute(text('SELECT state FROM education_monitor_claims')).scalar_one()==reservation_state
    assert review_db.execute(text('SELECT status,hold FROM jobs WHERE id=13')).one()==('printing' if reservation_state=='running' else 'scheduled',1)
    snapshot=inspect()
    body=dict(attempt_handle=snapshot['attempt_handle'],snapshot_handle=snapshot['snapshot_handle'],reason_category='verified_no_pending_dispatch',attestation=ATTEST)
    response=client.post('/printers/299/dispatch-recovery/release',headers=issue(['write:printers','write:education']),json=body)
    assert response.status_code==409 and response.json()['detail']['code']=='education_authority_unresolved'
    assert calls==[]


def test_alias_printer_running_observation_blocks_release(actions,review_db):
    *_,inspect,post=actions
    assert post('hold').status_code==200
    review_db.execute(text("INSERT INTO printers(id,name,is_active,org_id,api_type,api_host) VALUES(300,'Synthetic alias',1,1,'moonraker','127.0.0.1:7125')"))
    review_db.execute(text("INSERT INTO print_jobs(id,printer_id,status,started_at) VALUES(954,300,'running',CURRENT_TIMESTAMP)"));review_db.commit()
    response=post('release')
    assert response.status_code==409 and response.json()['detail']['code']=='active_observation'


def test_release_lost_lock_retains_claim(actions,monkeypatch):
    from core.dispatch_target_lock import TargetLockResult
    *_,post=actions
    class Lost:
        def held(self): return False
        def close(self): return type('Closed',(),{'released':False})()
    monkeypatch.setattr('core.dispatch_target_lock.acquire_target_lock',lambda *args:TargetLockResult('acquired',Lost()))
    assert post('release').json()['detail']['code']=='evidence_unavailable'


def test_both_api_prefixes_use_identical_handler_and_contract(actions):
    from modules.printers.routes_dispatch_recovery import router
    client,issue,calls,inspect,post=actions
    client.app.include_router(router,prefix='/api')
    client.app.include_router(router,prefix='/api/v1')
    headers=issue(['read:printers','write:printers'])
    responses=[client.get(prefix+'/printers/299/dispatch-recovery',headers=headers) for prefix in ('/api','/api/v1')]
    assert responses[0].json()==responses[1].json()
    snapshot=responses[0].json()
    assert snapshot['actions']['hold']
    response=client.post('/api/v1/printers/299/dispatch-recovery/hold',headers=headers,json={k:snapshot[k] for k in ('attempt_handle','snapshot_handle')}|{'reason_category':'investigation'})
    assert response.status_code==200,response.text
    refreshed=client.get('/api/printers/299/dispatch-recovery',headers=headers).json()
    assert refreshed['release_available'] and refreshed['actions']['release']
    assert calls==[]


def test_unknown_commit_forces_inspection_even_if_committed(actions,review_db,monkeypatch):
    from core.dispatch_recovery import mutate
    from modules.printers.routes_dispatch_recovery import HoldRequest
    from sqlalchemy.orm import Session
    *_,inspect,post=actions
    snapshot=inspect()
    with Session(review_db.get_bind()) as db:
        original=db.commit
        def uncertain():
            original()
            raise RuntimeError('Synthetic connection loss after commit')
        monkeypatch.setattr(db,'commit',uncertain)
        with pytest.raises(Exception) as failure:
            mutate(db,299,dict(id=3,username='admin',role='admin',group_id=1),HoldRequest(**{k:snapshot[k] for k in ('attempt_handle','snapshot_handle')},reason_category='investigation'),'hold',lambda conn,pid=299:None)
        assert failure.value.status_code==503 and failure.value.detail['code']=='outcome_unknown'
    assert inspect()['job']['held']


def test_active_sender_registration_refuses_release(actions,review_db):
    from core.dispatch_sender_schema import DispatchSenderRegistration
    *_,inspect,post=actions
    assert post('hold').status_code==200
    claim=review_db.execute(text('SELECT * FROM printer_dispatch_claims')).mappings().one()
    fields={k:claim[k] for k in ('printer_id','job_id','physical_key','configuration_key','payload_sha256','owner_org_id')}
    fields.update(claim_token=claim['token'],origin_key='a'*32,runtime_incarnation='b'*32,registration_key='c'*32,state='active')
    review_db.execute(DispatchSenderRegistration.__table__.insert().values(**fields));review_db.commit()
    response=post('release')
    assert response.status_code==409,response.text
    assert response.json()['detail']['code']=='sender_quiescence_unverified'


def test_concurrent_second_release_cannot_clear_again(actions,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from core import dispatch_target_lock
    client,issue,calls,inspect,post=actions
    assert post('hold').status_code==200
    snapshot=inspect();headers=issue(['write:printers'])
    body={k:snapshot[k] for k in ('attempt_handle','snapshot_handle')}|{'reason_category':'verified_no_pending_dispatch','attestation':ATTEST}
    acquired=Event();finish=Event();real=dispatch_target_lock.acquire_target_lock
    def pause(*args):
        result=real(*args)
        if result.status=='acquired':
            acquired.set()
            assert finish.wait(10)
        return result
    monkeypatch.setattr(dispatch_target_lock,'acquire_target_lock',pause)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first=pool.submit(client.post,'/printers/299/dispatch-recovery/release',headers=headers,json=body)
        assert acquired.wait(10)
        second=client.post('/printers/299/dispatch-recovery/release',headers=headers,json=body)
        finish.set()
        assert second.status_code==409 and second.json()['detail']['code']=='target_busy'
        response=first.result(timeout=10)
        assert response.status_code==200,response.text
    assert calls==[]


def test_terminal_transaction_wins_release_snapshot_race(actions,review_db,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from core import dispatch_target_lock
    client,issue,calls,inspect,post=actions
    assert post('hold').status_code==200
    snapshot=inspect();headers=issue(['write:printers'])
    body={k:snapshot[k] for k in ('attempt_handle','snapshot_handle')}|{'reason_category':'verified_no_pending_dispatch','attestation':ATTEST}
    acquired=Event();finish=Event();real=dispatch_target_lock.acquire_target_lock
    def pause(*args):
        result=real(*args)
        if result.status=='acquired':
            acquired.set();assert finish.wait(10)
        return result
    monkeypatch.setattr(dispatch_target_lock,'acquire_target_lock',pause)
    with ThreadPoolExecutor(max_workers=1) as pool:
        released=pool.submit(client.post,'/printers/299/dispatch-recovery/release',headers=headers,json=body)
        assert acquired.wait(10)
        with review_db.get_bind().begin() as conn:
            conn.execute(text("UPDATE jobs SET status='completed',actual_end=CURRENT_TIMESTAMP WHERE id=953"))
        finish.set()
        response=released.result(timeout=10)
        assert response.status_code==409 and response.json()['detail']['code']=='snapshot_changed'
    assert inspect()['state']=='uncertain' and calls==[]


def test_close_uncertainty_after_commit_is_sanitized_unknown(actions,monkeypatch):
    from core import dispatch_target_lock
    client,issue,calls,inspect,post=actions
    assert post('hold').status_code==200
    real=dispatch_target_lock.acquire_target_lock
    class UncertainClose:
        def __init__(self,handle):self.handle=handle
        def held(self): return self.handle.held()
        def close(self):
            self.handle.close()
            raise RuntimeError('Synthetic private driver detail')
    def acquire(*args):
        result=real(*args)
        return dispatch_target_lock.TargetLockResult(result.status,UncertainClose(result.handle)) if result.status=='acquired' else result
    monkeypatch.setattr(dispatch_target_lock,'acquire_target_lock',acquire)
    response=post('release')
    assert response.status_code==503 and response.json()['detail']['code']=='outcome_unknown'
    assert 'Synthetic private driver detail' not in response.text
    assert inspect()['state']=='no_retained_claim'


@pytest.mark.parametrize('same_printer,same_target,blocked',[(True,False,True),(False,True,True),(False,False,False)])
def test_power_fences_match_printer_or_configured_target(actions,review_db,same_printer,same_target,blocked):
    from core.power_target_operations import PowerFacts, _stage_operation
    *_,inspect,post=actions
    assert post('hold').status_code==200
    claim=review_db.execute(text('SELECT * FROM printer_dispatch_claims')).mappings().one()
    facts=PowerFacts(printer_id=299 if same_printer else 300,target_key=claim['physical_key'] if same_target else 'f'*64,
                     config_key='a'*64,destination_key='b'*64,purpose='observed_on',physical_job_id=999,observation_key='c'*64)
    _stage_operation(review_db.connection(),facts);review_db.commit()
    response=post('release')
    if blocked:
        assert response.status_code==409 and response.json()['detail']['code']=='sender_quiescence_unverified'
    else:
        assert response.status_code==200,response.text


@pytest.mark.parametrize('invalid_hold',[2,-1,'true'])
def test_release_requires_exact_stored_true_hold(actions,review_db,invalid_hold):
    *_,post=actions
    review_db.execute(text('UPDATE jobs SET hold=:value WHERE id=953'),{'value':invalid_hold});review_db.commit()
    response=post('release')
    assert response.status_code==409 and response.json()['detail']['code']=='hold_required'


def test_invalid_requests_do_not_echo_inputs_or_cache_handles(actions):
    client,issue,calls,inspect,post=actions
    snapshot=inspect()
    response=post('hold',snapshot,unexpected='SYNTHETIC_PRIVATE_INPUT_SENTINEL')
    assert response.status_code==422
    assert response.json()['detail']['code']=='invalid_request'
    assert 'SYNTHETIC_PRIVATE_INPUT_SENTINEL' not in response.text
    response=client.get('/printers/299/dispatch-recovery',headers=issue(['read:printers']))
    assert response.headers['Cache-Control']=='private, no-store'
    assert calls==[]
