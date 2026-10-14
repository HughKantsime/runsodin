"""Real admitted receipt, bound observation, terminal handler and next dispatch."""
from contextlib import contextmanager
import pytest
from sqlalchemy import text
from core.dispatch_observations import bind_generic_observation
from core.db_utils import _ConnectionAdapter
from tests.test_contracts.test_dispatch_observation_binding_behavior import observed
from tests.test_contracts.test_education_review_workflow import review_db


def finish(engine,monkeypatch,status,*,defer_retirement=False):
    from modules.printers.monitors import mqtt_job_lifecycle as lifecycle
    from modules.notifications import alert_dispatch,event_dispatcher
    from modules.archives import archive
    @contextmanager
    def owned_db():
        raw=engine.raw_connection()
        try:yield _ConnectionAdapter(raw,False)
        finally:raw.close()
    monkeypatch.setattr(lifecycle,'get_db',owned_db)
    monkeypatch.setattr(alert_dispatch,'dispatch_alert',lambda **kw:None)
    monkeypatch.setattr(alert_dispatch,'_start_bed_cooled_monitor',lambda *a:None)
    monkeypatch.setattr(event_dispatcher,'increment_care_counters',lambda *a:None)
    monkeypatch.setattr(event_dispatcher,'record_error',lambda **kw:None)
    monkeypatch.setattr(archive,'create_print_archive',lambda **kw:None)
    with engine.connect() as c:name=c.execute(text('SELECT filename FROM print_jobs WHERE id=70')).scalar_one()
    held=None
    if defer_retirement:
        from core.dispatch_target_lock import acquire_target_lock
        with engine.connect() as c:physical=c.execute(text('SELECT physical_key FROM printer_dispatch_claims WHERE printer_id=59')).scalar_one()
        acquired=acquire_target_lock(engine,physical);assert acquired.status=='acquired';held=acquired.handle
    try:
        lifecycle.record_job_ended(printer_id=59,printer_name='Owned fixture',current_job_id=70,
            linked_job_id=53,status=status,state={'subtask_name':name,'print_error':0},
            start_spool_weights={},dispatch_alert_fn=lambda **kw:None)
    finally:
        if held is not None:assert held.close().released

    with engine.connect() as c:
        assert c.execute(text('SELECT status FROM print_jobs WHERE id=70')).scalar_one()==status
        assert c.execute(text('SELECT status FROM jobs WHERE id=53')).scalar_one()==status


@pytest.mark.parametrize('terminal',['completed','cancelled','failed'])
def test_normal_terminal_allows_next_dispatch_without_erasing_receipt(observed,monkeypatch,terminal):
    from modules.printers import dispatch
    engine,key=observed
    # The reused binding fixture also creates a competing observation. This
    # case models one physical print; a separate case preserves that blocker.
    with engine.begin() as c:c.execute(text('DELETE FROM print_jobs WHERE id=71'))
    assert bind_generic_observation(engine,printer_id=59,print_job_id=70,key=key)=='matched'
    finish(engine,monkeypatch,terminal)
    with engine.connect() as c:original=c.execute(text('SELECT claim_token FROM dispatch_attempt_receipts WHERE job_id=53')).scalar_one()
    ok,reason=dispatch.dispatch_job(59,54)
    assert ok,reason
    with engine.connect() as c:
        assert c.execute(text('SELECT COUNT(*) FROM dispatch_attempt_receipts WHERE claim_token=:token AND observed_print_job_id=70'),{'token':original}).scalar_one()==1
        assert c.execute(text('SELECT job_id FROM printer_dispatch_claims WHERE printer_id=59')).scalar_one()==54


