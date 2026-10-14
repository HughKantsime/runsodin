"""Normal scoped HTTP producer to owned read/writer using software tray fixture."""
import time
import pytest
from types import SimpleNamespace
from cryptography.fernet import Fernet
from sqlalchemy import text
from sqlalchemy.orm import Session
from tests.test_contracts.test_api_token_delegation_behavior import token_client,review_db


@pytest.mark.parametrize('change',['none','manager','role','grant','revoked_token','revision','configuration','missing_report','commit_unconfirmed','begin_unconfirmed'])
def test_normal_teacher_read_endpoint_checks_after_network(token_client,review_db,monkeypatch,change):
    from modules.organizations import education_access,routes_education_reviews
    from modules.organizations import education_ams_read_service as service
    from modules.printers.telemetry.bambu import ams_evidence_transactions as transactions,ams_database_clock
    from modules.printers.telemetry.bambu.ams_acquisition_collector import RawAMSSnapshot
    from modules.printers.telemetry.bambu.ams_evidence import RawTrayFact
    from modules.printers.telemetry.bambu.trusted_ams_acquisition import RuntimeBoundAMSResult
    monkeypatch.setattr(education_access,'require_education_license',lambda:None)
    key=Fernet.generate_key();monkeypatch.setenv('ENCRYPTION_KEY',key.decode())
    review_db.execute(text('UPDATE printers SET api_host=:host,api_key=:credential WHERE id=9'),
        {'host':'printer.local','credential':Fernet(key).encrypt(b'SYNTHETIC|test-only-access').decode()})
    review_db.commit()
    runtime=SimpleNamespace(incarnation='a'*32,clock_domain='synthetic-domain')
    import core.runtime_identity as runtime_identity
    monkeypatch.setattr(runtime_identity,'read_runtime_identity',lambda:runtime)
    monkeypatch.setattr(transactions,'revalidate_runtime_identity',lambda value:None)
    monkeypatch.setattr(ams_database_clock,'revalidate_runtime_identity',lambda value:None)
    active=[]
    failed_ack=[]
    class OwnedSession(Session):
        def __enter__(self):active.append(self);return super().__enter__()
        def commit(self):
            ready=self.execute(text("SELECT count(*) FROM ams_read_generations WHERE state='ready'")).scalar_one()
            acquiring=self.execute(text("SELECT count(*) FROM ams_read_generations WHERE state='acquiring'")).scalar_one()
            super().commit()
            if ((change=='commit_unconfirmed' and ready) or (change=='begin_unconfirmed' and acquiring)) and not failed_ack:
                failed_ack.append(True)
                raise RuntimeError('synthetic lost commit acknowledgment')
        def __exit__(self,*args):
            try:return super().__exit__(*args)
            finally:active.remove(self)
    monkeypatch.setattr(service,'SessionLocal',lambda:OwnedSession(review_db.get_bind()))
    observed=[]
    def network(config,**kwargs):
        assert not active,'Owned DB session must close before network'
        observed.append(kwargs['operation_id'])
        with Session(review_db.get_bind()) as mutation:
            if change=='role':mutation.execute(text("UPDATE users SET role='viewer' WHERE id=3"))
            elif change=='grant':mutation.execute(text("UPDATE education_cost_center_printers SET state='revoked' WHERE printer_id=9"))
            elif change=='revoked_token':mutation.execute(text('DELETE FROM api_tokens'))
            elif change=='revision':mutation.execute(text('UPDATE education_submissions SET lifecycle_revision=2 WHERE id=14'))
            elif change=='configuration':mutation.execute(text("UPDATE printers SET api_host='changed.local' WHERE id=9"))
            mutation.commit()
        if change=='missing_report':return RuntimeBoundAMSResult(False,None,None,None,True,'unavailable')
        snapshot=RawAMSSnapshot(kwargs['operation_id'],'b'*32,1,time.monotonic(),kwargs['configuration_key'],
            kwargs['owner_org_id'],(RawTrayFact(1,0,0,'PLA','reported'),))
        return RuntimeBoundAMSResult(True,snapshot,runtime.incarnation,runtime.clock_domain,True,None)
    from modules.printers.telemetry.bambu import trusted_ams_acquisition
    monkeypatch.setattr(trusted_ams_acquisition,'read_current_selected_ams',network)
    client,issue=token_client;client.app.include_router(routes_education_reviews.router)
    headers=issue(['write:education'])
    if change=='manager':
        review_db.execute(text('UPDATE api_tokens SET user_id=2'));review_db.commit()
    response=client.post('/education/submissions/14/ams-read',headers=headers,
        json={'revision':1,'printer_id':9,'selected_slots':[1]})
    assert not active
    assert bool(observed) is (change!='begin_unconfirmed')
    if change in ('none','manager'):
        assert response.status_code==200
        assert response.json()['trays']==[{'slot_number':1,'material':'PLA','material_state':'reported','mechanically_ready':None}]
        assert not {'serial','access_code','transport','configuration_key','principal'} & response.json().keys()
    elif change in ('commit_unconfirmed','begin_unconfirmed'):
        assert response.status_code==503
        assert response.json()['error']['reason']=='ams_read_unconfirmed' if 'reason' in response.json().get('error',{}) else 'unconfirmed' in response.text
    else:assert response.status_code==409
    with Session(review_db.get_bind()) as fresh:
        row=fresh.execute(text('SELECT state FROM ams_read_generations')).scalar_one()
        assert row==('ready' if change in ('none','manager','commit_unconfirmed') else 'failed')
        assert fresh.execute(text('SELECT count(*) FROM ams_report_packets')).scalar_one()==(1 if change in ('none','manager','commit_unconfirmed') else 0)
        assert fresh.execute(text('SELECT count(*) FROM ams_current_reads')).scalar_one()==(1 if change in ('none','manager','commit_unconfirmed') else 0)


