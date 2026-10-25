"""Real HTTP/DB production acceptance; Square reads simulated, never stock writes."""
import importlib
import json
import uuid
import pytest
from sqlalchemy import text,select
from tests.test_contracts.test_square_readonly_workflow import shop, mapped
from tests.test_contracts.test_education_review_workflow import review_db


@pytest.fixture
def batch(shop, monkeypatch):
    client,user,calls,db,app,*_=shop
    from modules.orders.routes.batches import router
    app.include_router(router)
    app.router.routes.sort(key=lambda r: 0 if getattr(r,"path", "").startswith("/products/production") else 1)
    from core.registry import registry
    from modules.organizations.services import EducationPolicyService
    monkeypatch.setitem(registry._providers,"EducationPolicyProvider",EducationPolicyService())
    # Existing fixture bootstraps through migration035.
    db.execute(text("INSERT INTO printers(id,name,model,org_id) VALUES(301,'Test X1','X1',1)"))
    db.execute(text("UPDATE printers SET api_type='bambu' WHERE id=301"))
    db.execute(text("INSERT INTO models(id,name,org_id) VALUES(301,'Part',1)"))
    from core.economic_identity import _add_created
    from core.economic_source_creation import _create_attempt
    from modules.jobs.models import Job
    from datetime import datetime, timezone
    db.execute(text("INSERT INTO jobs(id,item_name,status) VALUES(300,'legacy fixture','pending')"))
    for jid in (301,302):
        created=_add_created(db,Job(item_name='Batch',status='completed',quantity=1,model_id=301,printer_id=301,
            print_file_id=301,payload_sha256='a'*64,plate_index=0,charged_to_org_id=1),'job',owner_org_id=1)
        assert created.id==jid
        claim=uuid.uuid4().hex
        observed=_create_attempt(db,{'printer_id':301,'scheduled_job_id':jid,'status':'running','started_at':datetime.now(timezone.utc),'filename':'odin-dispatch-'+claim+'.3mf','job_name':'odin-dispatch-'+claim})
        db.execute(text("UPDATE print_jobs SET status='completed',ended_at=CURRENT_TIMESTAMP WHERE id=:id"),{'id':observed.id})
        from core.auth import SECRET_KEY
        from core.dispatch_receipts import filename_fingerprint
        fp=filename_fingerprint(SECRET_KEY.encode(),claim,'odin-dispatch-'+claim+'.3mf')
        db.execute(text("""INSERT INTO dispatch_attempt_receipts(claim_token,printer_id,job_id,configuration_key,prepared_power_key,observed_filename_key,observed_print_job_id)
            VALUES(:claim,301,:id,:key,:key,:fp,:observed)"""),{'id':jid,'observed':observed.id,'claim':claim,'key':'b'*64,'fp':fp})
    db.commit()
    status=mapped(shop)
    return (*shop,status)


def make_recipe(batch,qualified=True,outputs=None):
    client=batch[0]
    response=client.post('/products/production/recipes',json={'reference_job_id':301,'name':'Qualified part','materials':'PLA black, 0.4 nozzle','qualification_confirmed':qualified,
        'outputs':outputs or [{'product_id':101,'kind':'main','expected':5}]})
    assert response.status_code==200,response.text
    return response.json()


def body(batch,recipe):
    return {'recipe_id':recipe['id'],'job_id':302,'square_revision':batch[-1]['revision'],'inspection_confirmed':True,
            'outputs':[{'product_id':101,'kind':'main','accepted':4,'rejected':1}]}


