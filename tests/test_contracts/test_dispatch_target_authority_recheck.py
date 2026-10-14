"""Live job/policy authority must be reread after configured-target acquisition."""
import pytest
from sqlalchemy import text
from tests.test_contracts.test_dispatch_orchestration_behavior import setup
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_dispatch_target_entrypoints import no_devices


def test_completed_job_during_lock_acquisition_cannot_prepare_power(setup,monkeypatch):
    from modules.printers import dispatch,dispatch_orchestration
    engine,_=setup;real=dispatch_orchestration.acquire_target_lock
    def acquire(engine,key):
        owned=real(engine,key)
        with engine.begin() as conn:conn.execute(text("UPDATE jobs SET status='completed' WHERE id=53"))
        return owned
    monkeypatch.setattr(dispatch_orchestration,'acquire_target_lock',acquire)
    no_devices(monkeypatch)
    assert not dispatch.dispatch_job(59,53)[0]
    with engine.connect() as conn:
        assert conn.execute(text('SELECT count(*) FROM printer_dispatch_claims')).scalar_one()==0
        assert conn.execute(text('SELECT status FROM jobs WHERE id=53')).scalar_one()=='completed'


def test_policy_must_be_authorized_again_after_target_lock(setup,monkeypatch):
    from modules.printers import dispatch
    engine,_=setup;checks=[]
    def policy(*args):
        checks.append(True)
        return None if len(checks)==1 else (False,'Owned revoked policy')
    monkeypatch.setattr(dispatch,'_authorize_education_hardware_action',policy)
    no_devices(monkeypatch)
    assert not dispatch.dispatch_job(59,53)[0]
    assert len(checks)==2
    with engine.connect() as conn:assert conn.execute(text('SELECT count(*) FROM printer_dispatch_claims')).scalar_one()==0
