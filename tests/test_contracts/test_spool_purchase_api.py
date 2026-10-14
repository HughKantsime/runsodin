"""Actual owner API acceptance, enabled after coordinated025 registration/wiring."""
import importlib
import pytest
from sqlalchemy import text
from tests.test_contracts.test_economic_creator_wiring import creators,review_db
M=importlib.import_module('core.schema.migrations.025_spool_purchase_evidence')

@pytest.fixture
def purchase_api(creators,review_db):
    review_db.commit()
    with review_db.get_bind().begin() as c:
        c.exec_driver_sql('BEGIN IMMEDIATE');M.apply(c)
    return creators

def basis(**changes):return {'currency':'USD','price':'000.000','net_weight_g':'001000.00',**changes}
def create(client,headers,**changes):
    response=client.post('/api/spools',headers=headers,json={'filament_id':80,**changes})
    status=response.status_code;assert status==200
    return response.json()['id']
def read(client,headers,id):
    response=client.get(f'/api/spools/{id}/purchase-evidence',headers=headers)
    status=response.status_code;assert status==200
    return response.json()

def test_actual_create_record_replace_clear_and_legacy_invalidation(purchase_api,review_db):
    client,auth,_,_=purchase_api;headers=auth()
    id=create(client,headers,purchase_evidence=basis())
    receipt=read(client,headers,id)
    assert (receipt['state'],receipt['revision'],receipt['price_decimal'],receipt['net_weight_g_decimal'])==('recorded',1,'0','1000')
    response=client.patch(f'/api/spools/{id}',headers=headers,json={'price':9})
    status=response.status_code;assert status==200
    assert read(client,headers,id)=={'state':'unknown','revision':2,'can_write':True}
    response=client.patch(f'/api/spools/{id}',headers=headers,json={'purchase_evidence':basis(currency='EUR',price='12.3400'),'expected_purchase_revision':2,'price':12.34})
    status=response.status_code;assert status==200
    receipt=read(client,headers,id);assert (receipt['revision'],receipt['currency'],receipt['price_decimal'])==(3,'EUR','12.34')
    response=client.patch(f'/api/spools/{id}',headers=headers,json={'purchase_evidence':None,'expected_purchase_revision':3})
    status=response.status_code;assert status==200
    assert read(client,headers,id)=={'state':'unknown','revision':4,'can_write':True}
    assert review_db.execute(text('SELECT count(*) FROM spool_purchase_evidence')).scalar_one()==2

def test_omitted_purchase_defaults_remain_unknown_and_cas_replay_refuses(purchase_api):
    client,auth,_,_=purchase_api;headers=auth();id=create(client,headers,price=20)
    assert read(client,headers,id)=={'state':'unknown','revision':0,'can_write':True}
    for expected,status in [(0,200),(0,409)]:
        response=client.patch(f'/api/spools/{id}',headers=headers,json={'purchase_evidence':basis(),'expected_purchase_revision':expected})
        actual=response.status_code;assert actual==status
    assert read(client,headers,id)['revision']==1

@pytest.mark.parametrize('invalid',[True,0,1.5,'1e3',' 1','-1','9'*19])
def test_invalid_explicit_strings_refuse_whole_spool_creation(purchase_api,review_db,invalid):
    client,auth,_,_=purchase_api
    before=review_db.execute(text('SELECT count(*) FROM spools')).scalar_one();review_db.commit()
    response=client.post('/api/spools',headers=auth(),json={'filament_id':80,'purchase_evidence':basis(price=invalid)})
    status=response.status_code;assert status==422
    assert review_db.execute(text('SELECT count(*) FROM spools')).scalar_one()==before

