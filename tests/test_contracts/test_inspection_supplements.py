"""Missed pieces are a separate immutable inspection of the same physical job."""
import json

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from modules.orders import batch_production as service, inspection_supplements, square_delivery_client as wire
from tests.test_contracts.test_batch_acceptance import batch, body, make_recipe
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_replenishment_list import floor
from tests.test_contracts.test_square_readonly_workflow import shop
from tests.test_contracts.test_square_store_concurrency import config_engine, pg_url
from tests.test_contracts.test_production_operator_token import authenticate
from tests.test_contracts.test_square_delivery import enable, counts, http
from tests.test_contracts.test_square_stock_release import baseline

RECIPES = '/products/production/recipes'
INSPECTIONS = '/products/production/inspections'
FLUSH = {'product_id':101, 'kind':'flush', 'expected':3}
COUNT = {'product_id':101, 'kind':'flush', 'accepted':2, 'rejected':1}


def extension(parent):
    original = [{k:o[k] for k in ('product_id','kind','expected')} for o in parent['outputs']]
    return {'extends_recipe_id':parent['id'], 'reference_job_id':301, 'name':'Mixed plate',
            'materials':'PLA', 'qualification_confirmed':True, 'outputs':original + [FLUSH.copy()]}


def post(client, path, data):
    response = client.post(path, json=data)
    assert response.status_code == 200, response.text
    return response.json()


def inspected(batch):
    parent = make_recipe(batch)
    first = post(batch[0], INSPECTIONS, body(batch, parent))
    return parent, first


def supplement(batch, child, first):
    return {**body(batch, child), 'supplement_of':first['id'], 'not_already_added_confirmed':True,
            'outputs':[COUNT.copy()], 'counted_by':'Avery', 'note':'Counted the extra pieces'}


@pytest.mark.parametrize('caller', ['session','token'])
def test_missed_pieces_full_path_and_immutable_retry(batch, caller, monkeypatch):
    from core.dependencies import get_current_user
    from tests.test_contracts.test_api_token_delegation_behavior import interactive_headers
    client, _, _, db, app, *_ = batch
    enable(batch)
    batch[-1].update(client.get('/products/square').json())
    if caller == 'token':
        authenticate(batch, scopes=('write:production','read'))
    else:
        app.dependency_overrides.pop(get_current_user)
        client.cookies.set('session', interactive_headers()['Authorization'].split()[1])
    parent, first = inspected(batch)
    original_bytes = db.execute(select(service.receipts)).one()
    child = post(client, RECIPES, extension(parent))
    assert child['extends_recipe_id'] == parent['id']
    request = supplement(batch, child, first)
    second = post(client, INSPECTIONS, request)
    assert second['id'] != first['id'] and second['supplement_of'] == first['id']
    assert second['outputs'][0]['kind'] == 'flush' and second['job_uid'] == first['job_uid']
    assert post(client, INSPECTIONS, {**request,'note':'Changed'}) == second
    assert post(client, INSPECTIONS, body(batch, parent)) == first
    for change in ({'outputs':[{**COUNT,'accepted':3,'rejected':0}]}, {'supplement_of':second['id']}):
        assert client.post(INSPECTIONS, json={**request,**change}).status_code == 409
    assert db.execute(select(service.receipts)).one() == original_bytes
    history = client.get('/products/production/history').json()
    assert history['receipts'] == [second, {**first,'has_supplement':True}]
    assert sorted(r['id'] for r in client.get('/products/production').json()['receipts']) == sorted([first['id'],second['id']])
    assert len(history['additions']) == 2
    row = db.execute(select(service.additions).where(service.additions.c.receipt_id==second['id'])).mappings().one()
    assert row['state'] == 'prepared'
    saved = json.loads(row['request_body'])
    assert saved == {'idempotency_key':second['id'],'changes':[{'type':'ADJUSTMENT','adjustment':{
        'catalog_object_id':'V1','from_location_id':'L1','to_location_id':'L1','from_state':'NONE',
        'to_state':'IN_STOCK','quantity':'2','occurred_at':second['occurred_at'],'reference_id':second['id']}}]}
    monkeypatch.setattr(wire,'LIVE_WRITES_ENABLED',True)
    _, transport = http(monkeypatch, counts(row['request_body']))
    url = f"/products/batches/additions/{row['id']}/send"
    assert client.post(url).json()['state'] == client.post(url).json()['state'] == 'confirmed'
    assert transport.request.call_count == 1 and transport.request.call_args.kwargs['data'] == row['request_body'].encode()


@pytest.mark.parametrize('reason',['owner','draft','reference','payload','removed','changed','unchanged','added_main','duplicate'])
def test_extension_refusals(batch, reason):
    parent = make_recipe(batch,qualified=reason!='draft')
    data = extension(parent)
    if reason == 'reference': data['reference_job_id'] = 302
    elif reason == 'owner':
        batch[3].execute(update(service.recipes).values(owner=2)); batch[3].commit()
    elif reason == 'payload':
        from sqlalchemy import text
        batch[3].execute(text('UPDATE jobs SET payload_sha256=:hash WHERE id=301'),{'hash':'c'*64}); batch[3].commit()
    elif reason == 'removed': data['outputs'].pop(0)
    elif reason == 'changed': data['outputs'][0]['expected'] += 1
    elif reason == 'unchanged': data['outputs'].pop()
    elif reason == 'added_main': data['outputs'][1].update(product_id=104,kind='main')
    elif reason == 'duplicate': data['outputs'].append(FLUSH)
    authenticate(batch)
    assert batch[0].post(RECIPES,json=data).status_code == (404 if reason=='owner' else 409)
    assert len(batch[3].execute(select(service.recipes)).all()) == 1