@pytest.mark.parametrize('change',[
    "UPDATE jobs SET status='pending' WHERE id=53",
    "UPDATE jobs SET actual_end=NULL WHERE id=53",
    "UPDATE jobs SET actual_end='invalid' WHERE id=53",
    "UPDATE printer_dispatch_claims SET owner_org_id=2 WHERE job_id=53",
    "UPDATE jobs SET payload_sha256='ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff' WHERE id=53",
    "UPDATE print_jobs SET status='running' WHERE id=70",
    "UPDATE print_jobs SET ended_at=NULL WHERE id=70",
    "UPDATE print_jobs SET scheduled_job_id=54 WHERE id=70",
    "UPDATE print_jobs SET printer_id=9 WHERE id=70",
    "UPDATE print_jobs SET filename='unrelated.gcode' WHERE id=70",
    "UPDATE dispatch_attempt_receipts SET observed_print_job_id=NULL WHERE job_id=53",
    "UPDATE dispatch_attempt_receipts SET configuration_key='ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff' WHERE job_id=53",
    "DELETE FROM dispatch_attempt_receipts WHERE job_id=53",
    "UPDATE printer_dispatch_claims SET state='uncertain' WHERE job_id=53",
    "UPDATE printer_dispatch_claims SET state='adapter_entered' WHERE job_id=53",
    "UPDATE printers SET api_host='different.example.test' WHERE id=59",
])
def test_contradictory_terminal_evidence_keeps_original_fence(observed,monkeypatch,change):
    from modules.printers import dispatch
    engine,key=observed
    with engine.begin() as c:c.execute(text('DELETE FROM print_jobs WHERE id=71'))
    assert bind_generic_observation(engine,printer_id=59,print_job_id=70,key=key)=='matched'
    finish(engine,monkeypatch,'completed',defer_retirement=True)
    with engine.begin() as c:c.execute(text(change))
    calls=[];monkeypatch.setattr(dispatch,'_dispatch_moonraker',lambda *a:(calls.append(True) or True,'Synthetic accepted'))
    assert not dispatch.dispatch_job(59,54)[0]
    assert calls==[]
    with engine.connect() as c:assert c.execute(text('SELECT job_id FROM printer_dispatch_claims WHERE printer_id=59')).scalar_one()==53


@pytest.mark.parametrize('state',['active','cleanup_uncertain'])
@pytest.mark.parametrize('matching',['printer','physical'])
def test_unresolved_sender_history_blocks_retirement(observed,monkeypatch,state,matching):
    from modules.printers import dispatch
    engine,key=observed
    with engine.begin() as c:c.execute(text('DELETE FROM print_jobs WHERE id=71'))
    assert bind_generic_observation(engine,printer_id=59,print_job_id=70,key=key)=='matched'
    finish(engine,monkeypatch,'completed',defer_retirement=True)
    with engine.begin() as c:
        row=c.execute(text('SELECT * FROM printer_dispatch_claims')).mappings().one()
        c.execute(text("INSERT INTO dispatch_sender_registrations(claim_token,printer_id,job_id,physical_key,configuration_key,payload_sha256,owner_org_id,origin_key,runtime_incarnation,registration_key,state,finished_at) VALUES(:token,:pid,53,:physical,:config,:payload,1,:origin,:runtime,:registration,:state,CASE WHEN :state='active' THEN NULL ELSE CURRENT_TIMESTAMP END)"),
          {'token':'f'*32,'pid':59 if matching=='printer' else 9,'physical':row['physical_key'] if matching=='physical' else 'f'*64,'config':row['configuration_key'],'payload':row['payload_sha256'],'origin':'a'*32,'runtime':'b'*32,'registration':'c'*32,'state':state})
    assert not dispatch.dispatch_job(59,54)[0]
    with engine.connect() as c:assert c.execute(text('SELECT job_id FROM printer_dispatch_claims WHERE printer_id=59')).scalar_one()==53


def test_unrelated_running_observation_still_blocks_new_adapter(observed,monkeypatch):
    from modules.printers import dispatch
    engine,key=observed
    assert bind_generic_observation(engine,printer_id=59,print_job_id=70,key=key)=='matched'
    finish(engine,monkeypatch,'completed')
    calls=[];monkeypatch.setattr(dispatch,'_dispatch_moonraker',lambda *a:(calls.append(True) or True,'Synthetic accepted'))
    assert not dispatch.dispatch_job(59,54)[0]
    assert calls==[]
    with engine.connect() as c:assert c.execute(text('SELECT status FROM print_jobs WHERE id=71')).scalar_one()=='running'


