"""Actual dispatch entry with durable DB claims and substituted device boundaries."""
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import pytest
from sqlalchemy import text
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.dispatch_test_support import configure_dispatch


@pytest.fixture
def setup(review_db,tmp_path,monkeypatch):
    engine,path=configure_dispatch(review_db,tmp_path,monkeypatch)
    return engine,path


def claim_state(engine):
    with engine.connect() as conn:return conn.execute(text('SELECT state FROM printer_dispatch_claims')).scalar()


@pytest.mark.parametrize('protocol',['moonraker','prusalink','bambu'])
def test_actual_dispatch_uses_owned_snapshot_and_committed_adapter_claim(review_db,tmp_path,monkeypatch,protocol):
    from modules.printers import dispatch
    engine,path=configure_dispatch(review_db,tmp_path,monkeypatch,protocol)
    original=path.read_bytes();seen=[]
    def adapter(jid,staged,remote,creds,**options):
        assert claim_state(engine)=='adapter_entered'
        assert Path(staged)!=path and Path(staged).read_bytes()==original
        assert Path(staged).suffix==path.suffix
        path.write_bytes(b'changed after admission')
        assert Path(staged).read_bytes()==original
        if protocol=='bambu':assert options['plate_num']==1
        seen.append(Path(staged));return True,'Synthetic accepted command'
    monkeypatch.setattr(dispatch,'_dispatch_'+protocol,adapter)
    assert dispatch.dispatch_job(59,53)==(True,'Print started')
    assert claim_state(engine)=='accepted'
    assert len(seen)==1 and not seen[0].exists()
    with engine.connect() as conn:assert conn.execute(text('SELECT status FROM jobs WHERE id=53')).scalar_one()=='printing'


@pytest.mark.parametrize('same_job',[True,False])
def test_concurrent_actual_dispatch_never_enters_second_adapter(setup,monkeypatch,same_job):
    from modules.printers import dispatch
    engine,path=setup;entered=threading.Event();release=threading.Event();calls=[]
    def adapter(*args):
        calls.append(args[0]);entered.set();assert release.wait(5)
        return False,'Synthetic upload refusal'
    monkeypatch.setattr(dispatch,'_dispatch_moonraker',adapter)
    with ThreadPoolExecutor(max_workers=1) as executor:
        first=executor.submit(dispatch.dispatch_job,59,53)
        try:
            assert entered.wait(2)
            second=dispatch.dispatch_job(59,53 if same_job else 54)
            assert not second[0] and 'claim' in second[1].lower()
            assert calls==[53]
        finally:
            release.set();assert not first.result(3)[0]
    assert claim_state(engine)=='uncertain'


@pytest.mark.parametrize('power_result',[True,False,None])
def test_actual_power_precedes_readiness_and_upload_and_has_durable_claim(setup,monkeypatch,power_result):
    from modules.printers import dispatch,dispatch_acquisition,plug_power_acquisition
    engine,path=setup;calls=[];deadlines=[]
    with engine.begin() as conn:conn.execute(text("UPDATE printers SET plug_type='tasmota',plug_host='127.0.0.1',plug_auto_on=TRUE WHERE id=59"))
    def power(config,deadline,connection):
        assert claim_state(engine)=='preparing'
        calls.append('power');deadlines.append(deadline)
        return plug_power_acquisition.PowerAttempt('accepted' if power_result is True else 'uncertain',power_result)
    def ready(creds,deadline):
        assert claim_state(engine)=='preparing'
        calls.append('ready');deadlines.append(deadline);return True
    monkeypatch.setattr(plug_power_acquisition,'bounded_power_on',power)
    monkeypatch.setattr(dispatch_acquisition,'bounded_status_is_idle',ready)
    monkeypatch.setattr(dispatch,'_dispatch_moonraker',lambda *args:calls.append('adapter') or (False,'Synthetic upload refusal'))
    success,message=dispatch.dispatch_job(59,53)
    assert not success
    if power_result is True:
        assert calls==['power','ready','adapter'] and deadlines[0]==deadlines[1]
        assert claim_state(engine)=='uncertain'
    else:
        assert calls==['power'] and claim_state(engine) is None


@pytest.mark.parametrize('change',['source','job_hold','job_owner','printer_host','slot_number'])
def test_intervening_preparation_change_prevents_actual_upload_and_releases_preparing(setup,monkeypatch,change):
    from modules.printers import dispatch,dispatch_acquisition
    engine,path=setup;calls=[]
    def ready(*args):
        if change=='source':path.write_bytes(b'changed')
        else:
            sql={'job_hold':'UPDATE jobs SET hold=TRUE WHERE id=53',
                 'job_owner':'UPDATE jobs SET charged_to_org_id=2 WHERE id=53',
                 'printer_host':"UPDATE printers SET api_host='changed.example.test' WHERE id=59",
                 'slot_number':'UPDATE filament_slots SET slot_number=2 WHERE printer_id=59'}[change]
            with engine.begin() as conn:conn.execute(text(sql))
        return True
    monkeypatch.setattr(dispatch_acquisition,'bounded_status_is_idle',ready)
    monkeypatch.setattr(dispatch,'_dispatch_moonraker',lambda *args:calls.append(True) or (True,'Synthetic accepted'))
    assert not dispatch.dispatch_job(59,53)[0]
    assert calls==[] and claim_state(engine) is None