def test_read_token_no_write_and_unassigned_no_adoption(purchase_api,review_db):
    client,auth,token,_=purchase_api;id=create(client,auth(),purchase_evidence=basis())
    headers=token(['read:spools']);receipt=read(client,headers,id);assert receipt['can_write'] is False
    response=client.patch(f'/api/spools/{id}',headers=headers,json={'purchase_evidence':None,'expected_purchase_revision':1})
    status=response.status_code;assert status==403
    before=review_db.execute(text('SELECT count(*) FROM spools')).scalar_one();review_db.commit()
    response=client.post('/api/spools',headers=auth('unassigned'),json={'filament_id':80,'purchase_evidence':basis()})
    status=response.status_code;assert status==409
    assert review_db.execute(text('SELECT count(*) FROM spools')).scalar_one()==before

def test_private_projection_owner_foreign_shared_global_and_generic_cache(purchase_api):
    client,auth,_,_=purchase_api
    own=create(client,auth(),purchase_evidence=basis(price='12.34'))
    response=client.get(f'/api/spools/{own}/purchase-evidence',headers=auth('student'))
    status=response.status_code;assert status==200
    assert response.json()['price_decimal']=='12.34' and response.json()['can_write'] is False
    response=client.get(f'/api/spools/{own}/purchase-evidence',headers=auth('foreign'))
    status=response.status_code;assert status==404
    shared=create(client,auth('platform'),price=20,purchase_evidence=basis(price='20'))
    assert read(client,auth(),shared)=={'state':'unavailable','can_write':False}
    response=client.patch(f'/api/spools/{shared}',headers=auth(),json={'notes':'shared operational edit','price':20})
    status=response.status_code;assert status==200
    response=client.patch(f'/api/spools/{shared}',headers=auth(),json={'notes':'should rollback','price':21})
    status=response.status_code;assert status==404
    response=client.patch(f'/api/spools/{shared}',headers=auth(),json={'purchase_evidence':basis(),'expected_purchase_revision':1})
    status=response.status_code;assert status==404
    assert read(client,auth('platform'),shared)['revision']==1
    for path in ['/api/spools',f'/api/spools/{own}',f'/api/spools/{shared}']:
        response=client.get(path,headers=auth());status=response.status_code;assert status==200
        rows=response.json() if isinstance(response.json(),list) else [response.json()]
        assert all('purchase_evidence' not in row and 'purchase_revision' not in row and 'evidence_nonce' not in row for row in rows)
    response=client.get(f'/api/spools/{shared}',headers=auth('platform'))
    status=response.status_code;assert status==200
    assert response.json()['notes']=='shared operational edit'

@pytest.mark.parametrize('body',[{'purchase_evidence':basis()}, {'expected_purchase_revision':0}, {'expected_purchase_revision':None}, {'purchase_evidence':None,'expected_purchase_revision':True}, {'purchase_evidence':None,'expected_purchase_revision':None}])
def test_purchase_operation_requires_strict_complete_cas_pair(purchase_api,body):
    client,auth,_,_=purchase_api;id=create(client,auth())
    response=client.patch(f'/api/spools/{id}',headers=auth(),json=body)
    status=response.status_code;assert status==422
    assert read(client,auth(),id)['revision']==0

def test_caller_audit_failure_rolls_back_spool_registry_history_and_purchase(purchase_api,review_db,monkeypatch):
    client,auth,_,_=purchase_api
    tables=['spools','cost_source_lifetimes','cost_identities','spool_purchase_evidence','audit_logs']
    before={name:review_db.execute(text('SELECT count(*) FROM '+name)).scalar_one() for name in tables};review_db.commit()
    def refusal(*args,**kwargs):raise RuntimeError('Owned purchase audit refusal')
    monkeypatch.setattr('modules.inventory.routes.spools.log_audit',refusal)
    with pytest.raises(RuntimeError,match='Owned purchase audit refusal'):
        client.post('/api/spools',headers=auth(),json={'filament_id':80,'purchase_evidence':basis()})
    assert {name:review_db.execute(text('SELECT count(*) FROM '+name)).scalar_one() for name in tables}==before