@pytest.mark.parametrize('reason',['wrong_owner','wrong_job','outside','not_extension','draft','overlap','missing','duplicate',
                                    'quantity','unconfirmed','component','wrong_parent'])
def test_supplement_refusals(batch,reason):
    from tests.test_contracts.test_finishing_workflow import component
    parent, first = inspected(batch)
    recipe_data = extension(parent)
    recipe_data['qualification_confirmed'] = reason != 'draft'
    if reason == 'missing':
        recipe_data['outputs'].append({**FLUSH,'product_id':104})
    child = post(batch[0], RECIPES, recipe_data)
    data = supplement(batch,child,first)
    if reason == 'wrong_owner':
        batch[3].execute(update(service.receipts).values(owner=2)); batch[3].commit()
    elif reason == 'wrong_job': data['job_id'] = 301
    elif reason == 'outside': data['print_job_id'] = data.pop('job_id')
    elif reason == 'not_extension': data['recipe_id'] = parent['id']
    elif reason == 'overlap': data['outputs'].append(body(batch,parent)['outputs'][0])
    elif reason == 'duplicate': data['outputs'].append(COUNT)
    elif reason == 'quantity': data['outputs'][0]['accepted'] = 5
    elif reason == 'unconfirmed': data.pop('not_already_added_confirmed')
    elif reason == 'component':
        assert component(batch,job=301).status_code == 200
        data['job_id'] = 301
    elif reason == 'wrong_parent': data['supplement_of'] = 'f'*32
    answer = batch[0].post(INSPECTIONS,json=data)
    assert answer.status_code == 409
    if reason == 'outside': assert 'print_job_id' in answer.json()['detail']
    if reason == 'unconfirmed': assert 'Square by hand' in answer.json()['detail']
    assert len(batch[3].execute(select(service.all_receipts)).all()) == 1


def test_supplement_uses_job_end_for_baseline(batch):
    parent, first = inspected(batch)
    child = post(batch[0], RECIPES, extension(parent))
    assert baseline(batch).status_code == 200
    data = supplement(batch,child,first)
    assert batch[0].post(INSPECTIONS,json=data).status_code == 409
    data['outputs'][0]['baseline_disposition'] = 'in_baseline'
    second = post(batch[0],INSPECTIONS,data)
    assert second['stock_status'] == 'in_baseline' and second['outputs'][0]['send_quantity'] == 0
    assert batch[0].get('/products/production/history',params={'state':'in_baseline'}).json()['receipts'] == [second]
    assert len(batch[3].execute(select(service.additions)).all()) == 1


def test_released_storage_enforces_both_unique_slots(config_engine):
    from sqlalchemy.orm import Session
    # Use the released table definitions directly, before any new application writes.
    with config_engine.begin() as conn:
        service.schema.apply(conn)
    row = dict(id='a'*32, owner=1, job_uid='job', claim_token='b'*32, recipe_id='c'*32, request_hash='d'*64, definition='{}')
    with Session(config_engine) as db:
        db.execute(service.receipts.insert().values(**row))
        db.commit()
        with pytest.raises(IntegrityError):
            db.execute(service.receipts.insert().values(**{**row,'id':'e'*32,'claim_token':'f'*32}))
        db.rollback()
        inspection_supplements.insert(db,row['id'],'e'*32,'f'*32,'0'*64,'{}')
        db.commit()
        with pytest.raises(IntegrityError):
            inspection_supplements.insert(db,row['id'],'1'*32,'2'*32,'3'*64,'{}')
        db.rollback()
        assert db.execute(select(service.all_receipts.c.supplement_of).where(service.all_receipts.c.id=='e'*32)).scalar_one() == row['id']
        assert db.execute(select(service.receipts)).mappings().one() == row


def test_planning_counts_flush_pieces_and_original_job_once(floor):
    from tests.test_contracts.test_replenishment_list import numbers, row
    batch = floor['batch']
    numbers(floor)
    parent, first = inspected(batch)
    child = post(batch[0], RECIPES, extension(parent))
    post(batch[0], INSPECTIONS, supplement(batch,child,first))
    assert row(floor)['on_the_way'] == {'pieces':11, 'job_ids':[301], 'awaiting_stock':6}


@pytest.mark.parametrize('same_count', [True, False])
def test_concurrent_supplements_share_one_slot(batch, same_count):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    parent, first = inspected(batch)
    child = post(batch[0], RECIPES, extension(parent))
    data = supplement(batch,child,first)
    other = {**data, 'note':'Independent recount'}
    if not same_count:
        other['outputs'] = [{**COUNT,'accepted':1,'rejected':2}]
    barrier = Barrier(2)
    def record(payload):
        barrier.wait(timeout=10)
        return batch[0].post(INSPECTIONS,json=payload)
    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(pool.map(record,[data,other]))
    assert sorted(r.status_code for r in replies) == ([200,200] if same_count else [200,409])
    if same_count:
        assert replies[0].json() == replies[1].json()
    assert len(batch[3].execute(select(service.all_receipts)).all()) == 2
    assert len(batch[3].execute(select(service.additions)).all()) == 2


def test_recipe_without_dispatched_reference_cannot_offer_an_extension(batch):
    parent, first = inspected(batch)
    saved = {k:v for k,v in parent.items() if k != 'id'}
    saved.update(reference_job_id=None, reference_claim_token=None, qualified_from_outside_print=True)
    batch[3].execute(update(service.recipes).where(service.recipes.c.id==parent['id']).values(definition=service.packed(saved)))
    batch[3].commit()
    row, = batch[0].get('/products/production/history').json()['receipts']
    assert row['id'] == first['id'] and row['can_supplement'] is False
    assert batch[0].post(RECIPES,json=extension(parent)).status_code == 409
