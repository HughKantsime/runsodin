"""Real signed order/schedule/dispatch through each actual first-start callback."""
import hashlib,json
from contextlib import contextmanager
from types import SimpleNamespace
import pytest
from sqlalchemy import text
from tests.test_contracts.test_economic_creator_wiring import creators
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.dispatch_test_support import configure_dispatch


@pytest.mark.parametrize('scope_name,actor,org',[('org','admin',1),('global','platform',None)])
@pytest.mark.parametrize('path,protocol',[('mqtt','bambu'),('generic-events','prusalink'),('moonraker','moonraker')])
def test_real_order_owned_first_start_callback_freezes_exact_unit_evidence(creators,review_db,tmp_path,monkeypatch,scope_name,actor,org,path,protocol):
    from core import auth as auth_module
    from core.registry import registry
    from core.db_utils import _ConnectionAdapter
    from modules.orders.production import OrderFulfillmentService
    from modules.orders.routes.orders_crud import router
    from modules.printers import dispatch
    from modules.printers.monitors import mqtt_job_lifecycle,moonraker_monitor
    from modules.notifications import job_events
    client,auth,_,_=creators;client.app.include_router(router,prefix='/api')
    monkeypatch.setitem(registry._providers,'OrderFulfillmentProvider',OrderFulfillmentService())
    engine,source=configure_dispatch(review_db,tmp_path,monkeypatch,protocol=protocol)
    with engine.begin() as c:
        c.execute(text('UPDATE printers SET org_id=:org WHERE id=59'),{'org':org})
        c.execute(text("INSERT INTO models(id,name,org_id,quantity_per_bed,build_time_hours,default_filament_type) VALUES(101,'Owned callback model',:org,1,1,'PLA')"),{'org':org})
        c.execute(text("INSERT INTO products(id,name,org_id,price) VALUES(101,'Owned callback product',:org,20)"),{'org':org})
        c.execute(text('INSERT INTO product_components(product_id,model_id,quantity_needed) VALUES(101,101,1)'))
        c.execute(text('INSERT INTO print_files(id,model_id,filename,original_filename,stored_path,org_id) VALUES(58,101,:name,:name,:source,:org)'),{'name':source.name,'source':str(source),'org':org})
    r=client.post('/api/orders',headers=auth(actor),json={'order_number':'Owned callback order','items':[{'product_id':101,'quantity':1,'unit_price':20}]});assert r.status_code==200,r.text
    order_id=r.json()['id'];r=client.post(f'/api/orders/{order_id}/schedule',headers=auth(actor));assert r.status_code==200,r.text
    with engine.begin() as c:
        job=c.execute(text('SELECT min(j.id) FROM jobs j JOIN order_items i ON i.id=j.order_item_id WHERE i.order_id=:id'),{'id':order_id}).scalar_one()
        c.execute(text('UPDATE jobs SET printer_id=59,print_file_id=58,payload_sha256=:sha,plate_index=0 WHERE id=:id'),{'sha':hashlib.sha256(source.read_bytes()).hexdigest(),'id':job})
        if org is None:assert c.execute(text('SELECT count(*) FROM cost_global_order_origins g JOIN orders o ON g.order_uid=o.economic_uid WHERE o.id=:id'),{'id':order_id}).scalar_one()==1
    r=client.post('/api/spools',headers=auth(actor),json={'filament_id':80,'purchase_evidence':{'currency':'USD','price':'12.99','net_weight_g':'750.25'}});assert r.status_code==200,r.text
    with engine.begin() as c:
        c.execute(text('UPDATE filament_slots SET assigned_spool_id=:id WHERE printer_id=59 AND slot_number=1'),{'id':r.json()['id']})
        c.execute(text("INSERT INTO finance_currency_settings(owner_scope,owner_key,currency) VALUES(:scope,:key,'USD') ON CONFLICT(owner_scope,owner_key) DO UPDATE SET currency='USD'"),{'scope':scope_name,'key':org or 0})
    names=[]
    monkeypatch.setattr(dispatch,'_dispatch_'+protocol,lambda jid,source,name,creds,**kw:(names.append(name) or True,'Owned physical sender stub'))
    assert dispatch.dispatch_job(59,job)[0]
    @contextmanager
    def database():
        raw=engine.raw_connection()
        try:yield _ConnectionAdapter(raw,False)
        finally:raw.close()
    events=[]
    def committed(event):
        with engine.connect() as fresh:
            assert fresh.execute(text('SELECT spool_snapshot FROM print_jobs WHERE id=:id'),{'id':event.data['print_job_id']}).scalar_one() is not None
        events.append(event)
    monkeypatch.setattr(job_events,'get_db',database);monkeypatch.setattr(mqtt_job_lifecycle,'get_db',database)
    monkeypatch.setattr(job_events,'get_event_bus',lambda:SimpleNamespace(publish=committed))
    from core import event_bus
    monkeypatch.setattr(event_bus,'get_event_bus',lambda:SimpleNamespace(publish=committed))
    monkeypatch.setattr(moonraker_monitor,'engine',engine)
    if path=='mqtt':identifier,linked,_=mqtt_job_lifecycle.record_job_started(59,'Owned',{'subtask_name':names[0],'gcode_file':'Metadata/plate_1.gcode','ams':{'tray_now':0}},lambda **kw:None,lambda:None);assert linked==job
    elif path=='generic-events':identifier=job_events.job_started(59,names[0])
    else:
        monitor=moonraker_monitor.MoonrakerMonitor.__new__(moonraker_monitor.MoonrakerMonitor)
        monitor.printer_id=59;monitor.name='Owned';monitor._current_job_db_id=None
        monkeypatch.setattr(monitor,'_try_auto_link',lambda *a:pytest.fail('Reserved exact authority must bypass fuzzy match'))
        monitor._job_started(SimpleNamespace(filename=names[0],total_layers=1,bed_target=60,nozzle_target=200));identifier=monitor._current_job_db_id
    assert identifier is not None
    with engine.connect() as c:
        row=c.execute(text('SELECT p.economic_uid,p.scheduled_job_id,l.state,l.owner_scope,l.owner_key,p.spool_snapshot FROM print_jobs p JOIN cost_source_lifetimes l ON l.birth_nonce=p.economic_birth_nonce WHERE p.id=:id'),{'id':identifier}).one()
        assert row[0] and tuple(row[1:5])==(job,'known',scope_name,org or 0)
        captured=json.loads(row[5]);value=captured['unit_valuation']
        assert value['target']['owner_scope']==scope_name
        if protocol=='bambu':
            # Cached start tray cannot resolve an unmapped Bambu source.
            assert captured['material_plan']['reason']=='missing_material_mapping'
            assert 'observed_tray_fallback' in captured['material_plan']
            assert value['state']=='unknown' and value['reason']=='unresolved_material_mapping'
            assert value['sources']==[]
        else:
            assert value['state']=='known'
            assert value['sources'][0]['price_decimal']=='12.99' and value['sources'][0]['net_weight_g_decimal']=='750.25'
        assert c.execute(text('SELECT observed_print_job_id FROM dispatch_attempt_receipts WHERE job_id=:id'),{'id':job}).scalar_one()==identifier

    if path=='mqtt':
        assert len(events)==1
        before_snapshot=row[5]
        replay,linked,_=mqtt_job_lifecycle.record_job_started(59,'Owned',{'subtask_name':names[0],'gcode_file':'Metadata/plate_1.gcode','ams':{'tray_now':0}},lambda **kw:None,lambda:None)
        assert replay==identifier and linked==job
        assert len(events)==1, 'Same physical observation must not republish JOB_STARTED'
        with engine.connect() as c:
            assert c.execute(text('SELECT count(*) FROM print_jobs')).scalar_one()==1
            assert c.execute(text('SELECT spool_snapshot FROM print_jobs WHERE id=:id'),{'id':identifier}).scalar_one()==before_snapshot