def test_committed_then_error_retirement_refuses_this_dispatch(observed,monkeypatch):
    from modules.printers import dispatch
    from core import dispatch_claims
    engine,key=observed
    with engine.begin() as c:c.execute(text('DELETE FROM print_jobs WHERE id=71'))
    assert bind_generic_observation(engine,printer_id=59,print_job_id=70,key=key)=='matched'
    finish(engine,monkeypatch,'completed',defer_retirement=True)
    original=dispatch_claims.retire_terminal_locked
    class UncertainCommit:
        @contextmanager
        def begin(self):
            with engine.begin() as c:yield c
            raise RuntimeError('Synthetic lost commit acknowledgment')
    monkeypatch.setattr(dispatch_claims,'retire_terminal_locked',lambda unused,**kw:original(UncertainCommit(),**kw))
    calls=[];monkeypatch.setattr(dispatch,'_dispatch_moonraker',lambda *a:(calls.append(True) or True,'Synthetic accepted'))
    assert not dispatch.dispatch_job(59,54)[0]
    assert calls==[]
    with engine.connect() as c:
        assert c.execute(text('SELECT COUNT(*) FROM printer_dispatch_claims')).scalar_one()==0
        assert c.execute(text('SELECT COUNT(*) FROM dispatch_attempt_receipts WHERE job_id=53')).scalar_one()==1


