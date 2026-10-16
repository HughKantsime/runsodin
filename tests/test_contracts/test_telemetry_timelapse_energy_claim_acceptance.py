"""Bounded owned telemetry/capture scheduling, energy and reporting assertions."""
from types import SimpleNamespace
import pytest
from sqlalchemy import text
from sqlalchemy.orm import sessionmaker
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_product_bom_integrity import bom_client
from tests.test_contracts.test_analytics_financial_behavior import analytics_client
from tests.test_contracts.test_license_user_limits import signed_license
from tests.test_contracts.test_job_approval_notification_behavior import approval_client

class StopOwnedLoop(BaseException): pass

def test_actual_timelapse_loop_filters_printers_and_sleeps_thirty_seconds(review_db,monkeypatch,tmp_path):
    from modules.archives import timelapse_capture as capture
    review_db.execute(text("UPDATE printers SET timelapse_enabled=1,is_active=1,gcode_state='RUNNING' WHERE id=9"))
    review_db.execute(text("UPDATE printers SET timelapse_enabled=0,is_active=1,gcode_state='RUNNING' WHERE id=10"))
    review_db.execute(text("INSERT INTO jobs(id,item_name,printer_id,status) VALUES(993,'Owned frame loop',9,'printing')"));review_db.commit()
    factory=sessionmaker(bind=review_db.get_bind());calls=[];sleeps=[]
    monkeypatch.setattr(capture,'SessionLocal',factory);monkeypatch.setattr(capture,'TIMELAPSE_DIR',tmp_path/'media')
    monkeypatch.setattr(capture,'get_or_create_timelapse',lambda db,p,j,**kw:capture.CaptureTicket(993,'a'*32,j))
    monkeypatch.setattr(capture,'capture_frame',lambda p,t,**kw:calls.append((p,t,kw)))
    monkeypatch.setattr(capture,'finalize_stale_timelapses',lambda:None)
    def sleep(seconds):
        sleeps.append(seconds)
        raise StopOwnedLoop()
    monkeypatch.setattr(capture.time,'sleep',sleep)
    with pytest.raises(StopOwnedLoop):capture.main_loop()
    assert sleeps==[30]
    assert calls==[(9,993,{'nonce':'a'*32,'job_id':993})]


@pytest.fixture
def metered(review_db,monkeypatch):
    from modules.printers import smart_plug
    monkeypatch.setattr(smart_plug,'engine',review_db.get_bind())
    monkeypatch.setattr(smart_plug,'_energy_starts',{})
    monkeypatch.setattr(smart_plug,'_energy_ended',{})
    reading={'total':12.0}
    monkeypatch.setattr(smart_plug,'_http_request',lambda url:(200,'{"StatusSNS":{"ENERGY":{"Total":%s,"Power":40,"Voltage":230,"Current":0.2}}}'%reading['total']))
    monkeypatch.setattr(smart_plug,'get_energy',lambda printer:smart_plug.tasmota_energy('owned.invalid'))
    review_db.execute(text("UPDATE printers SET plug_energy_kwh=0 WHERE id=9"))
    review_db.execute(text("INSERT OR REPLACE INTO system_config(key,value) VALUES('energy_cost_per_kwh','0.2')"))
    review_db.execute(text("INSERT INTO print_jobs(id,printer_id,job_id,filename,status,started_at) VALUES(993,9,'owned-993','owned.3mf','running','2026-10-14 10:00:00')"))
    review_db.commit()
    return smart_plug,reading


def _archive(db,status='completed'):
    db.execute(text("INSERT INTO print_archives(print_job_id,printer_id,print_name,status) VALUES(993,9,'Owned energy',:s)"),{'s':status});db.commit()


def _energy(db):
    db.expire_all()
    return tuple(db.execute(text('SELECT energy_kwh,energy_cost FROM print_archives WHERE print_job_id=993')).one()), db.execute(text('SELECT plug_energy_kwh FROM printers WHERE id=9')).scalar_one()


@pytest.mark.parametrize('status',['completed','failed'])
def test_tasmota_counter_delta_and_cost_persist_on_the_print_archive(metered,review_db,status):
    smart_plug,reading=metered
    assert smart_plug.tasmota_energy('owned.invalid')['total_kwh']==12.0
    smart_plug.record_energy_start(9,993)
    reading['total']=12.5
    _archive(review_db,status)
    assert smart_plug.record_energy_for_print(9,993)==.5
    assert _energy(review_db)==((.5,.1),.5)
    # Applied once: a duplicate completion cannot double-count.
    smart_plug.record_energy_start(9,993);reading['total']=13.0
    smart_plug.record_energy_for_print(9,993)
    assert _energy(review_db)==((.5,.1),.5)


def test_energy_waits_for_an_archive_created_after_completion(metered,review_db,monkeypatch):
    smart_plug,reading=metered
    timers=[]
    class Timer:
        def __init__(self,delay,function,args):self.delay=delay;self.function=function;self.args=args;self.daemon=False;timers.append(self)
        def start(self):pass
    monkeypatch.setattr(smart_plug.threading,'Timer',Timer)
    smart_plug.record_energy_start(9,993);reading['total']=12.25
    assert smart_plug.record_energy_for_print(9,993)==.25
    assert len(timers)==1 and timers[0].delay==1.0
    _archive(review_db)
    timers[0].function(*timers[0].args)
    assert _energy(review_db)==((.25,.05),.25)


@pytest.mark.parametrize('case',['counter_reset','no_start','no_reading'])
def test_unknown_energy_is_never_invented(metered,review_db,monkeypatch,case):
    smart_plug,reading=metered
    _archive(review_db)
    if case!='no_start':smart_plug.record_energy_start(9,993)
    if case=='counter_reset':reading['total']=0.5
    if case=='no_reading':monkeypatch.setattr(smart_plug,'get_energy',lambda printer:None)
    assert smart_plug.record_energy_for_print(9,993) is None
    assert _energy(review_db)==((None,None),0)


