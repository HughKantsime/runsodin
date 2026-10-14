"""Actual retention configuration validation/persistence, no scheduled cleanup."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session
from tests.test_contracts.test_ip_allowlist_behavior import allowlist_app,owned_runtime,review_db

@pytest.mark.parametrize('field',['completed_jobs_days','audit_logs_days','timelapses_days','alert_history_days'])
def test_explicit_null_retention_is_validation_refusal_without_write(allowlist_app,review_db,field):
    with TestClient(allowlist_app,client=('198.51.100.7',50000),raise_server_exceptions=False) as client:
        response=client.put('/api/config/retention',json={field:None})
        assert response.status_code in (400,422)
    with Session(review_db.get_bind()) as db:
        assert db.execute(text("SELECT COUNT(*) FROM system_config WHERE key='data_retention'")).scalar_one()==0


def test_retention_numeric_policy_persists_all_categories(allowlist_app):
    policy={'completed_jobs_days':30,'audit_logs_days':365,'timelapses_days':60,'alert_history_days':90}
    with TestClient(allowlist_app,client=('198.51.100.7',50000)) as client:
        response=client.put('/api/config/retention',json=policy)
        assert response.status_code==200
        assert response.json()==policy
        assert client.get('/api/config/retention').json()==policy


def test_negative_retention_refuses_without_write(allowlist_app,review_db):
    with TestClient(allowlist_app,client=('198.51.100.7',50000)) as client:
        assert client.put('/api/config/retention',json={'completed_jobs_days':-1}).status_code==400
    with Session(review_db.get_bind()) as db:
        assert db.execute(text("SELECT COUNT(*) FROM system_config WHERE key='data_retention'")).scalar_one()==0

@pytest.mark.parametrize('field',['completed_jobs_days','audit_logs_days','timelapses_days','alert_history_days'])
def test_invalid_update_preserves_existing_policy_and_audit(allowlist_app,review_db,field):
    policy={'completed_jobs_days':30,'audit_logs_days':365,'timelapses_days':60,'alert_history_days':90}
    with TestClient(allowlist_app,client=('198.51.100.7',50000),raise_server_exceptions=False) as client:
        assert client.put('/api/config/retention',json=policy).status_code==200
        with Session(review_db.get_bind()) as db:
            audit_count=db.execute(text('SELECT COUNT(*) FROM audit_logs')).scalar_one()
        assert client.put('/api/config/retention',json={**policy,'completed_jobs_days':40,field:None}).status_code==400
        assert client.get('/api/config/retention').json()==policy
        with Session(review_db.get_bind()) as db:
            assert db.execute(text('SELECT COUNT(*) FROM audit_logs')).scalar_one()==audit_count

def test_partial_and_empty_policy_preserve_existing_replacement_semantics(allowlist_app):
    from modules.system.routes_config import RETENTION_DEFAULTS
    with TestClient(allowlist_app,client=('198.51.100.7',50000)) as client:
        assert client.put('/api/config/retention',json={'completed_jobs_days':30}).json()=={**RETENTION_DEFAULTS,'completed_jobs_days':30}
        assert client.put('/api/config/retention',json={'audit_logs_days':90}).json()=={**RETENTION_DEFAULTS,'audit_logs_days':90}
        assert client.put('/api/config/retention',json={}).json()==RETENTION_DEFAULTS
