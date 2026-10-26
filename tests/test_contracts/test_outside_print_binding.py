"""Real HTTP/SQLite outside binding, restoration and acceptance contracts."""
import json
from datetime import datetime, timezone, timedelta
import pytest
from sqlalchemy import text, select
from tests.test_contracts.test_batch_acceptance import batch, make_recipe, body, shop, review_db
from tests.test_contracts.test_cert_D_jobsB_monitor import lifecycle, scheduled, NOW


@pytest.fixture
def outside(batch, monkeypatch):
    from modules.orders import outside_prints
    from core.registry import registry
    from modules.printers.binding import PrinterBindingService
    monkeypatch.setitem(registry._providers, 'PrinterBindingProvider', PrinterBindingService())
    # Configuration equality is mocked at its pure boundary; no credentials read.
    monkeypatch.setattr(outside_prints,'configuration_key',lambda db,pid:'b'*64)
    from core.economic_source_creation import _create_attempt
    db=batch[3]
    db.begin()
    created=_create_attempt(db,{'printer_id':301,'status':'running','started_at':datetime.now(timezone.utc),
        'filename':'cabinet lock v3.1.3mf','job_name':'cabinet lock v3.1'})
    db.execute(text("UPDATE print_jobs SET status='completed',ended_at=CURRENT_TIMESTAMP WHERE id=:id"),{'id':created.id})
    db.commit()
    recipe=make_recipe(batch)
    return batch,created.id,recipe


def binding_body(recipe):
    return {'recipe_id':recipe['id'],'attestation':{'started_outside_odin':True,'file_revision_confirmed':True},'attested_by':'owner_confirmed_to_operator'}


def bind(outside, **changes):
    batch,pid,recipe=outside
    data=binding_body(recipe);data.update(changes)
    return batch[0].post(f'/products/production/outside-prints/{pid}/bind',json=data)


def test_bind_inspect_list_and_retry_exactly_once(outside):
    batch,pid,recipe=outside
    client,user,calls,db,*_=batch
    response=bind(outside)
    assert response.status_code==200,response.text
    saved=response.json()
    assert saved['actor_user_id']==user['id'] and saved['actor_token_id'] is None
    assert saved['owner_attested'] is True and saved['odin_dispatched'] is False
    assert saved['filename']=='cabinet lock v3.1.3mf' and saved['recipe_revision']==recipe
    assert bind(outside).status_code==409
    jobs=client.get('/products/production/jobs').json()['jobs']
    entry=next(j for j in jobs if j.get('print_job_id')==pid)
    assert entry['kind']=='outside_print' and entry['recipe']==recipe
    request=body(batch,recipe);request.pop('job_id');request['print_job_id']=pid
    response=client.post('/products/production/inspections',json=request)
    assert response.status_code==200,response.text
    receipt=response.json()
    assert receipt['print_job_id']==pid and receipt['outside_binding']['id']==saved['id']
    assert client.post('/products/production/inspections',json=request).json()==receipt
    assert db.execute(text('SELECT COUNT(*) FROM production_acceptances')).scalar_one()==1
    assert db.execute(text('SELECT COUNT(*) FROM production_stock_additions')).scalar_one()==1
    assert not any(path=='/v2/inventory/changes/batch-create' for _,path,_,_ in calls)
    audit=db.execute(text("SELECT details,actor_user_id,actor_username,actor_auth_kind FROM audit_logs WHERE action='production.outside_print_bound'")).one()
    assert audit[1:] == (None,None,None)
    assert json.loads(audit[0])['actor_user_id']==user['id']


@pytest.mark.parametrize('attestation',[{}, {'started_outside_odin':False,'file_revision_confirmed':True}, {'started_outside_odin':True,'file_revision_confirmed':False}, {'started_outside_odin':1,'file_revision_confirmed':True}])
def test_attestation_is_required_and_strict(outside,attestation):
    assert bind(outside,attestation=attestation).status_code in (409,422)


@pytest.mark.parametrize('role,kind,scopes,expected',[
    ('viewer','jwt',[],403),('operator','user_api_token',['write'],403),
    ('operator','user_api_token',['write:production'],200),('operator','jwt',[],200)])
