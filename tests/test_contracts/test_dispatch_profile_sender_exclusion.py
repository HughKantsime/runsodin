"""Actual HTTP profile macros must not bypass an unresolved dispatch attempt."""
import json
import pytest
from sqlalchemy import text
from tests.test_contracts.test_profile_library_behavior import profile_client,token_client,review_db

@pytest.mark.parametrize('state',['preparing','adapter_entered','accepted','uncertain'])
def test_print_capable_profile_commands_respect_retained_dispatch(profile_client,review_db,state):
    client,issue,calls,_=profile_client
    review_db.execute(text("UPDATE printer_profiles SET category='custom',raw_content=:content WHERE id=101"),{'content':json.dumps({'gcode':'M24'})})
    review_db.execute(text("INSERT INTO printer_dispatch_claims(printer_id,job_id,physical_key,configuration_key,payload_sha256,token,owner_org_id,state) VALUES(9,953,:physical,:config,:payload,:token,1,:state)"),{'physical':'a'*64,'config':'b'*64,'payload':'c'*64,'token':'d'*32,'state':state})
    review_db.commit()
    response=client.post('/profiles/101/apply',headers=issue(['read:models','write:printers']),json={'printer_id':9})
    assert response.status_code==409
    assert calls==[]
    assert review_db.execute(text('SELECT state FROM printer_dispatch_claims WHERE printer_id=9')).scalar_one()==state
    assert review_db.execute(text('SELECT last_applied_at FROM printer_profiles WHERE id=101')).scalar_one() is None


@pytest.mark.parametrize('state',['active','cleanup_uncertain'])
def test_profile_retained_sender_without_claim_is_blocked(profile_client,review_db,state):
    client,issue,calls,_=profile_client
    values={'state':state,'finished':'2026-10-11' if state=='cleanup_uncertain' else None}
    review_db.execute(text("INSERT INTO dispatch_sender_registrations(claim_token,printer_id,job_id,physical_key,configuration_key,payload_sha256,owner_org_id,origin_key,runtime_incarnation,registration_key,state,finished_at) VALUES(:token,9,953,:physical,:config,:payload,1,:origin,:incarnation,:registration,:state,:finished)"),dict(values,token='d'*32,physical='a'*64,config='b'*64,payload='c'*64,origin='e'*32,incarnation='f'*32,registration='1'*32))
    review_db.commit()
    response=client.post('/profiles/101/apply',headers=issue(['read:models','write:printers']),json={'printer_id':9})
    assert response.status_code==409 and calls==[]
    assert review_db.execute(text('SELECT last_applied_at FROM printer_profiles WHERE id=101')).scalar_one() is None


def _shared_dispatch_target(profile_client,review_db,tmp_path,monkeypatch):
    from tests.test_contracts.dispatch_test_support import configure_dispatch
    engine,path=configure_dispatch(review_db,tmp_path,monkeypatch)
    with engine.begin() as conn:conn.execute(text("UPDATE printers SET api_host='synthetic-own.invalid' WHERE id=59"))
    return engine,path


