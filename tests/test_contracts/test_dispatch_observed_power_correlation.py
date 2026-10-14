"""Observed start for a prepared dispatch must not issue a second ON command."""
from types import SimpleNamespace
from sqlalchemy import text
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.dispatch_test_support import configure_dispatch

def test_actual_prepared_dispatch_start_event_does_not_repeat_power_on(review_db,tmp_path,monkeypatch):
    from modules.printers import dispatch,smart_plug,plug_power_acquisition
    engine,_=configure_dispatch(review_db,tmp_path,monkeypatch)
    calls=[];names=[]
    with engine.begin() as conn:
        conn.execute(text("UPDATE printers SET plug_type='tasmota',plug_host='127.0.0.1',plug_auto_on=TRUE WHERE id=59"))
    monkeypatch.setattr(plug_power_acquisition,'bounded_power_on',lambda *args:calls.append('prepared_on') or plug_power_acquisition.PowerAttempt('accepted',True))
    def adapter(job_id,path,filename,creds):
        names.append(filename)
        return True,'Synthetic accepted'
    monkeypatch.setattr(dispatch,'_dispatch_moonraker',adapter)
    monkeypatch.setattr(smart_plug,'power_on',lambda *args:calls.append('observed_on') or True)
    assert dispatch.dispatch_job(59,53)[0]
    with engine.begin() as conn:
        observed=conn.execute(text("INSERT INTO print_jobs(printer_id,filename,job_name,scheduled_job_id,status,started_at,observed_received_at) VALUES(59,:name,:name,53,'running',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP) RETURNING id"),{'name':names[0]}).scalar_one()
    smart_plug._on_job_started_event(SimpleNamespace(data={'printer_id':59,'print_job_id':observed,'job_name':names[0]}))
    assert calls==['prepared_on']

import pytest

@pytest.mark.parametrize('state',['preparing','adapter_entered','accepted','uncertain'])
def test_retained_attempt_refuses_repeated_unidentified_observation(review_db,tmp_path,monkeypatch,state):
    from modules.printers import dispatch,smart_plug
    engine,_=configure_dispatch(review_db,tmp_path,monkeypatch)
    monkeypatch.setattr(dispatch,'_dispatch_moonraker',lambda *args:(True,'Synthetic accepted'))
    assert dispatch.dispatch_job(59,53)[0]
    calls=[]
    monkeypatch.setattr(smart_plug,'power_on',lambda *args:calls.append('on'))
    with engine.begin() as conn:
        conn.execute(text("UPDATE printer_dispatch_claims SET state=:state WHERE printer_id=59"),{'state':state})
        conn.execute(text("UPDATE printers SET plug_type='tasmota',plug_host='127.0.0.1',plug_auto_on=TRUE WHERE id=59"))
    for _ in range(2):
        smart_plug._on_job_started_event(SimpleNamespace(data={'printer_id':59}))
    assert calls==[]
    with engine.connect() as conn:
        assert conn.execute(text('SELECT state FROM printer_dispatch_claims WHERE printer_id=59')).scalar_one()==state


def test_unavailable_claim_database_event_refuses_power_and_hides_error(monkeypatch,caplog):
    from modules.printers import smart_plug
    calls=[]
    def unavailable():raise RuntimeError('private synthetic connection marker')
    monkeypatch.setattr(smart_plug,'engine',SimpleNamespace(connect=unavailable))
    monkeypatch.setattr(smart_plug,'get_plug_config',lambda pid:{'auto_on':True})
    monkeypatch.setattr(smart_plug,'power_on',lambda *args:calls.append('on'))
    smart_plug._on_job_started_event(SimpleNamespace(data={'printer_id':59}))
    assert calls==[]
    assert 'private synthetic connection marker' not in caplog.text


@pytest.mark.parametrize('protocol',['moonraker','prusalink','bambu'])
def test_generic_transport_filename_binds_exact_durable_attempt(review_db,tmp_path,monkeypatch,protocol):
    from modules.printers import dispatch
    engine,_=configure_dispatch(review_db,tmp_path,monkeypatch,protocol)
    names=[]
    def adapter(job_id,path,filename,creds,**options):
        names.append(filename)
        return True,'Synthetic accepted'
    monkeypatch.setattr(dispatch,'_dispatch_'+protocol,adapter)
    assert dispatch.dispatch_job(59,53)[0]
    with engine.connect() as conn:
        token=conn.execute(text('SELECT token FROM printer_dispatch_claims WHERE printer_id=59 AND job_id=53')).scalar_one()
    suffix='.3mf' if protocol=='bambu' else '.gcode'
    assert names==[f'odin-dispatch-{token}{suffix}']


def test_attempt_filename_uses_actual_payload_suffix_not_original_name(review_db,tmp_path,monkeypatch):
    from modules.printers import dispatch
    engine,_=configure_dispatch(review_db,tmp_path,monkeypatch)
    with engine.begin() as conn:
        conn.execute(text("UPDATE print_files SET original_filename='misleading.3mf' WHERE id=52"))
    names=[]
    monkeypatch.setattr(dispatch,'_dispatch_moonraker',lambda jid,path,name,creds:(names.append(name) or True,'Synthetic accepted'))
    assert dispatch.dispatch_job(59,53)[0]
    assert len(names)==1 and names[0].endswith('.gcode')


def test_observed_prepared_attempt_binds_once_in_durable_receipt(review_db,tmp_path,monkeypatch):
    from modules.printers import dispatch,smart_plug,plug_power_acquisition
    engine,_=configure_dispatch(review_db,tmp_path,monkeypatch)
    with engine.begin() as conn:
        conn.execute(text("UPDATE printers SET plug_type='tasmota',plug_host='127.0.0.1',plug_auto_on=TRUE WHERE id=59"))
    monkeypatch.setattr(plug_power_acquisition,'bounded_power_on',lambda *args:plug_power_acquisition.PowerAttempt('accepted',True))
    names=[];calls=[]
    monkeypatch.setattr(dispatch,'_dispatch_moonraker',lambda jid,path,name,creds:(names.append(name) or True,'Synthetic accepted'))
    monkeypatch.setattr(smart_plug,'power_on',lambda *args:calls.append('on'))
    assert dispatch.dispatch_job(59,53)[0]
    with engine.begin() as conn:
        observed=conn.execute(text("INSERT INTO print_jobs(printer_id,filename,job_name,scheduled_job_id,status,started_at,observed_received_at) VALUES(59,:name,:name,53,'running',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP) RETURNING id"),{'name':names[0]}).scalar_one()
    event=SimpleNamespace(data={'printer_id':59,'print_job_id':observed,'job_name':names[0]})
    smart_plug._on_job_started_event(event)
    smart_plug._on_job_started_event(event)
    assert calls==[]
    engine.dispose()
    with engine.connect() as conn:
        assert conn.execute(text('SELECT observed_print_job_id FROM dispatch_attempt_receipts WHERE job_id=53')).scalar_one()==observed