def test_concurrent_next_requests_enter_only_one_adapter(observed,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from modules.printers import dispatch
    engine,key=observed
    with engine.begin() as c:c.execute(text('DELETE FROM print_jobs WHERE id=71'))
    assert bind_generic_observation(engine,printer_id=59,print_job_id=70,key=key)=='matched'
    finish(engine,monkeypatch,'completed')
    calls=[];monkeypatch.setattr(dispatch,'_dispatch_moonraker',lambda *a:(calls.append(True) or True,'Synthetic accepted'))
    with ThreadPoolExecutor(2) as pool:
        futures=[pool.submit(dispatch.dispatch_job,59,54) for _ in range(2)]
        results=[f.result(timeout=5) for f in futures]
    assert sum(ok is True for ok,_ in results)==1
    assert calls==[True]
    with engine.connect() as c:
        assert c.execute(text('SELECT job_id FROM printer_dispatch_claims WHERE printer_id=59')).scalar_one()==54
        assert c.execute(text('SELECT COUNT(*) FROM dispatch_attempt_receipts')).scalar_one()==2


def test_dispatch_cleanup_retires_terminal_that_precedes_accepted_outcome(observed,monkeypatch):
    from core import dispatch_claims
    from modules.printers import dispatch
    from modules.printers.monitors import moonraker_monitor
    from core import event_bus
    from core.event_bus import InMemoryEventBus
    from core.monitor_bootstrap import bootstrap_monitor_authority
    from core.registry import registry
    from types import SimpleNamespace
    engine,key=observed
    monkeypatch.setattr(registry,'_providers',dict(registry._providers))
    bus=InMemoryEventBus()
    monkeypatch.setattr(event_bus,'get_event_bus',lambda:bus)
    bootstrap_monitor_authority()
    monkeypatch.setattr(moonraker_monitor,'engine',engine)
    with engine.begin() as c:c.execute(text('DELETE FROM print_jobs WHERE id=71'))
    assert bind_generic_observation(engine,printer_id=59,print_job_id=70,key=key)=='matched'
    finish(engine,monkeypatch,'completed')
    names=[]
    monkeypatch.setattr(dispatch,'_dispatch_moonraker',lambda job,path,name,creds:(names.append(name) or True,'Synthetic accepted'))
    original=dispatch_claims.record_outcome
    def terminal_before_accept(storage,token,status):
        if status=='accepted':
            state=SimpleNamespace(filename=names[-1],total_layers=10,bed_target=60,
                nozzle_target=200,filament_used_mm=0,error_message=None,raw_print_state='complete')
            monitor=moonraker_monitor.MoonrakerMonitor(59,'Owned fixture','127.0.0.1')
            monitor._job_started(state)
            identifier=monitor._current_job_db_id
            assert identifier is not None
            monitor._job_ended('completed',state,education_terminal_status='completed')
            with engine.connect() as c:
                assert c.execute(text('SELECT scheduled_job_id FROM print_jobs WHERE id=:id'),{'id':identifier}).scalar_one()==54
                assert c.execute(text('SELECT status FROM print_jobs WHERE id=:id'),{'id':identifier}).scalar_one()=='completed'
            with engine.connect() as c:
                assert c.execute(text('SELECT state FROM printer_dispatch_claims WHERE printer_id=59')).scalar_one()=='adapter_entered'
        return original(storage,token,status)
    monkeypatch.setattr(dispatch_claims,'record_outcome',terminal_before_accept)
    assert dispatch.dispatch_job(59,54)[0]
    with engine.connect() as c:
        assert c.execute(text('SELECT COUNT(*) FROM printer_dispatch_claims')).scalar_one()==0
        assert c.execute(text('SELECT status FROM jobs WHERE id=54')).scalar_one()=='completed'
        assert c.execute(text('SELECT COUNT(*) FROM dispatch_attempt_receipts WHERE observed_print_job_id IS NOT NULL')).scalar_one()==2


@pytest.mark.parametrize("terminal",["completed","cancelled","failed"])
@pytest.mark.parametrize("next_job",["authorized","private_printer","foreign_payload"])
def test_shared_printer_terminal_allows_next_tenant_job(review_db,tmp_path,monkeypatch,terminal,next_job):
    from core import auth
    from modules.printers import dispatch
    from tests.test_contracts.dispatch_test_support import configure_dispatch
    engine,_=configure_dispatch(review_db,tmp_path,monkeypatch)
    names=[]
    monkeypatch.setattr(dispatch,'_dispatch_moonraker',lambda jid,path,name,creds:(names.append(name) or True,'Synthetic accepted'))
    with engine.begin() as c:
        c.execute(text('UPDATE printers SET shared=TRUE WHERE id=59'))
        c.execute(text('UPDATE jobs SET charged_to_org_id=2 WHERE id IN (53,54)'))
        c.execute(text('UPDATE models SET org_id=2 WHERE id=51'))
        c.execute(text('UPDATE print_files SET org_id=2 WHERE id=52'))
    ok,reason=dispatch.dispatch_job(59,53)
    assert ok,reason
    with engine.begin() as c:
        c.execute(text("INSERT INTO print_jobs(id,printer_id,filename,job_name,scheduled_job_id,status,started_at,observed_received_at) VALUES(70,59,:name,:name,53,'running',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"),{'name':names[0]})
    assert bind_generic_observation(engine,printer_id=59,print_job_id=70,key=auth.SECRET_KEY.encode())=='matched'
    finish(engine,monkeypatch,terminal)
    with engine.connect() as c:
        assert c.execute(text('SELECT COUNT(*) FROM printer_dispatch_claims WHERE job_id=53')).scalar_one()==0
        assert c.execute(text('SELECT observed_print_job_id FROM dispatch_attempt_receipts WHERE job_id=53')).scalar_one()==70
    if next_job!='authorized':
        with engine.begin() as c:
            if next_job=='private_printer':c.execute(text('UPDATE printers SET shared=FALSE WHERE id=59'))
            else:c.execute(text('UPDATE jobs SET charged_to_org_id=1 WHERE id=54'))
    ok,reason=dispatch.dispatch_job(59,54)
    if next_job=='authorized':
        assert ok,reason
        assert len(names)==2
    else:
        assert not ok,reason
        assert len(names)==1
        with engine.connect() as c:assert c.execute(text('SELECT COUNT(*) FROM printer_dispatch_claims')).scalar_one()==0


@pytest.mark.parametrize('tenant',[None,2])
def test_billed_tenant_change_does_not_reinterpret_proven_terminal(observed,monkeypatch,tenant):
    from core.dispatch_claims import retire_observed_terminal
    engine,key=observed
    with engine.begin() as c:c.execute(text('DELETE FROM print_jobs WHERE id=71'))
    assert bind_generic_observation(engine,printer_id=59,print_job_id=70,key=key)=='matched'
    finish(engine,monkeypatch,'completed',defer_retirement=True)
    with engine.begin() as c:c.execute(text('UPDATE jobs SET charged_to_org_id=:tenant WHERE id=53'),{'tenant':tenant})
    assert retire_observed_terminal(engine,printer_id=59,print_job_id=70)=='retired'
    with engine.connect() as c:
        assert c.execute(text('SELECT COUNT(*) FROM printer_dispatch_claims')).scalar_one()==0
        assert c.execute(text('SELECT observed_print_job_id FROM dispatch_attempt_receipts WHERE job_id=53')).scalar_one()==70