def test_live_routes_recipe_inspection_and_prepared_square_request(batch):
    client,_,calls,db,*_=batch
    recipe=make_recipe(batch)
    assert recipe['qualified'] is True
    eligible=client.get('/products/production/jobs').json()
    assert {j['id'] for j in eligible['jobs']}=={301,302}
    response=client.post('/products/production/inspections',json=body(batch,recipe))
    assert response.status_code==200,response.text
    receipt=response.json()
    assert receipt['stock_status']=='prepared'
    state=client.get('/products/production').json()
    assert state['square_writes_enabled'] is False
    addition=state['additions'][0]
    assert addition['state']=='prepared'
    assert 'request' not in addition and 'request_body' not in addition
    from modules.orders.batch_production import additions
    saved=json.loads(db.execute(select(additions.c.request_body)).scalar_one())
    assert saved['idempotency_key']==receipt['id']
    change=saved['changes'][0]
    assert change['adjustment']['quantity']=='4'
    assert change['adjustment']['from_state']=='NONE' and change['adjustment']['to_state']=='IN_STOCK'
    assert change['adjustment']['catalog_object_id']=='V1'
    assert change['adjustment']['from_location_id']==change['adjustment']['to_location_id']=='L1'
    assert 'location_id' not in change['adjustment']
    assert all(path!='/v2/inventory/changes/batch-create' for _,path,_,_ in calls)
    assert client.post('/products/production/inspections',json=body(batch,recipe)).json()['id']==receipt['id']
    changed=body(batch,recipe);changed['outputs'][0].update(accepted=3,rejected=2)
    assert client.post('/products/production/inspections',json=changed).status_code==409
    assert db.execute(text('SELECT COUNT(*) FROM production_acceptances')).scalar()==1
    assert db.execute(text('SELECT COUNT(*) FROM production_stock_additions')).scalar()==1


@pytest.mark.parametrize('change',[
    {'inspection_confirmed':False},
    {'outputs':[{'product_id':101,'kind':'main','accepted':5,'rejected':1}]},
    {'outputs':[{'product_id':101,'kind':'main','accepted':-1,'rejected':6}]},
    {'outputs':[{'product_id':101,'kind':'main','accepted':True,'rejected':4}]},
    {'outputs':[]},
    {'outputs':[{'product_id':101,'kind':'flush','accepted':4,'rejected':1}]},
    {'square_revision':'stale'},
])
def test_invalid_inspection_never_creates_receipt(batch,change):
    recipe=make_recipe(batch);request=body(batch,recipe);request.update(change)
    assert batch[0].post('/products/production/inspections',json=request).status_code in (409,422)
    assert batch[3].execute(text('SELECT COUNT(*) FROM production_acceptances')).scalar()==0


@pytest.mark.parametrize('column,value',[
    ('status','failed'),('quantity',2),('payload_sha256','c'*64),('plate_index',1),
    ('model_id',None)])
def test_wrong_or_legacy_job_refused(batch,column,value):
    recipe=make_recipe(batch)
    batch[3].execute(text(f'UPDATE jobs SET {column}=:v WHERE id=302'),{'v':value});batch[3].commit()
    assert batch[0].post('/products/production/inspections',json=body(batch,recipe)).status_code in (404,409)


def test_multiple_physical_attempts_and_unqualified_recipe_refused(batch):
    recipe=make_recipe(batch,False)
    assert batch[0].post('/products/production/inspections',json=body(batch,recipe)).status_code==409
    qualified=make_recipe(batch)
    batch[3].execute(text("INSERT INTO print_jobs(printer_id,scheduled_job_id,status,started_at) VALUES(301,302,'failed',CURRENT_TIMESTAMP)"));batch[3].commit()
    assert batch[0].post('/products/production/inspections',json=body(batch,qualified)).status_code==409


def test_all_rejected_needs_no_square_mapping_or_stock_addition(batch,monkeypatch):
    recipe=make_recipe(batch)
    from modules.orders import square_store
    def unavailable(*args):raise AssertionError("Rejected-only inspection must not require Square configuration")
    monkeypatch.setattr(square_store,"load",unavailable)
    request=body(batch,recipe);request['outputs'][0].update(accepted=0,rejected=5);request['square_revision']=None
    response=batch[0].post('/products/production/inspections',json=request)
    assert response.status_code==200,response.text
    assert response.json()['stock_status']=='no_sellable_output'
    assert batch[3].execute(text('SELECT COUNT(*) FROM production_stock_additions')).scalar()==0