def test_production_operator_authority(outside,role,kind,scopes,expected):
    batch,_,_=outside
    batch[1].update(role=role,_auth_kind=kind,_token_scopes=scopes,_token_id=77 if kind=='user_api_token' else None)
    response=bind(outside)
    assert response.status_code==expected,response.text
    if expected==200: assert response.json()['actor_token_id']==batch[1]['_token_id']


@pytest.mark.parametrize('reason',['dispatch','printer','config','unqualified','education','accepted','unfinished','owner'])
def test_binding_refusals(outside,monkeypatch,reason):
    batch,pid,recipe=outside
    db=batch[3]
    if reason=='dispatch':
        db.execute(text('UPDATE dispatch_attempt_receipts SET observed_print_job_id=:id WHERE job_id=302'),{'id':pid})
    elif reason=='printer':
        db.execute(text("INSERT INTO printers(id,name,org_id) VALUES(302,'Other',1)"))
        db.execute(text('UPDATE print_jobs SET printer_id=302 WHERE id=:id'),{'id':pid})
    elif reason=='config':
        from modules.orders import outside_prints
        monkeypatch.setattr(outside_prints,'configuration_key',lambda *a:'c'*64)
    elif reason=='unqualified':
        from modules.orders.batch_production import recipes,packed
        definition=dict(recipe);definition.pop('id');definition['qualified']=False
        db.execute(recipes.update().where(recipes.c.id==recipe['id']).values(definition=packed(definition)))
    elif reason=='education':
        db.execute(text("UPDATE print_jobs SET job_name='odin-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' WHERE id=:id"),{'id':pid})
    elif reason=='accepted':
        from modules.orders.batch_production import receipts
        physical=db.execute(text('SELECT economic_birth_nonce FROM print_jobs WHERE id=:id'),{'id':pid}).scalar_one()
        db.execute(receipts.insert().values(id='f'*32,owner=1,job_uid=physical,claim_token='e'*32,recipe_id=recipe['id'],request_hash='a'*64,definition=json.dumps({'physical_uid':physical})))
    elif reason=='unfinished':db.execute(text('UPDATE print_jobs SET ended_at=NULL WHERE id=:id'),{'id':pid})
    elif reason=='owner':batch[1]['group_id']=2
    db.commit()
    response=bind(outside)
    assert response.status_code in ((403,404) if reason=='owner' else (409,)),response.text
    assert db.execute(text("SELECT COUNT(*) FROM system_config WHERE key LIKE 'outside_print_binding:%'")).scalar_one()==0


def test_mislinked_held_job_restores_without_completion_credit(outside):
    batch,pid,_=outside
    db=batch[3]
    db.execute(text("UPDATE dispatch_attempt_receipts SET observed_print_job_id=NULL WHERE job_id=302"))
    db.execute(text("UPDATE print_jobs SET scheduled_job_id=NULL WHERE scheduled_job_id=302"))
    db.execute(text("UPDATE print_jobs SET scheduled_job_id=302 WHERE id=:id"),{'id':pid})
    db.execute(text("UPDATE jobs SET hold=true,duration_hours=7.5,actual_start=CURRENT_TIMESTAMP,actual_end=CURRENT_TIMESTAMP WHERE id=302"))
    from core.print_attempts import bind_print_attempt,sqlalchemy_runner
    bind_print_attempt(sqlalchemy_runner(db),pid,302)
    db.execute(text("INSERT INTO system_config(key,value) VALUES('quota_attempt:job:302:0','1')"));db.commit()
    response=bind(outside)
    assert response.status_code==200,response.text
    row=db.execute(text('SELECT status,hold,actual_start,actual_end,duration_hours FROM jobs WHERE id=302')).one()
    assert tuple(row)==('pending',1,None,None,None)
    assert db.execute(text('SELECT scheduled_job_id FROM print_jobs WHERE id=:id'),{'id':pid}).scalar_one() is None
    assert db.execute(text("SELECT COUNT(*) FROM system_config WHERE key IN ('quota_attempt:job:302:0',:key)"),{'key':f'quota_attempt:print_seq:{pid}'}).scalar_one()==0
    assert response.json()['restored_job']['job_id']==302
    assert db.execute(text('SELECT value FROM system_config WHERE key=:key'),{'key':f'outside_print_previous_job:{pid}'}).first() is None


