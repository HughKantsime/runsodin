"""All delivery paths share the inspection gate while holding the order lock."""
import pytest
from sqlalchemy import text
from tests.test_contracts.test_show_order_fulfillment import show, save, produced, handoff
from tests.test_contracts.test_product_bom_integrity import bom_client
from tests.test_contracts.test_education_review_workflow import review_db
from core import traceability as service


def require_inspection(show):
    client, user, oid, db = show
    item = client.get(f'/orders/{oid}').json()['items'][0]['id']
    job = db.execute(text("INSERT INTO jobs(item_name,status,order_item_id,charged_to_org_id) VALUES('QA part','completed',:item,1) RETURNING id"), {'item':item}).scalar_one()
    db.execute(text("INSERT INTO traceability_policies(owner_id,org_id,target_kind,target_key,required) VALUES(1,1,'product','101',1)"))
    db.commit()
    return job


@pytest.mark.parametrize('path', ['patch', 'ship', 'handoff'])
def test_every_shipping_path_refuses_then_passes_inspection(show, path):
    client, user, oid, db = show
    if path == 'handoff':
        saved = save(show)
    produced(show)
    jid = require_inspection(show)
    def ship():
        if path == 'patch':
            return client.patch(f'/orders/{oid}', json={'status':'shipped'})
        if path == 'ship':
            return client.patch(f'/orders/{oid}/ship', json={'tracking_number':'E4-TEST'})
        return client.post(f'/orders/{oid}/show-fulfillment/dispatch', json=handoff(saved['revision']))
    refused = ship()
    assert refused.status_code == 409 and 'inspection_required' in refused.text, refused.text
    db.rollback()
    assert db.execute(text('SELECT status FROM orders WHERE id=:id'), {'id':oid}).scalar() == 'fulfilled'
    job = service.one(db, 'SELECT * FROM jobs WHERE id=:id', id=jid)
    retained = service.record(db, job)
    db.execute(text("UPDATE traceability_records SET inspection_result='pass',inspected_by=3 WHERE job_id=:id"), {'id':jid})
    db.commit()
    shipped = ship()
    assert shipped.status_code == 200, shipped.text
    db.expire_all()
    assert db.execute(text('SELECT delivered_at FROM traceability_records WHERE job_id=:id'), {'id':jid}).scalar()
    from core.traceability_delivery import assert_deletable
    from fastapi import HTTPException
    with pytest.raises(HTTPException, match='Delivered traceability'):
        assert_deletable(db, jid)


@pytest.fixture(autouse=True)
def traceability_license(monkeypatch):
    from unittest.mock import Mock
    license = Mock()
    license.has_feature.return_value = True
    monkeypatch.setattr('license_manager.get_license', lambda:license)
    return license


@pytest.mark.parametrize('path', ['patch', 'ship', 'handoff'])
@pytest.mark.parametrize('inspection', [None, 'fail'])
def test_unlicensed_shipping_skips_traceability(show, traceability_license, path, inspection):
    client, user, oid, db = show
    saved = save(show) if path == 'handoff' else None
    produced(show)
    jid = require_inspection(show)
    job = service.one(db, 'SELECT * FROM jobs WHERE id=:id', id=jid)
    service.record(db, job)
    db.execute(text('UPDATE traceability_records SET inspection_result=:result WHERE job_id=:id'), {'result':inspection,'id':jid})
    db.commit()
    traceability_license.has_feature.return_value = False
    if path == 'patch':
        response = client.patch(f'/orders/{oid}', json={'status':'shipped'})
    elif path == 'ship':
        response = client.patch(f'/orders/{oid}/ship', json={'tracking_number':'SYNTHETIC'})
    else:
        response = client.post(f'/orders/{oid}/show-fulfillment/dispatch', json=handoff(saved['revision']))
    assert response.status_code == 200, response.text
    db.expire_all()
    assert db.execute(text('SELECT delivered_at FROM traceability_records WHERE job_id=:id'), {'id':jid}).scalar() is None


@pytest.mark.parametrize('path', ['patch', 'ship', 'handoff'])
def test_registry_only_job_blocks_shipping_until_passed_despite_binding_drift(show, path):
    client, user, oid, db = show
    saved = save(show) if path == 'handoff' else None
    produced(show)
    jid = require_inspection(show)
    job = service.one(db, 'SELECT * FROM jobs WHERE id=:id', id=jid)
    service.record(db, job)
    item = job['order_item_id']
    # Legacy registry binding remains authoritative when mutable direct linkage
    # has gone away. No modern economic anchor is weakened to create this case.
    db.execute(text('INSERT INTO order_production_components(order_item_id,model_id,pieces_per_unit) VALUES(:item,101,1)'), {'item':item})
    db.execute(text('INSERT INTO order_production_jobs(job_id,order_item_id,model_id,piece_count) VALUES(:job,:item,101,1)'), {'job':jid,'item':item})
    db.execute(text('UPDATE jobs SET order_item_id=NULL,payload_sha256=:hash WHERE id=:id'), {'id':jid,'hash':'d'*64})
    db.commit()
    def ship():
        if path == 'patch':
            return client.patch(f'/orders/{oid}', json={'status':'shipped'})
        if path == 'ship':
            return client.patch(f'/orders/{oid}/ship', json={'tracking_number':'SYNTHETIC'})
        return client.post(f'/orders/{oid}/show-fulfillment/dispatch', json=handoff(saved['revision']))
    response = ship()
    assert response.status_code == 409 and 'inspection_required' in response.text, response.text
    db.rollback()
    db.execute(text("UPDATE traceability_records SET inspection_result='pass' WHERE job_id=:id"), {'id':jid})
    db.commit()
    response = ship()
    assert response.status_code == 200, response.text
    assert db.execute(text('SELECT delivered_at FROM traceability_records WHERE job_id=:id'), {'id':jid}).scalar()
