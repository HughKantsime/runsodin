"""Retention may not bypass protected Education/production history."""
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session
from tests.test_contracts.test_retention_cleanup_behavior import allowlist_app, owned_runtime, review_db
from tests.test_contracts.test_order_production_behavior import production_order, bom_client

def test_terminal_education_history_survives_cleanup(allowlist_app,review_db):
    review_db.execute(text("UPDATE jobs SET status='completed',updated_at='2000-01-01' WHERE id=13"))
    review_db.commit()
    with TestClient(allowlist_app,client=('198.51.100.7',50000),raise_server_exceptions=False) as client:
        assert client.put('/api/config/retention',json={'completed_jobs_days':30}).status_code==200
        assert client.post('/api/admin/retention/cleanup').status_code==200
    with Session(review_db.get_bind()) as db:
        assert db.execute(text('SELECT COUNT(*) FROM jobs WHERE id=13')).scalar_one()==1
        assert db.execute(text('SELECT job_id FROM education_submissions WHERE id=14')).scalar_one()==13

def test_order_linked_completed_jobs_survive_cleanup(allowlist_app,review_db,production_order):
    producer,identity,order_id=production_order
    assert producer.post(f'/orders/{order_id}/schedule').status_code==200
    with Session(review_db.get_bind()) as db:
        jobs=list(db.execute(text('SELECT id FROM jobs WHERE order_item_id IN (SELECT id FROM order_items WHERE order_id=:id)'),{'id':order_id}).scalars())
        assert jobs
        for job in jobs:
            db.execute(text("UPDATE jobs SET status='completed',updated_at='2000-01-01' WHERE id=:id"),{'id':job})
        db.commit()
    with TestClient(allowlist_app,client=('198.51.100.7',50000),raise_server_exceptions=False) as client:
        assert client.put('/api/config/retention',json={'completed_jobs_days':30}).status_code==200
        assert client.post('/api/admin/retention/cleanup').status_code==200
    with Session(review_db.get_bind()) as db:
        remaining=set(db.execute(text('SELECT id FROM jobs')).scalars())
        assert set(jobs)<=remaining

import pytest

@pytest.mark.parametrize('status',['completed','failed','cancelled'])
@pytest.mark.parametrize('authority',['education','direct_order','registry_order'])
def test_each_protected_source_is_retained_while_unrelated_job_expires(allowlist_app,review_db,production_order,status,authority):
    producer,identity,order_id=production_order
    assert producer.post(f'/orders/{order_id}/schedule').status_code==200
    with Session(review_db.get_bind()) as db:
        order_state=db.execute(text('SELECT status FROM orders WHERE id=:id'),{'id':order_id}).scalar_one()
        jobs=list(db.execute(text('SELECT id FROM jobs WHERE order_item_id IN (SELECT id FROM order_items WHERE order_id=:id)'),{'id':order_id}).scalars())
        protected=13 if authority=='education' else jobs[0]
        if authority=='direct_order':
            db.execute(text('DELETE FROM order_production_jobs WHERE job_id=:id'),{'id':protected})
        elif authority=='registry_order':
            # Prospective economic history no longer permits severing this
            # source anchor. Verify refusal without weakening its live guard,
            # then prove cleanup preserves the protected source and registry.
            from sqlalchemy.exc import IntegrityError
            with pytest.raises(IntegrityError, match='economic source anchor differs'):
                with db.begin_nested():
                    db.execute(text('UPDATE jobs SET order_item_id=NULL WHERE id=:id'),{'id':protected})
            assert db.execute(text('SELECT order_item_id FROM jobs WHERE id=:id'),{'id':protected}).scalar_one() is not None
        db.execute(text("UPDATE jobs SET status=:status,updated_at='2000-01-01' WHERE id=:id"),{'status':status,'id':protected})
        db.execute(text("INSERT INTO jobs(id,item_name,status,updated_at) VALUES(950,'owned unlinked eligible','completed','2000-01-01')"))
        db.commit()
        registry_count=db.execute(text('SELECT COUNT(*) FROM order_production_jobs WHERE job_id=:id'),{'id':protected}).scalar_one()
        submission=db.execute(text('SELECT job_id,status FROM education_submissions WHERE id=14')).first()
    with TestClient(allowlist_app,client=('198.51.100.7',50000)) as client:
        assert client.put('/api/config/retention',json={'completed_jobs_days':30}).status_code==200
        response=client.post('/api/admin/retention/cleanup')
        assert response.status_code==200
        assert response.json()['deleted']['completed_jobs']==1
    with Session(review_db.get_bind()) as db:
        assert db.execute(text('SELECT COUNT(*) FROM jobs WHERE id=:id'),{'id':protected}).scalar_one()==1
        assert db.execute(text('SELECT COUNT(*) FROM jobs WHERE id=950')).scalar_one()==0
        assert db.execute(text('SELECT status FROM orders WHERE id=:id'),{'id':order_id}).scalar_one()==order_state
        assert db.execute(text('SELECT COUNT(*) FROM order_production_jobs WHERE job_id=:id'),{'id':protected}).scalar_one()==registry_count
        assert db.execute(text('SELECT job_id,status FROM education_submissions WHERE id=14')).first()==submission

@pytest.mark.parametrize('authority_table',['education_submissions','order_production_jobs'])
def test_missing_authority_table_aborts_without_unguarded_deletion(allowlist_app,review_db,authority_table):
    with Session(review_db.get_bind()) as db:
        db.execute(text("INSERT INTO jobs(id,item_name,status,updated_at) VALUES(950,'owned unknown authority','completed','2000-01-01')"))
        db.execute(text(f'DROP TABLE {authority_table}'))
        db.commit()
    with TestClient(allowlist_app,client=('198.51.100.7',50000),raise_server_exceptions=False) as client:
        assert client.put('/api/config/retention',json={'completed_jobs_days':30}).status_code==200
        assert client.post('/api/admin/retention/cleanup').status_code!=200
    with Session(review_db.get_bind()) as db:
        assert db.execute(text('SELECT COUNT(*) FROM jobs WHERE id=950')).scalar_one()==1