@pytest.mark.parametrize('fence',['held','receipt','recovery'])
def test_mqtt_never_heuristically_links_held_or_retained_history(lifecycle,fence):
    db,start,*_=lifecycle
    scheduled(db,801,9,NOW+timedelta(minutes=30))
    if fence=='held':db.execute(text('UPDATE jobs SET hold=true WHERE id=801'))
    elif fence=='receipt':
        db.execute(text("INSERT INTO dispatch_attempt_receipts(claim_token,printer_id,job_id,configuration_key,prepared_power_key,observed_filename_key) VALUES(:token,9,801,:key,:key,:key)"),{'token':'a'*32,'key':'b'*64})
    else:
        db.execute(text("""INSERT INTO dispatch_recovery_history(audit_id,actor_id,actor_identity,owner_org_id,charged_org_id,printer_id,job_id,attempt_handle,snapshot_handle,decision,reason_category,facts)
            VALUES(1,1,'synthetic',1,1,9,801,:attempt,:snapshot,'release','verified_no_pending_dispatch','{}')"""),{'attempt':'dr1.'+'a'*64,'snapshot':'ds1.'+'b'*64})
    db.commit()
    pid,linked,_=start(name='Queued part 801')
    assert pid and linked is None
    assert db.execute(text('SELECT scheduled_job_id FROM print_jobs WHERE id=:id'),{'id':pid}).scalar_one() is None
    assert db.execute(text('SELECT status FROM jobs WHERE id=801')).scalar_one()=='pending'


@pytest.mark.parametrize('source',['moonraker','prusalink','elegoo','anycubic_lan'])
@pytest.mark.parametrize('fence',['held','receipt','recovery'])
def test_other_monitor_paths_never_link_held_job(review_db,tmp_path,monkeypatch,source,fence):
    from contextlib import contextmanager
    from types import SimpleNamespace
    from core.db_utils import _ConnectionAdapter
    from tests.test_contracts.test_dispatch_real_monitor_correlation import configure_dispatch
    protocol='moonraker' if source=='moonraker' else 'prusalink'
    engine,_=configure_dispatch(review_db,tmp_path,monkeypatch,protocol=protocol)
    review_db.execute(text('UPDATE jobs SET hold=true WHERE printer_id=59 AND id<>53'))
    if fence=='held':review_db.execute(text('UPDATE jobs SET hold=true WHERE id=53'))
    elif fence=='receipt':
        review_db.execute(text('INSERT INTO dispatch_attempt_receipts(claim_token,printer_id,job_id,configuration_key,prepared_power_key,observed_filename_key) VALUES(:token,59,53,:key,:key,:key)'),{'token':'a'*32,'key':'b'*64})
    else:
        review_db.execute(text("""INSERT INTO dispatch_recovery_history(audit_id,actor_id,actor_identity,owner_org_id,charged_org_id,printer_id,job_id,attempt_handle,snapshot_handle,decision,reason_category,facts)
            VALUES(1,1,'synthetic',1,1,59,53,:attempt,:snapshot,'release','verified_no_pending_dispatch','{}')"""),{'attempt':'dr1.'+'a'*64,'snapshot':'ds1.'+'b'*64})
    review_db.execute(text('UPDATE printers SET api_type=:protocol WHERE id=59'),{'protocol':source})
    review_db.commit()
    if source=='moonraker':
        from modules.printers.monitors import moonraker_monitor as module
        monkeypatch.setattr(module,'engine',engine)
        monitor=module.MoonrakerMonitor(59,'Synthetic','127.0.0.1')
        monitor._job_started(SimpleNamespace(filename='Owned dispatch test',total_layers=10,bed_target=60,nozzle_target=200))
        identifier=monitor._current_job_db_id
        with engine.begin() as conn:
            assert monitor._link_job(conn,53) is False
    else:
        from modules.notifications import job_events as module
        @contextmanager
        def local_db():
            raw=engine.raw_connection()
            try:yield _ConnectionAdapter(raw,False)
            finally:raw.close()
        monkeypatch.setattr(module,'get_db',local_db)
        identifier=module.job_started(59,'Owned dispatch test')
    assert identifier is not None
    with engine.connect() as conn:
        assert conn.execute(text('SELECT scheduled_job_id FROM print_jobs WHERE id=:id'),{'id':identifier}).scalar_one() is None
        assert conn.execute(text('SELECT status,hold FROM jobs WHERE id=53')).one()==('scheduled',1 if fence=='held' else 0)


