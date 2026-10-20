"""Optional manual quote binds to address/order, never a label or carrier check."""
from datetime import datetime,timedelta,timezone
import pytest
from tests.test_contracts.test_show_order_fulfillment import show,save,data,produced,handoff
from tests.test_contracts.test_product_bom_integrity import bom_client
from tests.test_contracts.test_education_review_workflow import review_db

def quote():
    now=datetime.now(timezone.utc)
    return {'length_mm':200,'width_mm':150,'height_mm':100,'weight_g':450,'carrier':'Synthetic carrier','service':'Ground','source':'Manual website quote','amount_minor':500,'quoted_at':(now-timedelta(minutes=5)).isoformat(),'valid_until':(now+timedelta(days=1)).isoformat(),'reviewed':True}
def put(show,revision,q=None):return show[0].put(f'/orders/{show[2]}/show-fulfillment/shipping',json={'revision':revision,'quote':q or quote()})

def test_manual_quote_preserved_but_address_change_requires_review(show):
    initial=save(show);r=put(show,initial['revision']);assert r.status_code==200,r.text
    assert r.json()['shipping']['state']=='manually_reviewed' and not r.json()['shipping']['carrier_verified']
    assert 'no-store' in r.headers['cache-control']
    assert put(show,initial['revision']).status_code==409
    payload=data();payload.update(revision=r.json()['revision'],payment_received_at=r.json()['details']['payment_received_at']);payload['address']['line1']='456 New Lane'
    changed=save(show,payload);assert changed['shipping']['state']=='needs_review'
    produced(show)
    dispatch=show[0].post(f'/orders/{show[2]}/show-fulfillment/dispatch',json=handoff(changed['revision']))
    assert dispatch.status_code==200,dispatch.text # Optional quote never silently adds a dispatch gate.
    assert put(show,dispatch.json()['revision']).status_code==409

@pytest.mark.parametrize('change',[{'weight_g':0},{'length_mm':True},{'quoted_at':'2026-10-20T12:00:00'},{'amount_minor':501}])
def test_invalid_or_inconsistent_shipping_preparation_refused(show,change):
    initial=save(show);q=quote();q.update(change);r=put(show,initial['revision'],q)
    assert r.status_code in (409,422)
    latest=show[0].get(f'/orders/{show[2]}/show-fulfillment').json();assert latest['revision']==initial['revision'] and latest['shipping']['state']=='not_prepared'

def test_expired_quote_draft_needs_review(show):
    initial=save(show);q=quote();now=datetime.now(timezone.utc);q.update(quoted_at=(now-timedelta(days=2)).isoformat(),valid_until=(now-timedelta(days=1)).isoformat(),reviewed=False)
    response=put(show,initial['revision'],q);assert response.status_code==200,response.text
    assert response.json()['shipping']['state']=='needs_review'