@pytest.mark.parametrize('change',['private_printer','private_file','hold','inactive'])
def test_initial_unauthorized_or_unready_facts_trigger_zero_device_operations(setup,monkeypatch,change):
    from modules.printers import dispatch,smart_plug,dispatch_acquisition
    engine,path=setup
    sql={'private_printer':'UPDATE printers SET org_id=2 WHERE id=59',
         'private_file':'UPDATE print_files SET org_id=2 WHERE id=52',
         'hold':'UPDATE jobs SET hold=TRUE WHERE id=53',
         'inactive':'UPDATE printers SET is_active=FALSE WHERE id=59'}[change]
    with engine.begin() as conn:conn.execute(text(sql))
    monkeypatch.setattr(smart_plug,'prepare_bounded_dispatch_power',lambda *args:pytest.fail('Denied dispatch prepared power'))
    monkeypatch.setattr(dispatch_acquisition,'bounded_status_is_idle',lambda *args:pytest.fail('Denied dispatch read device'))
    monkeypatch.setattr(dispatch,'_dispatch_moonraker',lambda *args:pytest.fail('Denied dispatch uploaded'))
    assert not dispatch.dispatch_job(59,53)[0]
    assert claim_state(engine) is None


@pytest.mark.parametrize('outcome',['rejected','unknown','accepted'])
def test_postentry_result_always_retains_fence_against_repeat_dispatch(setup,monkeypatch,outcome):
    from modules.printers import dispatch
    engine,path=setup;calls=[]
    def adapter(*args):
        calls.append(True)
        if outcome=='unknown':raise RuntimeError('Synthetic exception')
        return outcome=='accepted','Synthetic result'
    monkeypatch.setattr(dispatch,'_dispatch_moonraker',adapter)
    first=dispatch.dispatch_job(59,53)
    assert first[0] is (outcome=='accepted')
    assert claim_state(engine)==('accepted' if outcome=='accepted' else 'uncertain')
    assert not dispatch.dispatch_job(59,54)[0]
    assert calls==[True]


@pytest.mark.parametrize('state',['preparing','adapter_entered','accepted','uncertain'])
def test_unresolved_universal_dispatch_claim_blocks_automatic_power_off(setup,state):
    from datetime import datetime,timezone
    from core import dispatch_claims
    from modules.printers import smart_plug
    engine,path=setup
    with engine.begin() as conn:conn.execute(text('UPDATE printers SET last_seen=:stamp WHERE id=59'),{'stamp':datetime.now(timezone.utc)})
    claim=dispatch_claims.acquire(engine,59,53,'a'*64,'b'*64,'c'*64,1)
    assert claim.status=='acquired'
    if state!='preparing':assert dispatch_claims.enter_adapter(engine,claim.token)
    if state in ('accepted','uncertain'):assert dispatch_claims.record_outcome(engine,claim.token,state)
    assert smart_plug._auto_off_safe(59) is False


@pytest.mark.parametrize('primary_alias',[False,True])
def test_operation_owned_source_or_primary_alias_triggers_zero_device_operations(setup,monkeypatch,primary_alias):
    import hashlib
    from modules.printers import dispatch,dispatch_acquisition,plug_power_acquisition
    engine,path=setup;calls=[]
    with engine.begin() as conn:
        conn.execute(text("UPDATE printers SET plug_type='tasmota',plug_host='127.0.0.1',plug_auto_on=TRUE WHERE id=59"))
        if primary_alias:
            conn.execute(text("INSERT INTO print_files(id,filename,original_filename,stored_path,org_id) VALUES(55,'primary.gcode','primary.gcode','/owned/primary.gcode',1)"))
            conn.execute(text('UPDATE models SET print_file_id=55 WHERE id=51'))
            conn.execute(text('UPDATE jobs SET print_file_id=52,payload_sha256=:hash,plate_index=0 WHERE id=53'),{'hash':hashlib.sha256(path.read_bytes()).hexdigest()})
            owned='/owned/primary.gcode'
        else:owned=str(path)
        conn.execute(text("UPDATE education_upload_operations SET final_path=:path WHERE operation_id='upload-1'"),{'path':owned})
    monkeypatch.setattr(plug_power_acquisition,'bounded_power_on',lambda *args:calls.append('power') or plug_power_acquisition.PowerAttempt('accepted',True))
    monkeypatch.setattr(dispatch_acquisition,'bounded_status_is_idle',lambda *args:calls.append('ready') or True)
    monkeypatch.setattr(dispatch,'_dispatch_moonraker',lambda *args:calls.append('adapter') or (True,'Synthetic accepted'))
    assert not dispatch.dispatch_job(59,53)[0]
    assert calls==[]
    assert claim_state(engine) is None