def test_print_jobs_api_exposes_archive_energy_for_the_energy_widget(metered,review_db):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy.orm import Session
    from core.db import get_db
    from core.rbac import require_role
    from modules.jobs.routes import presets
    smart_plug,reading=metered
    smart_plug.record_energy_start(9,993);reading['total']=12.5;_archive(review_db)
    smart_plug.record_energy_for_print(9,993)
    app=FastAPI();app.include_router(presets.router)
    def database():
        with Session(review_db.get_bind()) as session:yield session
    app.dependency_overrides[get_db]=database
    from core.dependencies import get_current_user
    app.dependency_overrides[get_current_user]=lambda:{'id':3,'role':'admin','group_id':None,'is_active':True}
    row=next(r for r in TestClient(app).get('/print-jobs').json() if r['id']==993)
    assert row['energy_kwh']==.5 and row['energy_cost']==.1


@pytest.mark.usefixtures("pro_license")  # chargebacks are a Pro analytics feature (cert-fix-G)
def test_chargeback_actual_http_dates_and_users_and_advertised_grams(analytics_client,review_db):
    from core.dependencies import get_current_user
    analytics_client.app.dependency_overrides[get_current_user]=lambda:{"id":3,"role":"admin","group_id":1,"is_active":True}
    from modules.reporting.routes import reports
    analytics_client.app.include_router(reports.router)
    review_db.execute(text('UPDATE jobs SET charged_to_user_id=NULL'))
    review_db.execute(text("INSERT INTO jobs(item_name,charged_to_user_id,charged_to_org_id,estimated_cost,duration_hours,created_at) VALUES('Owned in window',1,1,5,2,'2026-10-11 12:00:00'),('Owned second',1,1,7,3,'2026-10-11 13:00:00'),('Owned outside',1,1,99,9,'2026-01-01 12:00:00')"))
    review_db.execute(text("UPDATE jobs SET status='completed' WHERE item_name LIKE 'Owned%' AND charged_to_user_id IS NOT NULL"))  # chargebacks cover consumed (terminal) jobs
    review_db.commit()
    response=analytics_client.get('/reports/chargebacks',params={'start_date':'2026-10-10','end_date':'2026-10-12'})
    assert response.status_code==200
    row=response.json()[0]
    assert row['user_id']==1 and row['job_count']==2 and row['total_cost']==12 and row['total_hours']==5
    assert 'total_grams' in row, 'Advertised per-user grams are absent from chargeback report'

@pytest.mark.parametrize('tier,features,status',[('community',[],403),('pro',['usage_reports'],200),('pro',[],200)])
def test_actual_usage_route_uses_real_signed_feature_gate(analytics_client,signed_license,tier,features,status):
    signed_license(tier=tier,features=features)
    response=analytics_client.get('/education/usage-report')
    assert response.status_code==status


def test_job_creation_pins_current_charged_user(approval_client,review_db):
    client,identity=approval_client;identity['role']='operator'
    response=client.post('/api/v1/jobs',json={'item_name':'Owned autocharge'})
    assert response.status_code==201
    assert review_db.execute(text('SELECT charged_to_user_id FROM jobs WHERE id=:id'),{'id':response.json()['id']}).scalar_one()==identity['id']


def test_job_events_drive_energy_metering_through_the_real_bus(metered,review_db,monkeypatch):
    from core.event_bus import InMemoryEventBus
    from core.interfaces.event_bus import Event
    from core import events
    smart_plug,reading=metered
    monkeypatch.setattr(smart_plug,'_in_background',lambda function,*args:function(*args))
    monkeypatch.setattr(smart_plug,'_ENERGY_START_DELAY',0)
    monkeypatch.setattr(smart_plug,'on_print_start',lambda *a,**k:None)
    monkeypatch.setattr(smart_plug,'on_print_complete',lambda *a,**k:None)
    bus=InMemoryEventBus();smart_plug.register_subscribers(bus)
    bus.publish(Event(events.JOB_STARTED,'synthetic',{'printer_id':9,'print_job_id':993}))
    reading['total']=13.0;_archive(review_db,'cancelled')
    bus.publish(Event(events.JOB_CANCELLED,'synthetic',{'printer_id':9,'print_job_id':993}))
    assert _energy(review_db)==((1.0,.2),1.0)


def test_failed_print_records_energy_through_the_real_bus(metered,review_db,monkeypatch):
    from core.event_bus import InMemoryEventBus
    from core.interfaces.event_bus import Event
    from core import events
    smart_plug,reading=metered
    monkeypatch.setattr(smart_plug,'_in_background',lambda function,*args:function(*args))
    monkeypatch.setattr(smart_plug,'_ENERGY_START_DELAY',0)
    monkeypatch.setattr(smart_plug,'on_print_start',lambda *a,**k:None)
    bus=InMemoryEventBus();smart_plug.register_subscribers(bus)
    bus.publish(Event(events.JOB_STARTED,'synthetic',{'printer_id':9,'print_job_id':993}))
    reading['total']=12.3;_archive(review_db,'failed')
    bus.publish(Event(events.JOB_FAILED,'synthetic',{'printer_id':9,'print_job_id':993,'success':False}))
    assert _energy(review_db)==((.3,.06),.3)


def test_print_ending_before_the_delayed_start_reading_leaves_no_orphan(metered,review_db):
    smart_plug,reading=metered
    assert smart_plug.record_energy_for_print(9,993) is None   # end arrives first
    smart_plug.record_energy_start(9,993)                       # late start reading is skipped
    assert smart_plug._energy_starts=={} and smart_plug._energy_ended=={}
