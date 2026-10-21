"""Actual job entry point respects owned target locks/history before device I/O."""
import pytest
from sqlalchemy import text
from core import dispatch_target_lock,dispatch_senders,dispatch_claims
from core.dispatch_identity import build_identity
from tests.test_contracts.test_dispatch_orchestration_behavior import setup
from tests.test_contracts.test_education_review_workflow import review_db


def inputs(engine):
    from core.auth import SECRET_KEY
    from modules.printers import dispatch
    creds=dispatch._get_printer_info(59)
    identity=build_identity(creds,SECRET_KEY.encode())
    return dict(printer_id=59,job_id=53,physical_key=identity.target_key,
        configuration_key=identity.configuration_key,payload_sha256='c'*64,owner_org_id=creds['org_id'])


def no_devices(monkeypatch):
    from modules.printers import smart_plug,dispatch_acquisition,dispatch
    monkeypatch.setattr(smart_plug,'prepare_bounded_dispatch_power',lambda *a,**k:pytest.fail('Blocked dispatch called power'))
    monkeypatch.setattr(dispatch_acquisition,'bounded_status_is_idle',lambda *a,**k:pytest.fail('Blocked dispatch read device'))
    monkeypatch.setattr(dispatch,'_dispatch_moonraker',lambda *a,**k:pytest.fail('Blocked dispatch uploaded/printed'))


def test_actual_dispatch_external_target_lock_refuses_before_claim_and_device(setup,monkeypatch):
    from modules.printers import dispatch
    engine,_=setup;identity=inputs(engine);owned=dispatch_target_lock.acquire_target_lock(engine,identity['physical_key'])
    assert owned.status=='acquired';no_devices(monkeypatch)
    try:
        success,reason=dispatch.dispatch_job(59,53)
        assert not success
        with engine.connect() as conn:assert conn.execute(text('SELECT count(*) FROM printer_dispatch_claims')).scalar_one()==0
    finally:owned.handle.close()


@pytest.mark.parametrize('state',['active','cleanup_uncertain'])
def test_actual_dispatch_retained_sender_without_claim_refuses_device(setup,monkeypatch,state):
    from modules.printers import dispatch
    engine,_=setup;values=inputs(engine)
    claim=dispatch_claims.acquire(engine,**values);assert claim.status=='acquired'
    registered=dict(values,claim_token=claim.token,origin_key='d'*32,runtime_incarnation='e'*32)
    receipt=dispatch_senders.register(engine,**registered);assert receipt.status=='registered'
    if state=='cleanup_uncertain':assert dispatch_senders.finish_uncertain(engine,**registered,registration_key=receipt.registration_key)
    assert dispatch_claims.release_preparing(engine,claim.token)
    no_devices(monkeypatch)
    assert not dispatch.dispatch_job(59,53)[0]
    with engine.connect() as conn:
        assert conn.execute(text('SELECT count(*) FROM printer_dispatch_claims')).scalar_one()==0
        assert conn.execute(text('SELECT state FROM dispatch_sender_registrations')).scalar_one()==state


def test_same_printer_historical_sender_old_target_still_blocks(setup,monkeypatch):
    from modules.printers import dispatch
    engine,_=setup;values=inputs(engine)
    claim=dispatch_claims.acquire(engine,**values)
    owned=dict(values,claim_token=claim.token,origin_key='d'*32,runtime_incarnation='e'*32)
    assert dispatch_senders.register(engine,**owned).status=='registered'
    assert dispatch_claims.release_preparing(engine,claim.token)
    with engine.begin() as conn:conn.execute(text("UPDATE dispatch_sender_registrations SET physical_key=:key"),{'key':'8'*64})
    no_devices(monkeypatch)
    assert not dispatch.dispatch_job(59,53)[0]


@pytest.mark.parametrize('change',['host','hold','owner'])
def test_job_authority_reloaded_after_target_acquisition(setup,monkeypatch,change):
    from modules.printers import dispatch,dispatch_orchestration
    engine,_=setup;real=dispatch_orchestration.acquire_target_lock
    def acquire(engine,key):
        result=real(engine,key);assert result.status=='acquired'
        statement={'host':"UPDATE printers SET api_host='changed.invalid' WHERE id=59",
            'hold':'UPDATE jobs SET hold=TRUE WHERE id=53',
            'owner':'UPDATE jobs SET charged_to_org_id=2 WHERE id=53'}[change]
        try:
            with engine.begin() as conn:conn.execute(text(statement))
        except Exception:result.handle.close();raise
        return result
    monkeypatch.setattr(dispatch_orchestration,'acquire_target_lock',acquire)
    no_devices(monkeypatch)
    assert not dispatch.dispatch_job(59,53)[0]
    with engine.connect() as conn:assert conn.execute(text('SELECT count(*) FROM printer_dispatch_claims')).scalar_one()==0


def test_lost_target_after_readiness_retains_claim_and_never_uploads(setup,monkeypatch):
    from modules.printers import dispatch,dispatch_orchestration,dispatch_acquisition
    engine,_=setup;real=dispatch_orchestration.acquire_target_lock;lost=[False];owned=[]
    class Handle:
        def __init__(self,inner):self.inner=inner
        def held(self):return not lost[0] and self.inner.held()
        def close(self):
            result=self.inner.close()
            return dispatch_target_lock.TargetLockClose(result.released and not lost[0])
    def acquire(engine,key):
        result=real(engine,key);assert result.status=='acquired';owned.append(result.handle)
        return dispatch_target_lock.TargetLockResult('acquired',Handle(result.handle))
    monkeypatch.setattr(dispatch_orchestration,'acquire_target_lock',acquire)
    monkeypatch.setattr(dispatch_acquisition,'bounded_status_is_idle',lambda *a:lost.__setitem__(0,True) or True)
    monkeypatch.setattr(dispatch,'_dispatch_moonraker',lambda *a:pytest.fail('Lost target uploaded'))
    assert not dispatch.dispatch_job(59,53)[0]
    with engine.connect() as conn:assert conn.execute(text('SELECT state FROM printer_dispatch_claims')).scalar_one()=='preparing'
    assert all(not handle.held() for handle in owned)


def test_unsupported_storage_reports_configuration_without_claim_or_device(setup,monkeypatch):
    from modules.printers import dispatch,dispatch_orchestration
    engine,_=setup
    monkeypatch.setattr(dispatch_orchestration,'acquire_target_lock',
        lambda *args:dispatch_target_lock.TargetLockResult('unsupported'))
    no_devices(monkeypatch)
    with engine.connect() as conn:
        previous=conn.execute(text('SELECT status FROM jobs WHERE id=53')).scalar_one()
    success,reason=dispatch.dispatch_job(59,53)
    assert not success
    assert 'storage configuration' in reason and 'this attempt sent no command' in reason
    assert 'ownership conflict' not in reason
    with engine.connect() as conn:
        assert conn.execute(text('SELECT count(*) FROM printer_dispatch_claims')).scalar_one()==0
        assert conn.execute(text('SELECT status FROM jobs WHERE id=53')).scalar_one()==previous