def test_normal_dispatched_inspection_source_is_unchanged():
    """The outside adapter must keep the exact fixed-yield/baseline rules."""
    import inspect
    from modules.orders import batch_production, outside_prints
    normal=inspect.getsource(batch_production.inspect_batch)
    outside=inspect.getsource(outside_prints.inspect)
    # The output validation and baseline/addition calculation is verbatim.
    start="    expected = {(o['product_id'],o['kind']):o for o in spec['outputs']}"
    end='    definition = '
    assert normal[normal.index(start):normal.index(end)]==outside[outside.index(start):outside.index(end)]


def test_binding_and_restoration_roll_back_if_audit_fails(outside,monkeypatch):
    from sqlalchemy.orm import Session
    from modules.orders import outside_prints
    from modules.orders.batch_schemas import OutsideBindingInput
    batch,pid,recipe=outside
    db=batch[3]
    def refuse(*args,**kwargs):raise RuntimeError('synthetic audit failure')
    monkeypatch.setattr(outside_prints,'audit',refuse)
    with Session(db.get_bind()) as work:
        with pytest.raises(RuntimeError,match='synthetic audit failure'):
            outside_prints.bind(work,batch[1],pid,OutsideBindingInput(**binding_body(recipe)))
    assert db.execute(text("SELECT COUNT(*) FROM system_config WHERE key LIKE 'outside_print_binding:%'")).scalar_one()==0
    assert db.execute(text('SELECT COUNT(*) FROM production_acceptances')).scalar_one()==0


def test_metric_only_adhoc_job_does_not_block_owner_binding(outside):
    batch,pid,_=outside
    db=batch[3]
    db.execute(text("INSERT INTO jobs(id,item_name,printer_id,status,quantity,hold) VALUES(303,'cabinet lock v3.1',301,'completed',1,false)"))
    db.execute(text('UPDATE print_jobs SET scheduled_job_id=303 WHERE id=:id'),{'id':pid});db.commit()
    response=bind(outside)
    assert response.status_code==200,response.text
    assert response.json()['restored_job']['metrics_only'] is True
    assert db.execute(text('SELECT scheduled_job_id FROM print_jobs WHERE id=:id'),{'id':pid}).scalar_one() is None


def test_retained_heuristic_previous_state_is_restored(outside):
    batch,pid,_=outside
    db=batch[3]
    db.execute(text('UPDATE dispatch_attempt_receipts SET observed_print_job_id=NULL WHERE job_id=302'))
    db.execute(text('UPDATE print_jobs SET scheduled_job_id=NULL WHERE scheduled_job_id=302'))
    db.execute(text('UPDATE print_jobs SET scheduled_job_id=302 WHERE id=:id'),{'id':pid})
    previous={'job_id':302,'state':{'status':'scheduled','hold':False,'actual_start':None,'actual_end':None,'duration_hours':1.5,'queue_position':3}}
    db.execute(text('INSERT INTO system_config(key,value) VALUES(:key,:value)'),{'key':f'outside_print_previous_job:{pid}','value':json.dumps(previous)});db.commit()
    response=bind(outside)
    assert response.status_code==200,response.text
    assert db.execute(text('SELECT status,hold,actual_start,actual_end,duration_hours,queue_position FROM jobs WHERE id=302')).one()==('scheduled',0,None,None,1.5,3)


def test_outside_inspection_cannot_switch_recipe_or_supply_both_sources(outside):
    batch,pid,recipe=outside
    assert bind(outside).status_code==200
    other=make_recipe(batch)
    request=body(batch,other);request.pop('job_id');request['print_job_id']=pid
    assert batch[0].post('/products/production/inspections',json=request).status_code==409
    request['job_id']=302
    assert batch[0].post('/products/production/inspections',json=request).status_code==422


