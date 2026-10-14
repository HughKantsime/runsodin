"""Actual cleanup boundary/zero/rollback acceptance with owned rows only."""
from datetime import datetime, timezone
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session
from tests.test_contracts.test_ip_allowlist_behavior import allowlist_app, owned_runtime, review_db

@pytest.fixture
def cleanup_rows(review_db, monkeypatch):
    from modules.system import routes_config
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None): return datetime(2026, 10, 12, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(routes_config, 'datetime', Clock)
    old='2026-09-11 12:00:00'; boundary='2026-09-12 12:00:00'; recent='2026-10-11 12:00:00'
    for offset, timestamp in enumerate([old,boundary,recent]):
        review_db.execute(text("INSERT INTO jobs(id,item_name,status,updated_at) VALUES(:id,'owned retention','completed',:ts)"),{'id':900+offset,'ts':timestamp})
        review_db.execute(text("INSERT INTO audit_logs(id,action,timestamp) VALUES(:id,'owned_retention',:ts)"),{'id':900+offset,'ts':timestamp})
        review_db.execute(text("INSERT INTO alerts(id,user_id,alert_type,severity,title,created_at) VALUES(:id,3,'print_complete','info','owned retention',:ts)"),{'id':900+offset,'ts':timestamp})
        review_db.execute(text("INSERT INTO timelapses(id,printer_id,filename,status,created_at) VALUES(:id,9,'owned-no-file.mp4','ready',:ts)"),{'id':900+offset,'ts':timestamp})
    review_db.execute(text("INSERT INTO jobs(id,item_name,status,updated_at) VALUES(910,'owned active retention','printing',:ts)"),{'ts':old})
    review_db.commit()
    return ['jobs','audit_logs','alerts','timelapses']

@pytest.mark.parametrize('days',[0,30])
def test_cleanup_all_categories_zero_and_strict_cutoff(allowlist_app,review_db,cleanup_rows,days):
    policy={key:days for key in ['completed_jobs_days','audit_logs_days','timelapses_days','alert_history_days']}
    with TestClient(allowlist_app,client=('198.51.100.7',50000)) as client:
        assert client.put('/api/config/retention',json=policy).status_code==200
        result=client.post('/api/admin/retention/cleanup')
        assert result.status_code==200
        payload=result.json()
        assert "timelapses" not in payload["deleted"]
        assert payload["refused"]==({} if days==0 else {"timelapses":{"status":"refused","reason":"lifetime_retirement_pending"}})
        assert payload["status"]==("ok" if days==0 else "partial")
    with Session(review_db.get_bind()) as db:
        for table in cleanup_rows:
            ids=set(db.execute(text(f'SELECT id FROM {table} WHERE id BETWEEN 900 AND 902')).scalars())
            assert ids==({900,901,902} if days==0 or table=="timelapses" else {901,902}), table
        assert db.execute(text('SELECT COUNT(*) FROM jobs WHERE id=910')).scalar_one()==1

@pytest.mark.parametrize('timestamp',['2026-09-12 12:00:00','2026-09-12 12:00:00.000000','2026-09-12T08:00:00-04:00'])
def test_equal_cutoff_representation_is_not_expired(allowlist_app,review_db,cleanup_rows,timestamp):
    for table,column in [('jobs','updated_at'),('audit_logs','timestamp'),('alerts','created_at'),('timelapses','created_at')]:
        review_db.execute(text(f'UPDATE {table} SET {column}=:ts WHERE id=901'),{'ts':timestamp})
    review_db.commit()
    policy={key:30 for key in ['completed_jobs_days','audit_logs_days','timelapses_days','alert_history_days']}
    with TestClient(allowlist_app,client=('198.51.100.7',50000)) as client:
        assert client.put('/api/config/retention',json=policy).status_code==200
        assert client.post('/api/admin/retention/cleanup').status_code==200
    with Session(review_db.get_bind()) as db:
        for table in cleanup_rows:
            assert db.execute(text(f'SELECT COUNT(*) FROM {table} WHERE id=901')).scalar_one()==1, table

@pytest.mark.parametrize('status',['capturing','encoding'])
def test_cleanup_does_not_delete_active_timelapse(allowlist_app,review_db,cleanup_rows,status):
    review_db.execute(text('UPDATE timelapses SET status=:status WHERE id=900'),{'status':status})
    review_db.commit()
    with TestClient(allowlist_app,client=('198.51.100.7',50000)) as client:
        assert client.put('/api/config/retention',json={'timelapses_days':30}).status_code==200
        assert client.post('/api/admin/retention/cleanup').status_code==200
    with Session(review_db.get_bind()) as db:
        assert db.execute(text('SELECT COUNT(*) FROM timelapses WHERE id=900')).scalar_one()==1

@pytest.mark.parametrize('table',['jobs','audit_logs','alerts','timelapses'])
def test_malformed_age_is_retained(allowlist_app,review_db,cleanup_rows,table):
    column='updated_at' if table=='jobs' else 'timestamp' if table=='audit_logs' else 'created_at'
    review_db.execute(text(f'UPDATE {table} SET {column}=:ts WHERE id=900'),{'ts':'unknown-age'})
    review_db.commit()
    policy={key:30 for key in ['completed_jobs_days','audit_logs_days','timelapses_days','alert_history_days']}
    with TestClient(allowlist_app,client=('198.51.100.7',50000)) as client:
        assert client.put('/api/config/retention',json=policy).status_code==200
        assert client.post('/api/admin/retention/cleanup').status_code==200
    with Session(review_db.get_bind()) as db:
        assert db.execute(text(f'SELECT COUNT(*) FROM {table} WHERE id=900')).scalar_one()==1

@pytest.mark.parametrize('failed_table',['jobs','audit_logs','alerts','token_blacklist','active_sessions','classroom_oauth_states'])
def test_cleanup_sql_fault_rolls_back_all_ttl_deletions(allowlist_app,review_db,cleanup_rows,failed_table):
    from sqlalchemy import event
    policy={key:30 for key in ['completed_jobs_days','audit_logs_days','timelapses_days','alert_history_days']}
    engine=review_db.get_bind()
    def reject(_connection,_cursor,statement,_parameters,_context,_executemany):
        if ' '.join(statement.split()).startswith('DELETE FROM '+failed_table+' '): raise RuntimeError('Owned cleanup write refused')
    with TestClient(allowlist_app,client=('198.51.100.7',50000),raise_server_exceptions=False) as client:
        assert client.put('/api/config/retention',json=policy).status_code==200
        event.listen(engine,'before_cursor_execute',reject)
        try: assert client.post('/api/admin/retention/cleanup').status_code==503
        finally: event.remove(engine,'before_cursor_execute',reject)
    with Session(engine) as db:
        for table in cleanup_rows:
            assert db.execute(text(f'SELECT COUNT(*) FROM {table} WHERE id BETWEEN 900 AND 902')).scalar_one()==3

@pytest.mark.parametrize('principal',[{'id':1,'role':'viewer','group_id':1},{'id':5,'role':'admin','group_id':2}])
def test_cleanup_requires_global_admin_before_deletion(allowlist_app,review_db,cleanup_rows,principal):
    from core.dependencies import get_current_user
    allowlist_app.dependency_overrides[get_current_user]=lambda:principal
    with TestClient(allowlist_app,client=('198.51.100.7',50000)) as client:
        assert client.post('/api/admin/retention/cleanup').status_code==403
    with Session(review_db.get_bind()) as db:
        for table in cleanup_rows:
            assert db.execute(text(f'SELECT COUNT(*) FROM {table} WHERE id BETWEEN 900 AND 902')).scalar_one()==3

@pytest.mark.parametrize('committed',[False,True])
def test_cleanup_commit_uncertainty_never_reports_success(allowlist_app,review_db,cleanup_rows,committed):
    from core.db import get_db
    class FaultSession(Session):
        def commit(self):
            if committed: super().commit()
            raise RuntimeError('Owned commit result unavailable')
    def database():
        with FaultSession(review_db.get_bind()) as db: yield db
    policy={key:30 for key in ['completed_jobs_days','audit_logs_days','timelapses_days','alert_history_days']}
    with TestClient(allowlist_app,client=('198.51.100.7',50000),raise_server_exceptions=False) as client:
        assert client.put('/api/config/retention',json=policy).status_code==200
        allowlist_app.dependency_overrides[get_db]=database
        response=client.post('/api/admin/retention/cleanup')
        assert response.status_code==503
        assert response.json()['detail']=='Retention cleanup could not be confirmed'
    with Session(review_db.get_bind()) as db:
        for table in cleanup_rows:
            ids=set(db.execute(text(f'SELECT id FROM {table} WHERE id BETWEEN 900 AND 902')).scalars())
            assert ids==({901,902} if committed and table!="timelapses" else {900,901,902}), table

@pytest.mark.parametrize('fault',[False,True])
def test_cleanup_multiple_batches_preserve_atomicity(allowlist_app,review_db,fault):
    from sqlalchemy import event
    with Session(review_db.get_bind()) as db:
        db.execute(text("INSERT INTO jobs(id,item_name,status,updated_at) VALUES(:id,'owned batch cleanup','completed','2000-01-01')"),[{'id':2000+index} for index in range(600)])
        db.commit()
    engine=review_db.get_bind();deletes=[]
    def observe(_connection,_cursor,statement,_parameters,_context,_executemany):
        if ' '.join(statement.split()).startswith('DELETE FROM jobs '):
            deletes.append(1)
            if fault and len(deletes)==2:raise RuntimeError('Owned middle batch failure')
    with TestClient(allowlist_app,client=('198.51.100.7',50000),raise_server_exceptions=False) as client:
        assert client.put('/api/config/retention',json={'completed_jobs_days':30}).status_code==200
        event.listen(engine,'before_cursor_execute',observe)
        try:response=client.post('/api/admin/retention/cleanup')
        finally:event.remove(engine,'before_cursor_execute',observe)
        assert response.status_code==(503 if fault else 200)
        if not fault:assert response.json()['deleted']['completed_jobs']==600
    with Session(engine) as db:
        assert db.execute(text('SELECT COUNT(*) FROM jobs WHERE id BETWEEN 2000 AND 2599')).scalar_one()==(600 if fault else 0)
    assert len(deletes)==(2 if fault else 3)