def test_live_profile_macro_excludes_job_on_alternate_printer_id(profile_client,review_db,tmp_path,monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from modules.printers import dispatch,smart_plug
    from modules.printers.adapters import moonraker
    client,issue,calls,_=profile_client;engine,_=_shared_dispatch_target(profile_client,review_db,tmp_path,monkeypatch)
    entered=threading.Event();release=threading.Event();macro_calls=[]
    class Adapter:
        def __init__(self,*args,**kwargs):pass
        def send_gcode(self,command):
            macro_calls.append(command);entered.set();assert release.wait(5);return True
    monkeypatch.setattr(moonraker,'MoonrakerPrinter',Adapter)
    monkeypatch.setattr(smart_plug,'prepare_bounded_dispatch_power',lambda *a,**k:pytest.fail('Job bypassed active macro target'))
    headers=issue(['read:models','write:printers'])
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(client.post,'/profiles/101/apply',headers=headers,json={'printer_id':9})
        try:
            assert entered.wait(3)
            assert not dispatch.dispatch_job(59,53)[0]
            assert len(macro_calls)==1
        finally:release.set()
        assert future.result(5).status_code==200


def test_live_job_excludes_profile_on_alternate_printer_id(profile_client,review_db,tmp_path,monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from modules.printers import dispatch
    client,issue,calls,_=profile_client;engine,_=_shared_dispatch_target(profile_client,review_db,tmp_path,monkeypatch)
    entered=threading.Event();release=threading.Event();job_calls=[]
    def transport(*args,**kwargs):
        job_calls.append(True);entered.set();assert release.wait(5);return False,'Owned simulated refusal'
    monkeypatch.setattr(dispatch,'_dispatch_moonraker',transport)
    headers=issue(['read:models','write:printers'])
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(dispatch.dispatch_job,59,53)
        try:
            assert entered.wait(3)
            response=client.post('/profiles/101/apply',headers=headers,json={'printer_id':9})
            assert response.status_code==409 and calls==[] and job_calls==[True]
        finally:release.set()
        assert not future.result(5)[0]


def test_profile_command_has_no_open_request_transaction(profile_client,monkeypatch):
    from modules.system import profile_routes
    from modules.printers.adapters import moonraker
    client,issue,_,_=profile_client;current=[];checks=[]
    original=profile_routes._apply_profile_commands
    def capture(db,*args,**kwargs):current.append(db);return original(db,*args,**kwargs)
    class Adapter:
        def __init__(self,*args,**kwargs):assert not current[-1].in_transaction()
        def send_gcode(self,gcode):checks.append(current[-1].in_transaction());return True
    monkeypatch.setattr(profile_routes,'_apply_profile_commands',capture)
    monkeypatch.setattr(moonraker,'MoonrakerPrinter',Adapter)
    response=client.post('/profiles/101/apply',headers=issue(['read:models','write:printers']),json={'printer_id':9})
    assert response.status_code==200 and checks==[False,False]


@pytest.mark.parametrize('change',['host','printer_owner','profile_owner','profile_content'])
def test_profile_revalidates_after_lock_acquisition(profile_client,review_db,monkeypatch,change):
    from core import dispatch_target_lock
    client,issue,calls,_=profile_client;real=dispatch_target_lock.acquire_target_lock
    def acquire(engine,key):
        result=real(engine,key)
        assert result.status=='acquired'
        statement={'host':"UPDATE printers SET api_host='changed.invalid' WHERE id=9",
            'printer_owner':'UPDATE printers SET org_id=2 WHERE id=9',
            'profile_owner':'UPDATE printer_profiles SET org_id=2 WHERE id=101',
            'profile_content':"UPDATE printer_profiles SET raw_content='{}' WHERE id=101"}[change]
        try:
            with engine.begin() as conn:
                if change=='printer_owner':conn.execute(text('DELETE FROM education_cost_center_printers WHERE printer_id=9'))
                conn.execute(text(statement))
        except Exception:
            result.handle.close();raise
        return result
    monkeypatch.setattr(dispatch_target_lock,'acquire_target_lock',acquire)
    response=client.post('/profiles/101/apply',headers=issue(['read:models','write:printers']),json={'printer_id':9})
    assert response.status_code in (404,409) and calls==[]
    assert review_db.execute(text('SELECT last_applied_at FROM printer_profiles WHERE id=101')).scalar_one() is None


def test_profile_uncertain_close_rolls_back_success_anchor(profile_client,review_db,monkeypatch):
    from core import dispatch_target_lock
    client,issue,calls,_=profile_client;real=dispatch_target_lock.acquire_target_lock
    class Handle:
        def __init__(self,owned):self.owned=owned
        def held(self):return self.owned.held()
        def close(self):self.owned.close();return dispatch_target_lock.TargetLockClose(False)
    def acquire(engine,key):
        result=real(engine,key);assert result.status=='acquired'
        return dispatch_target_lock.TargetLockResult('acquired',Handle(result.handle))
    monkeypatch.setattr(dispatch_target_lock,'acquire_target_lock',acquire)
    response=client.post('/profiles/101/apply',headers=issue(['read:models','write:printers']),json={'printer_id':9})
    assert response.status_code==409 and len(calls)==3
    assert review_db.execute(text('SELECT last_applied_at FROM printer_profiles WHERE id=101')).scalar_one() is None


def test_other_printer_claim_for_exact_target_blocks_profile(profile_client,review_db):
    from core.auth import SECRET_KEY
    from core.dispatch_identity import build_identity
    from modules.printers.dispatch import _get_printer_info
    client,issue,calls,_=profile_client
    identity=build_identity(_get_printer_info(9,connection=review_db),SECRET_KEY.encode())
    review_db.execute(text("INSERT INTO printer_dispatch_claims(printer_id,job_id,physical_key,configuration_key,payload_sha256,token,owner_org_id,state) VALUES(999,953,:physical,:config,:payload,:token,2,'uncertain')"),dict(physical=identity.target_key,config='b'*64,payload='c'*64,token='d'*32));review_db.commit()
    response=client.post('/profiles/101/apply',headers=issue(['read:models','write:printers']),json={'printer_id':9})
    assert response.status_code==409 and calls==[]


@pytest.mark.parametrize('change',['profile_owner','profile_content','profile_category','printer_owner','printer_host'])
def test_committed_change_during_command_cannot_stamp_new_authority(profile_client,review_db,monkeypatch,change):
    from modules.printers.adapters import moonraker
    client,issue,_,_=profile_client;engine=review_db.get_bind();calls=[]
    statement={'profile_owner':'UPDATE printer_profiles SET org_id=2 WHERE id=101',
        'profile_content':"UPDATE printer_profiles SET raw_content='{}' WHERE id=101",
        'profile_category':"UPDATE printer_profiles SET category='custom' WHERE id=101",
        'printer_owner':'UPDATE printers SET org_id=2 WHERE id=9',
        'printer_host':"UPDATE printers SET api_host='changed.invalid' WHERE id=9"}[change]
    class Adapter:
        def __init__(self,*args,**kwargs):pass
        def send_gcode(self,gcode):
            calls.append(gcode)
            if len(calls)==1:
                with engine.begin() as conn:
                    if change=='printer_owner':conn.execute(text('DELETE FROM education_cost_center_printers WHERE printer_id=9'))
                    conn.execute(text(statement))
            return True
    monkeypatch.setattr(moonraker,'MoonrakerPrinter',Adapter)
    response=client.post('/profiles/101/apply',headers=issue(['read:models','write:printers']),json={'printer_id':9})
    assert response.status_code==409
    assert len(calls)==1
    assert review_db.execute(text('SELECT last_applied_at FROM printer_profiles WHERE id=101')).scalar_one() is None