def test_two_binding_requests_only_one_attestation_wins(outside):
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:bind(outside),range(2)))
    assert sorted(r.status_code for r in results)==[200,409]


@pytest.mark.parametrize('change',['model_owner','education'])
def test_inspection_rechecks_bound_model(outside,monkeypatch,change):
    batch,pid,recipe=outside
    assert bind(outside).status_code==200
    model_id=recipe['payload']['model_id']
    if change=='model_owner':
        batch[3].execute(text('UPDATE models SET org_id=2 WHERE id=:id'),{'id':model_id})
        batch[3].commit()
    else:
        from core.interfaces import education_policy
        from fastapi import HTTPException
        original=education_policy.assert_generic_model_reference_allowed
        def refuse(db,identifier):
            if identifier==model_id:raise HTTPException(404,'Model not found')
            return original(db,identifier)
        monkeypatch.setattr(education_policy,'assert_generic_model_reference_allowed',refuse)
    request=body(batch,recipe);request.pop('job_id');request['print_job_id']=pid
    response=batch[0].post('/products/production/inspections',json=request)
    assert response.status_code==(409 if change=='model_owner' else 404),response.text
    assert batch[3].execute(text('SELECT COUNT(*) FROM production_acceptances')).scalar_one()==0


def test_snapshot_only_written_after_success_replaced_and_bounded(lifecycle):
    from core.print_attempts import heuristic_link_state,remember_heuristic_link,sqlalchemy_runner,clear_job_attempts,prune_heuristic_links
    db,start,*_=lifecycle
    scheduled(db,801,9,NOW+timedelta(minutes=30))
    run=sqlalchemy_runner(db)
    state=heuristic_link_state(run,801)
    assert state['status']=='scheduled'
    assert db.execute(text("SELECT COUNT(*) FROM system_config WHERE key LIKE 'outside_print_previous_job:%'")).scalar_one()==0
    pid,linked,_=start(name='Queued part 801')
    assert linked==801
    key=f'outside_print_previous_job:{pid}'
    saved=json.loads(db.execute(text('SELECT value FROM system_config WHERE key=:key'),{'key':key}).scalar_one())
    assert saved['state']==state
    remember_heuristic_link(run,pid,801,{**state,'duration_hours':7})
    assert json.loads(db.execute(text('SELECT value FROM system_config WHERE key=:key'),{'key':key}).scalar_one())['state']['duration_hours']==7
    saved['recorded_at']=0
    db.execute(text('UPDATE system_config SET value=:value WHERE key=:key'),{'key':key,'value':json.dumps(saved)})
    prune_heuristic_links(run)
    assert db.execute(text('SELECT value FROM system_config WHERE key=:key'),{'key':key}).first() is None
    remember_heuristic_link(run,pid,801,state)
    clear_job_attempts(run,801)
    assert db.execute(text('SELECT value FROM system_config WHERE key=:key'),{'key':key}).first() is None


@pytest.mark.parametrize('stale',[True,False])
@pytest.mark.parametrize('fence',['held','receipt','recovery'])
def test_fenced_jobs_still_participate_in_schedule_cleanup(lifecycle,stale,fence):
    db,start,alerts,reschedules=lifecycle
    scheduled(db,801,9,NOW+timedelta(hours=-3 if stale else 1))
    if fence=='held':db.execute(text('UPDATE jobs SET hold=true WHERE id=801'))
    elif fence=='receipt':
        db.execute(text('INSERT INTO dispatch_attempt_receipts(claim_token,printer_id,job_id,configuration_key,prepared_power_key,observed_filename_key) VALUES(:t,9,801,:k,:k,:k)'),{'t':'a'*32,'k':'b'*64})
    else:
        db.execute(text("""INSERT INTO dispatch_recovery_history(audit_id,actor_id,actor_identity,owner_org_id,charged_org_id,printer_id,job_id,attempt_handle,snapshot_handle,decision,reason_category,facts)
            VALUES(1,1,'synthetic',1,1,9,801,:a,:s,'release','verified_no_pending_dispatch','{}')"""),{'a':'dr1.'+'a'*64,'s':'ds1.'+'b'*64})
    db.commit()
    pid,linked,_=start(name='Unrelated outside plate')
    assert pid and linked is None
    assert db.execute(text('SELECT status FROM jobs WHERE id=801')).scalar_one()=='pending'
    assert alerts and reschedules


