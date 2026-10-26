"""Part 2/3 contracts: deterministic advice, durable content and admission safety."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
import json
from types import SimpleNamespace
from unittest.mock import Mock
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session
from modules.jobs import completion_summary as service
from modules.printers import start_next
from modules.printers.routes_start_next import StartNext
from tests.test_contracts.test_private_attempt_valuation_route import private_attempt, creators, review_db


@pytest.fixture
def queue(review_db, monkeypatch):
    db=review_db
    from core.registry import registry
    from modules.organizations.services import EducationPolicyService
    monkeypatch.setitem(registry._providers, 'EducationPolicyProvider', EducationPolicyService())
    db.execute(text("INSERT INTO printers(id,name) VALUES(204,'Other printer')"))
    db.execute(text("INSERT INTO groups(id,name) VALUES(99,'Other org')"))
    db.execute(text("INSERT INTO printers(id,name,api_type,is_active,org_id,shared) VALUES(201,'Queue printer','moonraker',TRUE,NULL,FALSE)"))
    db.execute(text("INSERT INTO models(id,name,default_filament_type,build_time_hours,color_requirements) VALUES(201,'Queue model','PLA',1,'{}')"))
    db.execute(text("INSERT INTO print_files(id,model_id,filename,stored_path,org_id) VALUES(201,201,'queue.gcode','/synthetic/queue.gcode',NULL)"))
    for jid in (201,202,203):
        db.execute(text("INSERT INTO jobs(id,item_name,model_id,print_file_id,printer_id,payload_sha256,plate_index,status,quantity,hold,priority,queue_position,created_at,updated_at,colors_required,filament_type) VALUES(:id,'Queue job',201,201,201,:sha,0,'pending',1,FALSE,3,:position,'2026-10-01','2026-10-01','black','PLA')"),{'id':jid,'position':None if jid==201 else jid-201,'sha':'a'*64})
    db.commit()
    monkeypatch.setattr(service,'configuration_key',lambda *a:'c'*64)
    monkeypatch.setattr(service,'finished_attempt',lambda *a:{'economic_uid':'retained-finished-attempt','ended_at':'2026-10-01T00:00:00Z'})
    monkeypatch.setattr(service.assortment,'preview',Mock(side_effect=AssertionError('Live preview forbidden')))
    from modules.printers.models import Printer
    return db.get(Printer,201)


def advice(db,printer,**kw):return service.next_suggestion(db,printer,**kw)


def test_scheduler_nulls_and_final_id(queue,review_db):
    assert advice(review_db,queue)['job_id']==202
    review_db.execute(text('UPDATE jobs SET queue_position=1 WHERE id IN (202,203)'));review_db.commit()
    assert advice(review_db,queue)['job_id']==202


@pytest.mark.parametrize('mutation',[
    "UPDATE jobs SET hold=TRUE WHERE id=202",
    "UPDATE jobs SET status='completed' WHERE id=202",
    "UPDATE jobs SET printer_id=204 WHERE id=202",
    "UPDATE jobs SET charged_to_org_id=99 WHERE id=202",
    "UPDATE jobs SET model_id=NULL WHERE id=202",
    "UPDATE jobs SET required_tags='[\"missing\"]' WHERE id=202",
    "UPDATE jobs SET target_type='protocol',target_filter='bambu' WHERE id=202",
    "UPDATE jobs SET target_type='specific',target_filter='204' WHERE id=202",
    "UPDATE jobs SET target_type='specific',target_filter='deleted:204' WHERE id=202",
])
def test_candidate_predicates(queue,review_db,mutation):
    review_db.execute(text(mutation));review_db.commit()
    visible=lambda j:j.charged_to_org_id is None
    assert advice(review_db,queue,visible=visible)['job_id']==203


def test_no_fit_and_org_isolation(queue,review_db):
    assert advice(review_db,queue,visible=lambda j:False)['reason']=='No queued job fits this printer.'
    review_db.execute(text('UPDATE models SET org_id=99 WHERE id=201'));review_db.commit();review_db.expire_all()
    assert advice(review_db,queue)['job_id'] is None


def recipe(db,jid,product=1,count=5,identifier='r1'):
    job=db.execute(text('SELECT * FROM jobs WHERE id=:id'),{'id':jid}).mappings().one()
    payload={**dict(job),'configuration_key':'c'*64}
    spec={'qualified':True,'payload':service.batches.recipe_payload(payload),'outputs':[{'product_id':product,'product_name':'Main','kind':'main','expected':count}]}
    db.execute(service.batches.recipes.insert().values(id=identifier,owner=0,definition=json.dumps(spec)))
    return spec


def test_exact_payload_qualified_ambiguity(queue,review_db):
    payload=recipe(review_db,202)['payload']
    assert len(service.matching_recipes(review_db,0,payload))==1
    for key in payload:
        assert not service.matching_recipes(review_db,0,{**payload,key:'different'})
    recipe(review_db,202,identifier='r2')
    assert len(service.matching_recipes(review_db,0,payload))==2
    assert not service.matching_recipes(review_db,99,payload)


def test_replenishment_main_only_and_freshness(queue,review_db,monkeypatch):
    recipe(review_db,202)
    review_db.execute(text("UPDATE jobs SET payload_sha256=:sha WHERE id=203"),{'sha':'b'*64})
    recipe(review_db,203,product=2,identifier='r2')
    monkeypatch.setattr(service.assortment,'load',lambda *a:({'products':{'1':{'assortment':'active','kind':'other'},'2':{'assortment':'active','kind':'dragon_jumbo'}}},None))
    facts={1:{'quantity':0},2:{'quantity':1}}
    monkeypatch.setattr(service,'snapshot',lambda db,owner,pid,at:facts.get(pid))
    assert advice(review_db,queue)['job_id']==203 # shortfall 14 beats 5
    facts.pop(2) # stale/unknown removes only this signal
    assert advice(review_db,queue)['job_id']==202
    facts.clear()
    assert advice(review_db,queue)['reason']=='Scheduler queue order'


@pytest.mark.parametrize('age,expected',[(0,7),(3600,7),(3601,None),(-1,None)])
def test_snapshot_freshness_and_owner(queue,review_db,monkeypatch,age,expected):
    now=datetime.now(timezone.utc)
    monkeypatch.setattr(service.square_store,'load',lambda *a:({'mappings':{'1':{'variation_id':'v','location_id':'l'}}},None))
    from modules.orders.stock_snapshots import record
    record(review_db,0,'v','l',{'state':'known','quantity':'7','calculated_at':(now-timedelta(seconds=age)).isoformat()})
    review_db.execute(text('UPDATE stock_snapshots SET read_at=:at'),{'at':now-timedelta(seconds=age)})
    stock=service.snapshot(review_db,0,1,now)
    assert (stock['quantity'] if stock else None)==expected
    assert service.snapshot(review_db,99,1,now) is None
    record(review_db,0,'v','l',{'state':'unknown'})
    assert service.snapshot(review_db,0,1,now) is None


def test_material_fresh_after_finished_and_fingerprint_attempt(queue,review_db,monkeypatch):
    from modules.jobs.models import Job
    at=datetime.now(timezone.utc);end=at-timedelta(minutes=1)
    job=review_db.get(Job,202)
    review_db.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type,color,loaded_at) VALUES(201,1,'PLA','black',:at)"),{'at':at})
    finished={'economic_uid':'first','ended_at':end}
    assert service.material_facts(review_db,job,queue,finished,at,'c'*64)[0]=='no_change'
    review_db.execute(text("UPDATE filament_slots SET color='white' WHERE printer_id=201"))
    assert service.material_facts(review_db,job,queue,finished,at,'c'*64)[0]=='change_needed'
    review_db.execute(text('UPDATE filament_slots SET loaded_at=:at WHERE printer_id=201'),{'at':end-timedelta(seconds=1)})
    assert service.material_facts(review_db,job,queue,finished,at,'c'*64)[0]=='verify_needed'
    monkeypatch.setattr(service,'finished_attempt',lambda *a:finished)
    first=advice(review_db,queue)['fingerprint'];finished['economic_uid']='reprint'
    assert advice(review_db,queue)['fingerprint']!=first


def request(suggestion,**kw):
    return StartNext(suggested_job_id=suggestion['job_id'],suggestion_fingerprint=suggestion['fingerprint'],request_id=uuid.uuid4(),bed_clear_confirmed=True,**kw)


def test_start_requires_confirmation_and_changed_fingerprint(queue,review_db,monkeypatch):
    data=request(advice(review_db,queue));data.bed_clear_confirmed=False
    with pytest.raises(HTTPException) as exc:start_next.start(review_db,queue,{'id':1},data)
    assert exc.value.status_code==422
    data.bed_clear_confirmed=True;data.suggestion_fingerprint='f'*64
    with pytest.raises(HTTPException) as exc:start_next.start(review_db,queue,{'id':1},data)
    assert exc.value.status_code==409 and 'suggestion' in exc.value.detail


def test_request_replay_before_recompute_and_actor_isolation(queue,review_db,monkeypatch):
    data=request(advice(review_db,queue));user={'id':1,'role':'admin'}
    from modules.printers import dispatch
    send=Mock(return_value=(False,'Existing gate refused'))
    monkeypatch.setattr(dispatch,'dispatch_job',send)
    first=start_next.start(review_db,queue,user,data)
    assert first['state']=='refused' and send.call_count==1
    monkeypatch.setattr(start_next,'next_suggestion',Mock(side_effect=AssertionError('No recompute on replay')))
    assert start_next.start(review_db,queue,user,data)==first
    assert send.call_count==1
    with pytest.raises(HTTPException):start_next.start(review_db,queue,{'id':99},data)


def test_concurrent_start_claim_once(queue,review_db,monkeypatch):
    from modules.printers import dispatch
    send=Mock(return_value=(True,'Synthetic transport'))
    monkeypatch.setattr(dispatch,'dispatch_job',send)
    suggestion=advice(review_db,queue);requests=[request(suggestion),request(suggestion)]
    engine=review_db.get_bind();review_db.rollback()
    def call(data):
        from modules.printers.models import Printer
        with Session(engine) as db:
            try:return start_next.start(db,db.get(Printer,201),{'id':1,'role':'admin'},data)
            except HTTPException as exc:return exc.status_code
    with ThreadPoolExecutor(2) as pool:results=list(pool.map(call,requests))
    assert send.call_count==1
    assert 409 in results


def test_bed_write_bound_to_admission_connection_and_failure_rolls_back(queue,review_db,monkeypatch):
    suggestion=advice(review_db,queue);data=request(suggestion)
    record={'actor':{'user_id':None,'token_id':None},'printer_id':201,'job_id':202,'fingerprint':suggestion['fingerprint'],'attestation':'owner_confirmed_to_operator','result':{}}
    key=start_next.request_key(data.request_id)
    start_next.insert(review_db.connection(),key,record);review_db.commit()
    marker=start_next._context.set({'key':key,'record':record,'user':{'role':'admin'}})
    try:
        with review_db.get_bind().begin() as conn:
            start_next.confirm_on_admission(conn,token='d'*32,job={'id':202,'printer_id':201})
            row=conn.execute(text("SELECT detail FROM job_timeline_events WHERE kind='bed_clear_confirmed'")).scalar_one()
            assert json.loads(row)['claim_token']=='d'*32
        with pytest.raises(RuntimeError):
            with review_db.get_bind().begin() as conn:start_next.confirm_on_admission(conn,token='e'*32,job={'id':202,'printer_id':201})
    finally:start_next._context.reset(marker)


def test_summary_health_recipe_states_and_short_text(private_attempt,review_db,monkeypatch):
    from core.job_terminal_observations import claim_terminal
    physical=private_attempt[3]
    review_db.execute(text("UPDATE print_jobs SET status='completed',ended_at='2026-10-13 13:00:00' WHERE id=:id"),{'id':physical})
    assert claim_terminal(review_db.connection(),physical)
    review_db.commit()
    uid=review_db.execute(text('SELECT economic_uid FROM print_jobs WHERE id=:id'),{'id':physical}).scalar_one()
    monkeypatch.setattr(service,'next_suggestion',lambda *a,**kw:{'job_id':None,'reason':'No queued job fits this printer.'})
    result=service.build_summary(review_db,uid)
    from pathlib import Path
    fixture=json.loads(Path('frontend/src/test-fixtures/job-completion-wire.json').read_text())
    assert set(fixture['summary_fields'])<=set(result)
    assert result['recipe_state']=='No qualified recipe'
    assert result['health']=='history incomplete'
    assert len(result['text'])<300 and result['bed_line'] in result['text']
    assert result['inspection_url']==f"/products?inspect={result['job_id']}"
    printer_id=review_db.execute(text('SELECT printer_id FROM print_jobs WHERE id=:id'),{'id':physical}).scalar_one()
    review_db.execute(text("INSERT INTO timeline_writer_state(process_id,started_at,clean_shutdown,last_drained_at,printer_ids) VALUES('healthy:1','2026-10-13 11:00:00',1,'2026-10-13 14:00:00',:printers)"),{'printers':json.dumps([printer_id])})
    assert service.build_summary(review_db,uid)['health']=='none'
    review_db.execute(text("UPDATE timeline_writer_state SET drop_epoch=1,epoch_history='[[\"2026-10-13T12:30:00Z\",\"drop\",1]]'"))
    assert service.build_summary(review_db,uid)['health']=='history incomplete'
    review_db.execute(text("UPDATE timeline_writer_state SET drop_epoch=0,epoch_history='[]'"))
    review_db.execute(text("INSERT INTO job_timeline_events(id,job_id,print_attempt_uid,kind,code,occurred_at) VALUES('health',:job,:uid,'hms','HMS_1',CURRENT_TIMESTAMP)"),{'job':result['job_id'],'uid':uid})
    result=service.build_summary(review_db,uid)
    assert result['health']=='attention required' and result['health_events'][0]['code']=='HMS_1'


def test_fault_once_per_attempt_kind_code(queue,review_db,monkeypatch):
    from modules.notifications import job_faults,alert_dispatcher
    stage=Mock(return_value='plan');monkeypatch.setattr(alert_dispatcher,'stage_alert',stage)
    item={'job':202,'printer':201,'attempt':'first','kind':'paused','code':None}
    assert job_faults.stage(review_db,item)=='plan'
    assert job_faults.stage(review_db,item) is None
    assert job_faults.stage(review_db,{**item,'kind':'runout'})=='plan'
    assert job_faults.stage(review_db,{**item,'attempt':'reprint'})=='plan'
    assert stage.call_count==3


from tests.test_contracts.test_dispatch_final_admission_behavior import setup, state

@pytest.mark.parametrize('fail_write',[False,True])
def test_real_admission_attestation_atomic_with_claim_and_receipt(setup,monkeypatch,fail_write):
    from modules.printers.dispatch_admission import admit
    engine,_,inputs=setup
    record={'actor':{'user_id':3,'token_id':None},'printer_id':59,'job_id':53,'fingerprint':'f'*64,'attestation':'owner_confirmed_to_operator','result':{}}
    key=start_next.request_key(uuid.uuid4())
    with engine.begin() as conn:
        start_next.insert(conn,key,record)
        if fail_write:
            conn.exec_driver_sql("CREATE TRIGGER refuse_bed BEFORE INSERT ON job_timeline_events WHEN NEW.kind='bed_clear_confirmed' BEGIN SELECT RAISE(ABORT,'synthetic bed write refusal'); END")
    monkeypatch.setattr(start_next,'next_suggestion',lambda *a,**kw:{'job_id':53,'fingerprint':'f'*64})
    marker=start_next._context.set({'key':key,'record':record,'user':{'id':3,'role':'admin','group_id':1}})
    try:result=admit(engine,**inputs)
    finally:start_next._context.reset(marker)
    assert result.admitted is not fail_write
    assert state(engine)==('preparing' if fail_write else 'adapter_entered')
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM job_timeline_events WHERE kind='bed_clear_confirmed'")).scalar_one()==int(not fail_write)
        assert conn.execute(text('SELECT COUNT(*) FROM dispatch_attempt_receipts')).scalar_one()==int(not fail_write)
        saved=start_next.load(conn,key)
        assert ('claim_token' in saved) is not fail_write
        if not fail_write:
            assert conn.execute(text("SELECT actor_user_id FROM job_timeline_events WHERE kind='bed_clear_confirmed'")).scalar_one()==3


def test_printer_routes_auth_scope_and_tenant(queue,creators,review_db):
    from modules.printers.routes_start_next import router
    client,auth,token,_=creators;client.app.include_router(router,prefix='/api')
    url='/api/printers/201/next-suggestion'
    assert client.get(url).status_code==401
    assert client.get(url,headers=token(['read:spools'])).status_code==403
    response=client.get(url,headers=token(['read:printers']))
    assert response.status_code==200,response.text
    from pathlib import Path
    fixture=json.loads(Path('frontend/src/test-fixtures/job-completion-wire.json').read_text())
    assert {k:response.json()[k] for k in fixture['suggestion']}==fixture['suggestion']
    assert response.headers['cache-control']=='private, no-store'
    payload=request(response.json()).model_dump(mode='json')
    assert client.post('/api/printers/201/start-next',headers=token(['read:printers']),json=payload).status_code==403
    review_db.execute(text('UPDATE printers SET org_id=99 WHERE id=201'));review_db.commit()
    assert client.get(url,headers=auth('admin')).status_code==404
    assert client.post('/api/printers/201/start-next',headers=auth('admin'),json=payload).status_code==404
    spec=client.app.openapi()
    assert '/api/printers/{printer_id}/next-suggestion' in spec['paths']
    assert '/api/printers/{printer_id}/start-next' in spec['paths']
    inventory={'routes':{p:spec['paths'][p] for p in fixture['routes'].values()},
               'schemas':{k:spec['components']['schemas'][k] for k in ('StartNext','Suggestion','StartResult')},
               'claims':{'start_requires':['jobs.start','write:jobs'],'suggestion_requires':['read:printers'],
                         'hardware_certified':False,'summary_fields':fixture['summary_fields']}}
    assert inventory==json.loads(Path('tests/fixtures/job-completion-openapi.json').read_text())
    target=Path('artifacts/job-completion');target.mkdir(exist_ok=True,parents=True)
    (target/'openapi-claims.json').write_text(json.dumps(inventory,indent=2)+'\n')


@pytest.mark.parametrize('status,paid,platform,expected',[
    ('pending',True,'manual',203),('shipped',True,'wholesale',202),
    ('cancelled',True,'wholesale',202),('pending',False,'manual',202),
    ('pending',False,'wholesale',203),
])
def test_open_paid_wholesale_order_predicates(queue,review_db,monkeypatch,status,paid,platform,expected):
    # Stub only show_fulfillment's attested payment document, never its queue
    # linkage or open-order predicate. State is paid_committed, not a guessed flag.
    from modules.orders import show_fulfillment
    monkeypatch.setattr(show_fulfillment,'load',lambda *a:({},None))
    monkeypatch.setattr(show_fulfillment,'summary',lambda *a:{'state':'paid_committed' if paid else 'payment_unconfirmed'})
    review_db.execute(text("INSERT INTO products(id,name) VALUES(201,'Queue product')"))
    review_db.execute(text('INSERT INTO orders(id,status,platform,created_at) VALUES(201,:status,:platform,\'2026-09-01\')'),{'status':status,'platform':platform})
    review_db.execute(text('INSERT INTO order_items(id,order_id,product_id,quantity) VALUES(201,201,201,1)'))
    review_db.execute(text('UPDATE jobs SET order_item_id=201 WHERE id=203'));review_db.commit()
    assert advice(review_db,queue)['job_id']==expected


def test_summary_expected_flush_projection_ambiguous_and_inspection_reason(private_attempt,review_db,monkeypatch):
    from core.job_terminal_observations import claim_terminal
    physical=private_attempt[3]
    review_db.execute(text("UPDATE print_jobs SET status='completed',ended_at='2026-10-13 13:00:00' WHERE id=:id"),{'id':physical})
    claim_terminal(review_db.connection(),physical);review_db.commit()
    uid,job_id=review_db.execute(text('SELECT economic_uid,scheduled_job_id FROM print_jobs WHERE id=:id'),{'id':physical}).one()
    payload=dict(review_db.execute(text('SELECT * FROM jobs WHERE id=:id'),{'id':job_id}).mappings().one())
    payload['configuration_key']=review_db.execute(text('SELECT configuration_key FROM dispatch_attempt_receipts WHERE observed_print_job_id=:id'),{'id':physical}).scalar_one()
    definition={'qualified':True,'payload':service.batches.recipe_payload(payload),'outputs':[
        {'product_id':101,'product_name':'Mini','kind':'main','expected':5},
        {'product_id':102,'product_name':'Flush','kind':'flush','expected':2}]}
    review_db.execute(service.batches.recipes.insert().values(id='qualified',owner=0,definition=json.dumps(definition)))
    monkeypatch.setattr(service,'next_suggestion',lambda *a,**kw:None)
    monkeypatch.setattr(service,'snapshot',lambda db,owner,pid,at:{'quantity':10} if pid==101 else None)
    def unavailable(*a):raise HTTPException(409,'Multiple physical attempts need review')
    monkeypatch.setattr(service.batches,'job_evidence',unavailable)
    result=service.build_summary(review_db,uid)
    assert result['recipe_state']=='matched' and result['product']==['Mini']
    assert [v['expected'] for v in result['expected_outputs']]==[5,2]
    assert result['square_projection']==[{'product_id':101,'quantity':15},{'product_id':102,'quantity':None}]
    assert result['inspection_unavailable_reason']=='Multiple physical attempts need review'
    assert 'inspection unavailable:' in result['text'] and len(result['text'])<300
    review_db.execute(service.batches.recipes.insert().values(id='duplicate',owner=0,definition=json.dumps(definition)))
    result=service.build_summary(review_db,uid)
    assert result['recipe_state']=='Recipe ambiguous' and result['expected_outputs']==[]


@pytest.mark.parametrize('age,after_end,expected',[(0,True,'no_change'),(600,True,'no_change'),(601,True,'verify_needed'),(0,False,'verify_needed')])
def test_ams_freshness_exact_boundary(queue,review_db,monkeypatch,age,after_end,expected):
    from modules.jobs.models import Job
    now=datetime.now(timezone.utc);read=now-timedelta(seconds=age)
    end=read-timedelta(seconds=1) if after_end else read
    queue.api_type='bambu';review_db.flush()
    review_db.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type,color,loaded_at) VALUES(201,1,'PLA','black',:at)"),{'at':now})
    # Exact current-read generation, ownership and configuration are real schema
    # facts; only the timestamp is synthetic.
    review_db.execute(text("INSERT INTO ams_read_generations(generation_id,printer_id,owner_org_id,configuration_key,runtime_incarnation,started_at,state,completed_at) VALUES(:g,201,NULL,:c,:r,:at,'ready',:at)"),{'g':'1'*32,'c':'c'*64,'r':'2'*32,'at':read})
    review_db.execute(text('INSERT INTO ams_current_reads(printer_id,generation_id,runtime_incarnation) VALUES(201,:g,:r)'),{'g':'1'*32,'r':'2'*32})
    assert service.material_facts(review_db,review_db.get(Job,202),queue,{'ended_at':end},now,'c'*64)[0]==expected


def test_attestation_enum_and_strict_confirmation():
    from pydantic import ValidationError
    base=dict(suggested_job_id=1,suggestion_fingerprint='a'*64,request_id=str(uuid.uuid4()),bed_clear_confirmed=True)
    for change in ({'attestation':'a free text quote'},{'bed_clear_confirmed':1},{'bed_clear_confirmed':'true'}):
        with pytest.raises(ValidationError):StartNext(**{**base,**change})


def test_fault_lane_survives_dropped_timeline(monkeypatch):
    from core import job_timeline
    from modules.notifications import job_faults
    notify=Mock();monkeypatch.setattr(job_faults,'notify',notify)
    monkeypatch.setattr(job_timeline,'_writer',None)
    assert job_timeline.for_printer(201,'paused') is False
    notify.assert_called_once_with(201,'paused',None)


def test_missing_finished_identity_cannot_authorize_start(queue,review_db,monkeypatch):
    monkeypatch.setattr(service,'finished_attempt',lambda *a:None)
    suggestion=advice(review_db,queue)
    assert suggestion['job_id']==202 and suggestion['fingerprint'] is None
    data=StartNext(suggested_job_id=202,suggestion_fingerprint='a'*64,request_id=uuid.uuid4(),bed_clear_confirmed=True)
    with pytest.raises(HTTPException) as exc:start_next.start(review_db,queue,{'id':1,'role':'admin'},data)
    assert exc.value.status_code==409


@pytest.mark.parametrize('stored_state',['admitted','dispatched'])
def test_admitted_request_replay_ignores_changed_queue_and_confirmation(queue,review_db,monkeypatch,stored_state):
    data=request(advice(review_db,queue));user={'id':1,'role':'admin'}
    stored={'actor':start_next.principal(user),'printer_id':201,'job_id':202,'result':{'success':True,'state':stored_state,'job_id':202,'request_id':str(data.request_id)}}
    start_next.insert(review_db.connection(),start_next.request_key(data.request_id),stored);review_db.commit()
    data.bed_clear_confirmed=False;data.suggestion_fingerprint='f'*64
    monkeypatch.setattr(start_next,'next_suggestion',Mock(side_effect=AssertionError('No queue read on replay')))
    assert start_next.start(review_db,queue,user,data)==stored['result']


def test_reprint_claim_gets_its_own_summary_and_replays_none(tmp_path):
    # Physical identity claim boundary only, as in the Foundation's historical
    # observation tests. Two physical IDs intentionally share one scheduled job.
    from sqlalchemy import create_engine
    from core.job_terminal_observations import claim_terminal
    engine=create_engine('sqlite:///'+str(tmp_path/'attempts.db'))
    with engine.begin() as conn:
        conn.exec_driver_sql('CREATE TABLE jobs(id INTEGER PRIMARY KEY,charged_to_org_id INTEGER)')
        conn.exec_driver_sql('CREATE TABLE print_jobs(id INTEGER PRIMARY KEY,economic_uid TEXT,economic_birth_nonce TEXT,scheduled_job_id INTEGER,printer_id INTEGER,status TEXT)')
        conn.exec_driver_sql("CREATE TABLE job_summary_outbox(print_attempt_uid TEXT PRIMARY KEY,attempt_birth TEXT,job_id INTEGER,printer_id INTEGER,org_id INTEGER,outcome TEXT,delivery_state TEXT DEFAULT '{}')")
        conn.exec_driver_sql('CREATE TABLE alert_outbox(id INTEGER PRIMARY KEY,job_id INTEGER,alert_kind TEXT)')
        conn.exec_driver_sql('INSERT INTO jobs VALUES(1,NULL)')
        conn.exec_driver_sql("INSERT INTO print_jobs VALUES(1,'attempt-first','birth-first',1,1,'completed'),(2,'attempt-reprint','birth-reprint',1,1,'completed')")
        assert claim_terminal(conn,1) and claim_terminal(conn,2)
        assert not claim_terminal(conn,1) and not claim_terminal(conn,2)
        assert conn.execute(text('SELECT count(*) FROM job_summary_outbox')).scalar_one()==2
        assert conn.execute(text('SELECT count(*) FROM alert_outbox')).scalar_one()==2
    engine.dispose()


def test_start_next_runs_normal_dispatch_and_bed_is_committed_before_adapter(review_db,tmp_path,monkeypatch):
    from tests.test_contracts.dispatch_test_support import configure_dispatch,join_background_threads
    from modules.printers import dispatch
    from modules.printers.models import Printer
    engine,_=configure_dispatch(review_db,tmp_path,monkeypatch)
    with engine.begin() as conn:conn.execute(text("UPDATE jobs SET status='pending' WHERE id=53"))
    suggestion={'job_id':53,'fingerprint':'f'*64}
    monkeypatch.setattr(start_next,'next_suggestion',lambda *a,**kw:suggestion)
    seen=[]
    def adapter(jid,path,filename,creds):
        with engine.connect() as conn:
            event=conn.execute(text("SELECT detail,actor_user_id FROM job_timeline_events WHERE job_id=53 AND kind='bed_clear_confirmed'")).one()
            token=conn.execute(text('SELECT claim_token FROM dispatch_attempt_receipts WHERE job_id=53')).scalar_one()
            assert json.loads(event[0])['claim_token']==token and event[1]==3
            assert filename==f'odin-dispatch-{token}.gcode'
        seen.append(True)
        return True,'Synthetic hardware accepted'
    monkeypatch.setattr(dispatch,'_dispatch_moonraker',adapter)
    data=request(suggestion)
    result=start_next.start(review_db,review_db.get(Printer,59),{'id':3,'role':'admin','group_id':1},data)
    join_background_threads()
    assert result['success'] and seen==[True]
    monkeypatch.setattr(start_next,'next_suggestion',Mock(side_effect=AssertionError('No replay recomputation')))
    assert start_next.start(review_db,review_db.get(Printer,59),{'id':3,'role':'admin','group_id':1},data)==result
    assert seen==[True]


def test_fingerprint_changes_when_required_material_payload_or_revision_changes(queue,review_db):
    initial=advice(review_db,queue)['fingerprint']
    for assignment in ("colors_required='white'", "payload_sha256='changed'", 'plate_index=1', "updated_at='2026-10-02'"):
        review_db.execute(text('UPDATE jobs SET '+assignment+' WHERE id=202'))
        review_db.expire_all()
        changed=advice(review_db,queue)['fingerprint']
        assert changed!=initial
        initial=changed


from tests.test_contracts.test_private_attempt_valuation_route import org_attempt, production_order, bom_client


def test_delivered_summary_metadata_is_org_isolated(org_attempt,review_db,monkeypatch):
    from core.job_terminal_observations import claim_terminal
    from modules.notifications import alert_outbox,alert_dispatcher
    physical=org_attempt[3]
    job_id=review_db.execute(text('SELECT scheduled_job_id FROM print_jobs WHERE id=:id'),{'id':physical}).scalar_one()
    review_db.execute(text("UPDATE jobs SET item_name='Private summary content' WHERE id=:id"),{'id':job_id})
    review_db.execute(text("UPDATE print_jobs SET status='completed',ended_at=CURRENT_TIMESTAMP WHERE id=:id"),{'id':physical})
    assert claim_terminal(review_db.connection(),physical)
    outbox_id=review_db.execute(text('SELECT id FROM alert_outbox WHERE job_id=:id'),{'id':job_id}).scalar_one()
    review_db.commit()
    monkeypatch.setattr(service,'next_suggestion',lambda *a,**kw:None)
    monkeypatch.setattr(alert_dispatcher,'_deliver_staged_alert',lambda *a:None)
    monkeypatch.setattr(alert_outbox,'deliver_mobile',lambda *a:None)
    assert alert_outbox.dispatch(review_db.get_bind(),outbox_id)
    review_db.expire_all()
    rows=review_db.execute(text("SELECT user_id,metadata_json FROM alerts WHERE job_id=:id AND alert_type='print_complete'"),{'id':job_id}).all()
    assert any(r[0]==3 for r in rows) and not any(r[0]==5 for r in rows)
    assert all(json.loads(r[1])['summary']['job_name']=='Private summary content' for r in rows)
    assert not alert_outbox.dispatch(review_db.get_bind(),outbox_id)


def test_required_color_matches_fresh_slot_hex(queue,review_db):
    from modules.jobs.models import Job
    now=datetime.now(timezone.utc)
    job=review_db.get(Job,202);job.colors_required='#000000FF';review_db.flush()
    review_db.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type,color,color_hex,loaded_at) VALUES(201,1,'PLA','Black','#000000',:at)"),{'at':now})
    result=service.material_facts(review_db,job,queue,{'ended_at':now-timedelta(minutes=1)},now,'c'*64)
    assert result[0]=='no_change'