def cancellation_fixture_worker(channel,config,key,owner,slots,incarnation,deadline):
    from modules.printers.telemetry.bambu.ams_acquisition_collector import RawAMSSnapshot
    from modules.printers.telemetry.bambu.ams_evidence import RawTrayFact
    time.sleep(.2)
    channel.put({'snapshot':RawAMSSnapshot(incarnation,'c'*32,1,time.monotonic(),key,owner,
        (RawTrayFact(1,0,0,'PLA','reported'),)), 'error':None,
        'runtime_incarnation':'a'*32,'clock_domain':'synthetic-domain'})


def test_cancelled_request_waits_for_owned_child_and_never_persists(token_client,review_db,monkeypatch):
    import asyncio,threading,multiprocessing
    from starlette.requests import Request
    from core.dependencies import get_current_user
    import core.runtime_identity as runtime_identity
    from modules.organizations import education_access,education_ams_read_service as service
    from modules.organizations.education_schemas import TeacherAMSRead
    from modules.printers.telemetry.bambu import ams_evidence_transactions as transactions,trusted_ams_acquisition
    from modules.printers.telemetry.bambu.ams_acquisition_session import _run_owned_worker
    runtime=SimpleNamespace(incarnation='a'*32,clock_domain='synthetic-domain')
    monkeypatch.setattr(education_access,'require_education_license',lambda:None)
    monkeypatch.setattr(runtime_identity,'read_runtime_identity',lambda:runtime)
    monkeypatch.setattr(transactions,'revalidate_runtime_identity',lambda value:None)
    monkeypatch.setattr(service,'SessionLocal',lambda:Session(review_db.get_bind()))
    key=Fernet.generate_key();monkeypatch.setenv('ENCRYPTION_KEY',key.decode())
    review_db.execute(text('UPDATE printers SET api_host=:host,api_key=:credential WHERE id=9'),
        {'host':'printer.local','credential':Fernet(key).encrypt(b'SYNTHETIC|test-only-access').decode()})
    review_db.commit()
    _,issue=token_client;headers=issue(['write:education'])
    request=Request({'type':'http','method':'POST','path':'/synthetic',
        'headers':[(k.lower().encode(),v.encode()) for k,v in headers.items()]})
    entered=threading.Event();finished=threading.Event();results=[];persist_calls=[]
    def forbidden_persist(*args,**kwargs):
        persist_calls.append(True)
        raise AssertionError('Cancelled result must not enter persistence')
    monkeypatch.setattr(service,'persist_ams_report',forbidden_persist)
    def network(config,**kwargs):
        entered.set()
        result=_run_owned_worker(config,kwargs['configuration_key'],kwargs['owner_org_id'],[1],5,
            worker=cancellation_fixture_worker,required_runtime=runtime,acquisition_id=kwargs['operation_id'])
        results.append(result);finished.set()
        return result  # Cancelled orchestration must discard even a success.
    monkeypatch.setattr(trusted_ams_acquisition,'read_current_selected_ams',network)
    before={p.pid for p in multiprocessing.active_children()}
    async def run():
        with Session(review_db.get_bind()) as authdb:
            principal=await get_current_user(request=request,token=None,db=authdb)
        task=asyncio.create_task(service.acquire_teacher_ams_report(request,principal=principal,submission_id=14,
            body=TeacherAMSRead(revision=1,printer_id=9,selected_slots=[1])))
        assert await asyncio.to_thread(entered.wait,5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):await asyncio.wait_for(task,8)
    asyncio.run(run())
    assert finished.is_set() and results[0].success and results[0].cleanup_confirmed
    assert not persist_calls
    assert {p.pid for p in multiprocessing.active_children()}<=before
    with Session(review_db.get_bind()) as db:
        assert db.execute(text('SELECT state FROM ams_read_generations')).scalar_one()=='failed'
        assert db.execute(text('SELECT count(*) FROM ams_current_reads')).scalar_one()==0
        assert db.execute(text('SELECT count(*) FROM ams_report_packets')).scalar_one()==0
