"""Two-connection SQLite admission/recovery ordering; synthetic dispatch only."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from sqlalchemy import text
from sqlalchemy.orm import Session
from core.dispatch_recovery import capture, mutate
from modules.printers.routes_dispatch_recovery import HoldRequest
from modules.printers.dispatch_admission import admit
from tests.test_contracts.test_dispatch_final_admission_behavior import setup, review_db

ACTOR=dict(id=3,username='admin',role='admin',group_id=1,_auth_kind='bearer_jwt')


def hold(engine, before_capture=None):
    with Session(engine) as db:
        evidence=capture(db,59,ACTOR)
        body=HoldRequest(attempt_handle=evidence['attempt'],snapshot_handle=evidence['snapshot'],reason_category='investigation')
        db.rollback()
        if before_capture: before_capture.set()
        return mutate(db,59,ACTOR,body,'hold',lambda conn,pid=59:None)


def test_hold_commits_before_admission_and_admission_refuses(setup):
    engine,provider,inputs=setup
    assert hold(engine)['held']
    result=admit(engine,**inputs)
    assert not result.admitted
    with engine.connect() as conn:
        assert conn.execute(text('SELECT hold FROM jobs WHERE id=53')).scalar_one()==1
        assert conn.execute(text('SELECT state FROM printer_dispatch_claims')).scalar_one()=='preparing'


def test_admission_commits_first_then_hold_records_limitation(setup,monkeypatch):
    from modules.printers import dispatch_admission
    engine,provider,inputs=setup
    locked=Event();finish=Event();captured=Event()
    original=dispatch_admission._lock
    def pause(conn,table,identifier,column):
        result=original(conn,table,identifier,column)
        if table=='jobs':
            locked.set()
            assert finish.wait(10)
        return result
    monkeypatch.setattr(dispatch_admission,'_lock',pause)
    with ThreadPoolExecutor(max_workers=2) as pool:
        admitted=pool.submit(admit,engine,**inputs)
        assert locked.wait(10)
        held=pool.submit(hold,engine,captured)
        assert captured.wait(10)
        finish.set()
        assert admitted.result(timeout=10).admitted
        # A claim state changed since the captured UI snapshot: safe refusal,
        # followed by fresh inspection/hold, never cancellation or POST retry.
        try:
            result=held.result(timeout=10)
        except Exception as error:
            assert getattr(error,'detail',{}).get('code')=='snapshot_changed'
            result=hold(engine)
        assert result['held'] and 'cannot stop an admitted command' in result['guidance']
    with engine.connect() as conn:
        assert conn.execute(text('SELECT state FROM printer_dispatch_claims')).scalar_one()=='adapter_entered'
        assert conn.execute(text('SELECT hold FROM jobs WHERE id=53')).scalar_one()==1