def test_flush_separate_inspection_and_combined_same_product_addition(batch):
    recipe=make_recipe(batch,outputs=[{'product_id':101,'kind':'main','expected':5},{'product_id':101,'kind':'flush','expected':2}])
    request=body(batch,recipe);request['outputs'].append({'product_id':101,'kind':'flush','accepted':1,'rejected':1})
    response=batch[0].post('/products/production/inspections',json=request)
    assert response.status_code==200,response.text
    state=batch[0].get('/products/production').json()
    assert len(response.json()['outputs'])==2
    from modules.orders.batch_production import additions
    saved=json.loads(batch[3].execute(select(additions.c.request_body)).scalar_one())
    assert saved['changes'][0]['adjustment']['quantity']=='5'


def test_tenant_and_role_guards_and_immutable_routes(batch):
    client,user,*_=batch
    recipe=make_recipe(batch)
    for role in ('viewer',):
        user['role']=role
        assert client.post('/products/production/recipes',json={}).status_code==403
        assert client.post('/products/production/inspections',json=body(batch,recipe)).status_code==403
    user['role']='admin';user['group_id']=2
    assert client.get('/products/production').json()['recipes']==[]
    assert client.post('/products/production/inspections',json=body(batch,recipe)).status_code in (403,404)
    user['group_id']=1;user['_auth_kind']='user_api_token'
    assert client.post('/products/production/inspections',json=body(batch,recipe)).status_code==403
    user['_auth_kind']='jwt'
    assert client.delete('/products/production/recipes/'+recipe['id']).status_code in (404,405)
    assert client.post('/products/production/deliver',json={}).status_code in (404,405)


@pytest.mark.parametrize('state,attempts', [('unknown',1), ('prepared',1), ('confirmed',1), ('withdrawn_baseline',0)])
def test_simulated_delivery_refuses_anything_but_first_prepared_send(batch,state,attempts):
    from modules.orders.stock_delivery import deliver
    from modules.orders.batch_production import additions
    from fastapi import HTTPException
    from sqlalchemy import update
    recipe=make_recipe(batch);batch[0].post('/products/production/inspections',json=body(batch,recipe))
    db=batch[3];row=dict(db.execute(select(additions)).mappings().one());seen=[]
    dest=json.loads(row['destination'])
    def transport(destination,raw):seen.append(raw);return True
    db.execute(update(additions).values(state=state,attempts=attempts));db.commit()
    with pytest.raises(HTTPException) as exc:
        deliver(db,1,row['id'],transport=transport,expected_destination=dest)
    assert exc.value.status_code==409
    assert seen==[]
    saved=db.execute(select(additions)).mappings().one()
    assert (saved['state'],saved['attempts'])==(state,attempts)


def test_simulated_delivery_timeout_never_resends(batch):
    from modules.orders.stock_delivery import deliver
    from modules.orders.batch_production import additions
    from fastapi import HTTPException
    recipe=make_recipe(batch);batch[0].post('/products/production/inspections',json=body(batch,recipe))
    db=batch[3];row=dict(db.execute(select(additions)).mappings().one());seen=[]
    def timeout(destination,raw):
        seen.append(raw)
        raise TimeoutError('private remote error must not escape')
    dest=json.loads(row['destination'])
    assert deliver(db,1,row['id'],transport=timeout,expected_destination=dest)=={'state':'unknown','attempts':1}
    with pytest.raises(HTTPException) as exc:
        deliver(db,1,row['id'],transport=timeout,expected_destination=dest)
    assert exc.value.status_code==409
    assert seen==[row['request_body']]


def test_tenant_deletion_retains_inspection_history(batch):
    from modules.orders.square_store import forget_tenant
    recipe=make_recipe(batch);batch[0].post('/products/production/inspections',json=body(batch,recipe))
    with pytest.raises(Exception):forget_tenant(batch[3],1)
    batch[3].rollback()
    assert batch[3].execute(text('SELECT COUNT(*) FROM production_acceptances')).scalar()==1


