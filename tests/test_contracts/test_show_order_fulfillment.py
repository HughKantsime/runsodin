"""Synthetic existing-order intake: no real payments, customer records or shipments."""
import copy,json
from datetime import datetime,timedelta,timezone
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import text,select
from tests.test_contracts.test_product_bom_integrity import bom_client
from tests.test_contracts.test_education_review_workflow import review_db

@pytest.fixture
def show(bom_client,review_db,monkeypatch):
    from core.dependencies import get_current_user
    from modules.orders.routes.show_orders import router
    monkeypatch.setenv('ENCRYPTION_KEY',Fernet.generate_key().decode())
    user={'id':3,'role':'admin','group_id':1,'_auth_kind':'jwt','is_active':True}
    bom_client.app.dependency_overrides[get_current_user]=lambda:user
    bom_client.app.include_router(router)
    # Static path must precede the generic order_id route, as in the real router.
    bom_client.app.router.routes.sort(key=lambda r:0 if getattr(r,'path','')=='/orders/show-commitments' else 1)
    created=bom_client.post('/orders',json={'order_number':'SHOW-TEST','platform':'show_ship_later','customer_name':'Synthetic buyer',
        'revenue':4500/100,'shipping_charged':500/100,'items':[{'product_id':101,'quantity':2,'unit_price':20}]})
    assert created.status_code==200,created.text
    return bom_client,user,created.json()['id'],review_db


def data():
    return {'revision':None,'source':'Synthetic show','address':{'recipient':'Test Buyer','line1':'123 Example Lane','line2':'','city':'Example','region':'TN','postal_code':'00000','country':'US'},
        'address_confirmed':True,'dispatch_by':(datetime.now(timezone.utc)+timedelta(days=7)).date().isoformat(),
        'currency':'USD','amount_received_minor':4500,'payment_method':'square_external','payment_reference':'SYNTHETIC-RECEIPT',
        'payment_received_at':(datetime.now(timezone.utc)-timedelta(minutes=5)).isoformat(),'payment_confirmed':True}


def save(show,payload=None):
    response=show[0].put(f'/orders/{show[2]}/show-fulfillment',json=payload or data())
    assert response.status_code==200,response.text
    return response.json()


