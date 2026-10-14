"""Actual registry writer must not attach deleted or changed jobs."""
from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session
from tests.test_contracts.test_retention_protected_jobs_behavior import production_order, bom_client, review_db, owned_runtime

@pytest.mark.parametrize('changed',['deleted','order_changed'])
def test_production_binding_rechecks_actual_job_identity(production_order,review_db,changed):
    from modules.orders.production import OrderFulfillmentService
    client,identity,order_id=production_order
    assert client.post(f'/orders/{order_id}/schedule').status_code==200
    with Session(review_db.get_bind()) as db:
        row=db.execute(text('SELECT id,order_item_id,model_id,quantity_on_bed FROM jobs WHERE order_item_id IN (SELECT id FROM order_items WHERE order_id=:id) ORDER BY id LIMIT 1'),{'id':order_id}).first()
        binding=SimpleNamespace(id=row[0],order_item_id=row[1],model_id=row[2],quantity_on_bed=row[3])
        db.execute(text('DELETE FROM order_production_jobs WHERE job_id=:id'),{'id':binding.id})
        if changed=='deleted':db.execute(text('DELETE FROM jobs WHERE id=:id'),{'id':binding.id})
        else:
            from sqlalchemy.exc import IntegrityError
            with pytest.raises(IntegrityError,match='economic source anchor differs'):
                db.execute(text('UPDATE jobs SET order_item_id=NULL WHERE id=:id'),{'id':binding.id})
            assert db.execute(text('SELECT order_item_id FROM jobs WHERE id=:id'),{'id':binding.id}).scalar_one()==row[1]
            binding.order_item_id=None  # Untrusted stale writer metadata must not replace retained truth.
        db.commit()
        with pytest.raises(HTTPException) as refused:
            OrderFulfillmentService().bind_job(db,binding)
        assert refused.value.status_code==409
        assert db.execute(text('SELECT COUNT(*) FROM order_production_jobs WHERE job_id=:id'),{'id':binding.id}).scalar_one()==0
