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


def test_tasmota_counter_delta_and_cost_persist_in_owned_database(review_db,monkeypatch):
    from modules.printers import smart_plug
    monkeypatch.setattr(smart_plug,'engine',review_db.get_bind())
    monkeypatch.setattr(smart_plug,'_http_request',lambda url:(200,'{"StatusSNS":{"ENERGY":{"Total":12.5,"Power":40,"Voltage":230,"Current":0.2}}}'))
    assert smart_plug.tasmota_energy('owned.invalid')['total_kwh']==12.5
    monkeypatch.setattr(smart_plug,'get_energy',lambda printer:smart_plug.tasmota_energy('owned.invalid'))
    review_db.execute(text("INSERT INTO jobs(id,item_name) VALUES(993,'Owned energy')"))
    review_db.execute(text("UPDATE printers SET plug_energy_kwh=0 WHERE id=9"))
    review_db.execute(text("INSERT OR REPLACE INTO system_config(key,value) VALUES('energy_cost_per_kwh','0.2')"));review_db.commit()
    smart_plug.record_energy_for_job(9,993,12)
    review_db.expire_all()
    from sqlalchemy import inspect
    assert {'energy_kwh','energy_cost'} <= {c['name'] for c in inspect(review_db.get_bind()).get_columns('jobs')}, 'Energy recorder writes missing canonical job columns'
    assert tuple(review_db.execute(text('SELECT energy_kwh,energy_cost FROM jobs WHERE id=993')).one())==(.5,.1)
    assert review_db.execute(text('SELECT plug_energy_kwh FROM printers WHERE id=9')).scalar_one()==.5


def test_chargeback_actual_http_dates_and_users_and_advertised_grams(analytics_client,review_db):
    from core.dependencies import get_current_user
    analytics_client.app.dependency_overrides[get_current_user]=lambda:{"id":3,"role":"admin","group_id":1,"is_active":True}
    from modules.reporting.routes import reports
    analytics_client.app.include_router(reports.router)
    review_db.execute(text('UPDATE jobs SET charged_to_user_id=NULL'))
    review_db.execute(text("INSERT INTO jobs(item_name,charged_to_user_id,charged_to_org_id,estimated_cost,duration_hours,created_at) VALUES('Owned in window',1,1,5,2,'2026-10-11 12:00:00'),('Owned second',1,1,7,3,'2026-10-11 13:00:00'),('Owned outside',1,1,99,9,'2026-01-01 12:00:00')"));review_db.commit()
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