def handoff(revision):
    return {'revision':revision,'carrier':'Synthetic carrier','tracking_number':'SYNTHETIC-TRACK',
        'handed_at':(datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat(),'physically_handed_over':True}


def produced(show):
    # Existing explicitly confirmed production, not fabricated job completion.
    item=show[0].get(f'/orders/{show[2]}').json()['items'][0]
    r=show[0].post(f'/orders/{show[2]}/items/{item["id"]}/confirm-production',json={'quantity':2})
    assert r.status_code==200,r.text


def test_existing_order_encrypted_manual_paid_commitment_no_external_action(show):
    answer=save(show);assert answer['state']=='paid_committed' and answer['can_dispatch'] is False
    assert answer['payment_verification']=='manual operator attestation only'
    raw=show[3].execute(text("SELECT value FROM system_config WHERE key LIKE 'show_fulfillment:%'")).scalar_one()
    assert 'Example Lane' not in raw and 'SYNTHETIC-RECEIPT' not in raw
    assert show[0].get(f'/orders/{show[2]}').json()['has_show_fulfillment'] is True
    summary=show[0].get('/orders/show-commitments').json();assert summary['orders'][0]['order_id']==show[2]
    assert 'Example Lane' not in json.dumps(summary) and 'SYNTHETIC-RECEIPT' not in json.dumps(summary)
    assert summary['capacity_verified'] is False


@pytest.mark.parametrize('received,confirmed,state',[(0,False,'payment_unconfirmed'),(1000,False,'partial_payment'),(4500,False,'payment_unconfirmed')])
def test_partial_or_unverified_receipts_are_not_paid(show,received,confirmed,state):
    payload=data();payload.update(amount_received_minor=received,payment_confirmed=confirmed)
    assert save(show,payload)['state']==state


@pytest.mark.parametrize('change',[{'amount_received_minor':1000},{'amount_received_minor':4600},{'payment_reference':''},{'payment_received_at':None}])
def test_full_paid_confirmation_requires_full_known_receipt(show,change):
    payload=data();payload.update(change)
    assert show[0].put(f'/orders/{show[2]}/show-fulfillment',json=payload).status_code==409


def test_unconfirmed_address_blocks_ready_dispatch(show):
    payload=data();payload['address_confirmed']=False;save(show,payload);produced(show)
    latest=show[0].get(f'/orders/{show[2]}/show-fulfillment').json()
    assert latest['state']=='address_unconfirmed' and not latest['can_dispatch']
    assert show[0].post(f'/orders/{show[2]}/show-fulfillment/dispatch',json=handoff(latest['revision'])).status_code==409


def test_handoff_requires_production_fresh_confirmation_and_actual_time(show):
    saved=save(show)
    url=f'/orders/{show[2]}/show-fulfillment/dispatch'
    assert show[0].post(url,json=handoff(saved['revision'])).status_code==409
    produced(show)
    payload=handoff(saved['revision']);payload['physically_handed_over']=False
    assert show[0].post(url,json=payload).status_code==409
    payload=handoff(saved['revision']);payload['handed_at']=(datetime.now(timezone.utc)+timedelta(days=1)).isoformat()
    assert show[0].post(url,json=payload).status_code==409
    r=show[0].post(url,json=handoff(saved['revision']));assert r.status_code==200,r.text
    assert r.json()['state']=='dispatched'
    assert show[0].get(f'/orders/{show[2]}').json()['status']=='shipped'
    assert show[0].post(url,json=handoff(r.json()['revision'])).status_code==409


def test_legacy_shipping_patch_delete_and_dispatched_reopen_bypasses_refused(show):
    saved=save(show);url=f'/orders/{show[2]}'
    assert show[0].patch(url+'/ship',json={'tracking_number':'label-only'}).status_code==409
    for fields in ({'status':'shipped'},{'shipped_date':'2026-10-20T12:00:00Z'},{'tracking_number':'label-only'}):assert show[0].patch(url,json=fields).status_code==409
    assert show[0].delete(url).status_code==409
    produced(show);assert show[0].post(url+'/show-fulfillment/dispatch',json=handoff(saved['revision'])).status_code==200
    for fields in ({'status':'pending'},{'status':'cancelled'},{'revenue':99},{'customer_name':'rewrite'}):assert show[0].patch(url,json=fields).status_code==409
    item=show[0].get(url).json()['items'][0]
    assert show[0].patch(f'{url}/items/{item["id"]}',json={'quantity':3}).status_code==409
    assert show[0].patch(f'{url}/items/{item["id"]}',json={'unit_price':21}).status_code==409
    assert show[0].post(url+'/items',json={'product_id':101,'quantity':1,'unit_price':20}).status_code==409


def test_quote_or_items_change_requires_reconfirmation_not_silent_paid_status(show):
    saved=save(show)
    assert show[0].patch(f'/orders/{show[2]}',json={'revenue':55}).status_code==200
    latest=show[0].get(f'/orders/{show[2]}/show-fulfillment').json();assert latest['state']=='quote_changed' and latest['needs_review']
    payload=data();payload.update(revision=saved['revision'],amount_received_minor=5500,correction_note='Additional payment confirmed after changed quote')
    assert save(show,payload)['state']=='paid_committed'


def test_cancel_keeps_payment_for_refund_review_without_claiming_refund(show):
    save(show);assert show[0].patch(f'/orders/{show[2]}',json={'status':'cancelled'}).status_code==200
    saved=show[0].get(f'/orders/{show[2]}/show-fulfillment').json();assert saved['state']=='cancelled_payment_review' and saved['details']['amount_received_minor']==4500


def test_stale_revision_refused_and_saved_details_retained(show):
    saved=save(show);payload=data();payload['source']='Changed';assert show[0].put(f'/orders/{show[2]}/show-fulfillment',json=payload).status_code==409
    payload['revision']=saved['revision'];payload['payment_received_at']=saved['details']['payment_received_at'];assert save(show,payload)['details']['source']=='Changed'


def test_private_no_store_validation_omits_inputs_and_context(show):
    payload=data();payload['address']['country']='private_invalid_country';payload['amount_received_minor']='private invalid amount'
    response=show[0].put(f'/orders/{show[2]}/show-fulfillment',json=payload)
    assert response.status_code==422 and 'private_invalid' not in response.text and 'Example Lane' not in response.text
    assert response.headers['cache-control']=='private, no-store'
    saved=show[0].get(f'/orders/{show[2]}/show-fulfillment');assert saved.headers['cache-control']=='private, no-store'


@pytest.mark.parametrize('role,kind,group,status',[('viewer','jwt',1,403),('operator','user_api_token',1,403),('admin','jwt',2,404),('admin','jwt',None,404)])
def test_exact_tenant_role_and_api_credential_guards(show,role,kind,group,status):
    show[1].update(role=role,_auth_kind=kind,group_id=group)
    response=show[0].get(f'/orders/{show[2]}/show-fulfillment');assert response.status_code==status
    assert response.headers['cache-control']=='private, no-store'


def test_missing_encryption_and_legacy_identity_fail_closed(show,monkeypatch):
    from modules.orders import square_store
    monkeypatch.setattr(square_store,'cipher',lambda:(_ for _ in ()).throw(__import__('fastapi').HTTPException(503,'Encryption unavailable')))
    assert show[0].put(f'/orders/{show[2]}/show-fulfillment',json=data()).status_code==503


def test_receipt_correction_retains_original_private_evidence(show):
    saved=save(show);payload={**saved['details'],'revision':saved['revision'],'amount_received_minor':4000,'payment_confirmed':False}
    assert show[0].put(f'/orders/{show[2]}/show-fulfillment',json=payload).status_code==409
    payload['correction_note']='Entered original amount incorrectly; retained for review'
    changed=save(show,payload);assert changed['state']=='partial_payment' and changed['payment_history'][0]['amount_received_minor']==4500
    assert changed['payment_history'][0]['payment_reference']=='SYNTHETIC-RECEIPT'


def test_future_or_naive_payment_time_refused(show):
    for value in ((datetime.now(timezone.utc)+timedelta(days=1)).isoformat(),'2026-10-20T12:00:00'):
        payload=data();payload['payment_received_at']=value
        assert show[0].put(f'/orders/{show[2]}/show-fulfillment',json=payload).status_code==409


def test_order_item_edit_invalidates_paid_binding(show):
    save(show);order=show[0].get(f'/orders/{show[2]}').json();item=order['items'][0]
    assert show[0].patch(f'/orders/{show[2]}/items/{item["id"]}',json={'quantity':3}).status_code==200
    assert show[0].get(f'/orders/{show[2]}/show-fulfillment').json()['state']=='quote_changed'


def test_payment_and_address_details_are_absent_from_audit_and_diagnostics_source(show):
    save(show)
    audit=' '.join(str(r) for r in show[3].execute(text('SELECT details FROM audit_logs')).all())
    assert 'Example Lane' not in audit and 'SYNTHETIC-RECEIPT' not in audit
    from pathlib import Path
    source=(Path(__file__).resolve().parents[2]/'backend/modules/system/routes_admin.py').read_text()
    assert '"show_fulfillment:"' in source


def test_operator_cannot_override_existing_edit_permissions(show):
    show[1]['role']='operator'
    response=show[0].put(f'/orders/{show[2]}/show-fulfillment',json=data())
    assert response.status_code==403 and response.headers['cache-control']=='private, no-store'


def test_tenant_deletion_preserves_received_payment_history(show):
    from modules.orders.square_store import forget_tenant
    from fastapi import HTTPException
    save(show)
    with pytest.raises(HTTPException) as error:
        forget_tenant(show[3],1)
    assert error.value.status_code==409
    assert show[0].get(f'/orders/{show[2]}/show-fulfillment').json()['state']=='paid_committed'


def test_actual_support_zip_excludes_private_sidecar(show,monkeypatch):
    import io,zipfile
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from core.db import get_db
    from core.dependencies import get_current_user
    from modules.system import diagnostics,routes_admin
    save(show)
    raw=show[3].execute(text("SELECT value FROM system_config WHERE key LIKE 'show_fulfillment:%'")).scalar_one()
    monkeypatch.setattr(diagnostics,'_read_tail',lambda path,n:[])
    app=FastAPI();app.include_router(routes_admin.router)
    app.dependency_overrides[get_db]=lambda:show[3]
    app.dependency_overrides[get_current_user]=lambda:{'id':3,'role':'admin','group_id':None,'is_active':True}
    response=TestClient(app).get('/admin/support-bundle')
    assert response.status_code==200
    with zipfile.ZipFile(io.BytesIO(response.content)) as bundle:
        everything=''.join(bundle.read(name).decode() for name in bundle.namelist())
    for private in (raw,'show_fulfillment:','123 Example Lane','SYNTHETIC-RECEIPT'):
        assert private not in everything