def test_refused_transition_keeps_no_snapshot(lifecycle,monkeypatch):
    from modules.jobs import services
    db,start,*_=lifecycle
    scheduled(db,801,9,NOW+timedelta(minutes=30))
    original=services.transition_job
    def refuse(*args,**kwargs):
        if len(args)>2 and args[2]=='printing':return False
        return original(*args,**kwargs)
    monkeypatch.setattr(services,'transition_job',refuse)
    pid,linked,_=start(name='Queued part 801')
    assert pid and linked is None
    assert db.execute(text('SELECT value FROM system_config WHERE key=:key'),{'key':f'outside_print_previous_job:{pid}'}).first() is None


@pytest.mark.parametrize('fence',['held','receipt','recovery'])
def test_moonraker_reconnect_keeps_running_physical_print(review_db,tmp_path,monkeypatch,fence):
    from types import SimpleNamespace
    from tests.test_contracts.test_dispatch_real_monitor_correlation import configure_dispatch
    from modules.printers.monitors import moonraker_monitor as module
    engine,_=configure_dispatch(review_db,tmp_path,monkeypatch,protocol='moonraker')
    monkeypatch.setattr(module,'engine',engine)
    monitor=module.MoonrakerMonitor(59,'Synthetic','127.0.0.1')
    status=SimpleNamespace(filename='Unmatched outside plate',total_layers=0,bed_target=60,nozzle_target=200)
    monitor._job_started(status)
    pid=monitor._current_job_db_id
    assert pid is not None
    # Retained running print is resumed regardless of the linked job's fences.
    review_db.execute(text('UPDATE print_jobs SET scheduled_job_id=53 WHERE id=:id'),{'id':pid})
    if fence=='held':review_db.execute(text('UPDATE jobs SET hold=true WHERE id=53'))
    elif fence=='receipt':
        review_db.execute(text('INSERT INTO dispatch_attempt_receipts(claim_token,printer_id,job_id,configuration_key,prepared_power_key,observed_filename_key) VALUES(:t,59,53,:k,:k,:k)'),{'t':'a'*32,'k':'b'*64})
    else:
        review_db.execute(text("""INSERT INTO dispatch_recovery_history(audit_id,actor_id,actor_identity,owner_org_id,charged_org_id,printer_id,job_id,attempt_handle,snapshot_handle,decision,reason_category,facts)
            VALUES(1,1,'synthetic',1,1,59,53,:a,:s,'release','verified_no_pending_dispatch','{}')"""),{'a':'dr1.'+'a'*64,'s':'ds1.'+'b'*64})
    review_db.commit()
    monitor._current_job_db_id=None
    monitor._job_started(status)
    assert monitor._current_job_db_id==pid
    assert review_db.execute(text('SELECT COUNT(*) FROM print_jobs WHERE printer_id=59')).scalar_one()==1


def test_diagnostics_omits_outside_binding_and_undo_state(outside,monkeypatch):
    import asyncio
    import io
    import zipfile
    from modules.system.routes_admin import download_support_bundle
    from modules.system import diagnostics
    import subprocess
    batch,pid,_=outside
    db=batch[3]
    assert bind(outside).status_code==200
    db.execute(text('INSERT INTO system_config(key,value) VALUES(:key,:value)'),{'key':f'outside_print_previous_job:{pid}','value':json.dumps({'job_id':302,'state':{'filename':'synthetic private name'}})})
    db.commit()
    monkeypatch.setattr(subprocess,'check_output',lambda *a,**k:'synthetic docker' if k.get('text') else b'synthetic platform')
    monkeypatch.setattr(diagnostics,'_read_tail',lambda *a:[])
    response=download_support_bundle(user=batch[1],db=db)
    async def collect():
        return b''.join([chunk async for chunk in response.body_iterator])
    with zipfile.ZipFile(io.BytesIO(asyncio.run(collect()))) as archive:
        settings=json.loads(archive.read('settings_safe.json'))
    assert not any(k.startswith(('outside_print_binding:','outside_print_previous_job:')) for k in settings)