def test_assembly_bom_cannot_be_qualified_or_accepted_after_change(batch):
    recipe=make_recipe(batch)
    db=batch[3]
    db.execute(text("INSERT INTO product_components(product_id,model_id,quantity_needed) VALUES(101,301,2)"));db.commit()
    payload={'reference_job_id':301,'name':'Assembly','materials':'PLA','qualification_confirmed':True,'outputs':[{'product_id':101,'kind':'main','expected':5}]}
    assert batch[0].post('/products/production/recipes',json=payload).status_code==409
    assert batch[0].post('/products/production/inspections',json=body(batch,recipe)).status_code==409


def test_real_generic_bambu_names_required_not_education_names(batch):
    recipe=make_recipe(batch);db=batch[3]
    db.execute(text("UPDATE print_jobs SET job_name='odin-' || (SELECT claim_token FROM dispatch_attempt_receipts WHERE job_id=302) WHERE scheduled_job_id=302"));db.commit()
    assert batch[0].post('/products/production/inspections',json=body(batch,recipe)).status_code==409


def test_simultaneous_identical_inspections_create_one_receipt(batch):
    from concurrent.futures import ThreadPoolExecutor
    recipe=make_recipe(batch);request=body(batch,recipe)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses=list(pool.map(lambda _:batch[0].post('/products/production/inspections',json=request),range(2)))
    assert [r.status_code for r in responses]==[200,200]
    assert len({r.json()['id'] for r in responses})==1
    assert batch[3].execute(text('SELECT COUNT(*) FROM production_stock_additions')).scalar_one()==1


def test_product_deletion_is_blocked_after_recipe_revision(batch):
    make_recipe(batch)
    assert batch[0].delete('/products/101').status_code==409
    assert batch[3].execute(text('SELECT COUNT(*) FROM production_recipes')).scalar_one()==1


def test_education_owned_sources_never_enter_generic_stock_acceptance(batch):
    recipe=make_recipe(batch);db=batch[3]
    db.execute(text("UPDATE education_submissions SET model_id=301,job_id=302 WHERE id=14"));db.commit()
    assert batch[0].get('/products/production/jobs').json()['jobs']==[]
    assert batch[0].post('/products/production/inspections',json=body(batch,recipe)).status_code in (403,404,409)
    assert db.execute(text('SELECT COUNT(*) FROM production_acceptances')).scalar_one()==0


def test_separate_flush_product_with_own_model_keeps_its_own_square_destination(batch,monkeypatch):
    from modules.orders.square_client import SquareReader
    client,_,_,db,_,upstream,*_=batch
    original=SquareReader._read
    def read(self,method,path,**kwargs):
        if path=='/v2/catalog/object/V2':
            result=upstream(self,method,'/v2/catalog/object/V1',**kwargs)
            result['object']['id']='V2';result['object']['item_variation_data']['name']='Flush'
            result['related_objects'][0]['item_data']['variations']=[result['object']]
            return result
        return original(self,method,path,**kwargs)
    monkeypatch.setattr(SquareReader,'_read',read)
    db.execute(text("INSERT INTO models(id,name,org_id) VALUES(302,'Flush turtle',1)"))
    db.execute(text("INSERT INTO product_components(product_id,model_id,quantity_needed) VALUES(104,302,1)"));db.commit()
    mapped=client.put('/products/104/square',json={'revision':batch[-1]['revision'],'location_id':'L1','variation_id':'V2'})
    assert mapped.status_code==200,mapped.text
    batch[-1].update(mapped.json())
    recipe=make_recipe(batch,outputs=[{'product_id':101,'kind':'main','expected':5},{'product_id':104,'kind':'flush','expected':2}])
    request=body(batch,recipe);request['outputs'].append({'product_id':104,'kind':'flush','accepted':1,'rejected':1})
    response=client.post('/products/production/inspections',json=request)
    assert response.status_code==200,response.text
    from modules.orders.batch_production import additions
    changes=json.loads(db.execute(select(additions.c.request_body)).scalar_one())['changes']
    assert {(c['adjustment']['catalog_object_id'],c['adjustment']['quantity']) for c in changes}=={('V1','4'),('V2','1')}
    db.execute(text("INSERT INTO models(id,name,org_id) VALUES(303,'Changed flush design',1)"))
    db.execute(text("UPDATE product_components SET model_id=303 WHERE product_id=104"));db.commit()
    request['job_id']=301
    assert client.post('/products/production/inspections',json=request).status_code==409
    assert db.execute(text('SELECT COUNT(*) FROM production_acceptances')).scalar_one()==1


def test_missing_sender_is_real_retained_shape_and_claim_blocks_acceptance(batch):
    db = batch[3]
    assert db.execute(text('SELECT COUNT(*) FROM dispatch_sender_registrations')).scalar_one() == 0
    recipe = make_recipe(batch)
    row = db.execute(text('SELECT * FROM dispatch_attempt_receipts WHERE job_id=302')).mappings().one()
    db.execute(text("""INSERT INTO printer_dispatch_claims(printer_id,job_id,physical_key,configuration_key,payload_sha256,token,owner_org_id,state)
        VALUES(301,302,:key,:key,:payload,:token,1,'accepted')"""),
        {'key':row['configuration_key'],'payload':'a'*64,'token':row['claim_token']})
    db.commit()
    response = batch[0].post('/products/production/inspections', json=body(batch, recipe))
    assert response.status_code == 409 and 'retired' in response.json()['detail']
    assert db.execute(text('SELECT COUNT(*) FROM production_acceptances')).scalar_one() == 0


@pytest.mark.parametrize('mutation', [
    "UPDATE dispatch_attempt_receipts SET printer_id=999 WHERE job_id=302",
    "UPDATE dispatch_attempt_receipts SET observed_filename_key='ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff' WHERE job_id=302",
    "UPDATE print_jobs SET ended_at=NULL WHERE scheduled_job_id=302",
    "UPDATE print_jobs SET job_name='unknown' WHERE scheduled_job_id=302",
])
def test_sender_absence_does_not_relax_correlation(batch, mutation):
    recipe = make_recipe(batch)
    batch[3].execute(text(mutation)); batch[3].commit()
    assert batch[0].post('/products/production/inspections', json=body(batch, recipe)).status_code == 409


@pytest.mark.parametrize('state,token_match,sha_match,accepted', [
    ('quiesced',True,True,True), ('active',True,True,False),
    ('cleanup_uncertain',True,True,False), ('quiesced',False,True,False),
    ('quiesced',True,False,False),
])
def test_optional_sender_never_overrides_contradictory_retained_evidence(batch,state,token_match,sha_match,accepted):
    recipe = make_recipe(batch)
    db = batch[3]
    token = db.execute(text('SELECT claim_token FROM dispatch_attempt_receipts WHERE job_id=302')).scalar_one()
    db.execute(text("""INSERT INTO dispatch_sender_registrations(claim_token,printer_id,job_id,physical_key,configuration_key,payload_sha256,owner_org_id,origin_key,runtime_incarnation,registration_key,state,finished_at,cleanup_evidence_key)
        VALUES(:token,301,302,:key,:key,:payload,1,:nonce,:nonce,:nonce,:state,
          CASE WHEN :state='active' THEN NULL ELSE CURRENT_TIMESTAMP END,
          CASE WHEN :state='quiesced' THEN :key ELSE NULL END)"""),
        {'token':token if token_match else 'f'*32,'key':'b'*64,
         'payload':('a' if sha_match else 'c')*64,'nonce':uuid.uuid4().hex,'state':state})
    db.commit()
    assert batch[0].post('/products/production/inspections', json=body(batch,recipe)).status_code == (200 if accepted else 409)
