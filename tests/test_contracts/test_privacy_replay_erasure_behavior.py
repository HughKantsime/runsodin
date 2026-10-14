"""Owned erasure must remove identity-bearing queued replay without losing unrelated facts."""
import json
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session
from tests.test_contracts.test_education_review_workflow import review_db

@pytest.fixture
def replay_erasure(review_db):
    from core.db import get_db
    from core.dependencies import get_current_user
    from modules.organizations.routes_sessions import router
    app=FastAPI();app.include_router(router)
    def database():
        with Session(review_db.get_bind()) as db:yield db
    app.dependency_overrides[get_db]=database
    app.dependency_overrides[get_current_user]=lambda:{'id':3,'role':'admin','is_active':True,'group_id':None}
    return TestClient(app,raise_server_exceptions=False)

def queue(db,payload):
    identifier=db.execute(text("INSERT INTO ws_events(event_type,data,created_at) VALUES('owned_privacy',:data,1) RETURNING id"),{'data':payload}).scalar_one()
    db.commit();return identifier

@pytest.mark.parametrize('identity',['ordinary-synthetic','Quoted "synthetic" \\ identity','Unicode synthetic é東京'])
def test_erasure_removes_identity_bearing_replay_preserving_unrelated_rows(replay_erasure,review_db,identity):
    review_db.execute(text('UPDATE users SET username=:identity,email=:email WHERE id=1'),{'identity':identity,'email':'erase-owned@example.invalid'});review_db.commit()
    affected=queue(review_db,json.dumps({'type':'alert','data':{'message':identity+' submitted a print job'},'_audience':{'user_ids':[2,3]}}))
    email_event=queue(review_db,json.dumps({'type':'alert','data':{'message':'erase-owned@example.invalid submitted a print job'}}))
    untouched_payload=json.dumps({'type':'printer','data':{'state':'idle','printer_id':9}})
    untouched=queue(review_db,untouched_payload)
    response=replay_erasure.delete('/users/1/erase')
    assert response.status_code==200
    with Session(review_db.get_bind()) as db:
        assert db.execute(text('SELECT COUNT(*) FROM ws_events WHERE id IN (:one,:two)'),{'one':affected,'two':email_event}).scalar_one()==0
        preserved=db.execute(text('SELECT data FROM ws_events WHERE id=:id'),{'id':untouched}).scalar_one()==untouched_payload
        assert preserved


def test_replay_delete_failure_rolls_back_account_and_queue(replay_erasure,review_db,monkeypatch):
    identity=review_db.execute(text('SELECT username FROM users WHERE id=1')).scalar_one()
    queued=queue(review_db,json.dumps({'data':{'message':identity+' submitted a print job'}}))
    original=Session.execute
    def fail(self,statement,*args,**kwargs):
        if str(statement).startswith('DELETE FROM ws_events'):raise RuntimeError('synthetic replay erasure write failure')
        return original(self,statement,*args,**kwargs)
    monkeypatch.setattr(Session,'execute',fail)
    response=replay_erasure.delete('/users/1/erase')
    assert response.status_code>=400
    with Session(review_db.get_bind()) as db:
        user=db.execute(text('SELECT username,is_active FROM users WHERE id=1')).one()
        unchanged=user.username==identity and bool(user.is_active)
        assert unchanged
        assert db.execute(text('SELECT COUNT(*) FROM ws_events WHERE id=:id'),{'id':queued}).scalar_one()==1
        assert db.execute(text("SELECT COUNT(*) FROM audit_logs WHERE action='gdpr_erasure'")).scalar_one()==0

@pytest.mark.parametrize('budget',['MAX_ROWS','MAX_PAYLOAD_BYTES','MAX_TOTAL_BYTES','MAX_NODES','MAX_DEPTH'])
def test_replay_budget_failure_rolls_back_without_partial_erasure(replay_erasure,review_db,monkeypatch,budget):
    from core import privacy_replay
    monkeypatch.setattr(privacy_replay,budget,1)
    queued=queue(review_db,json.dumps({'data':{'actor_user_id':1,'message':'owned'}}))
    queue(review_db,json.dumps({'data':{'state':'idle'}}))
    response=replay_erasure.delete('/users/1/erase')
    assert response.status_code==503
    with Session(review_db.get_bind()) as db:
        assert bool(db.execute(text('SELECT is_active FROM users WHERE id=1')).scalar_one())
        assert db.execute(text('SELECT COUNT(*) FROM ws_events WHERE id=:id'),{'id':queued}).scalar_one()==1

def test_common_name_keeps_unrelated_text_and_explicit_foreign_author(replay_erasure,review_db):
    review_db.execute(text("UPDATE users SET username='Ann' WHERE id=1"));review_db.commit()
    unrelated=json.dumps({'data':{'message':'Ann printer is idle','name':'Ann'}})
    foreign=json.dumps({'data':{'actor_user_id':2,'message':'Ann submitted a print job'}})
    one=queue(review_db,unrelated);two=queue(review_db,foreign)
    affected=queue(review_db,json.dumps({'data':{'message':'Ann submitted a print job'}}))
    assert replay_erasure.delete('/users/1/erase').status_code==200
    with Session(review_db.get_bind()) as db:
        preserved=db.execute(text('SELECT data FROM ws_events WHERE id=:id'),{'id':one}).scalar_one()==unrelated
        foreign_preserved=db.execute(text('SELECT data FROM ws_events WHERE id=:id'),{'id':two}).scalar_one()==foreign
        assert preserved and foreign_preserved
        assert db.execute(text('SELECT COUNT(*) FROM ws_events WHERE id=:id'),{'id':affected}).scalar_one()==0

def test_malformed_matching_email_is_removed_but_unrelated_malformed_row_remains(replay_erasure,review_db):
    review_db.execute(text("UPDATE users SET email='erase-owned@example.invalid' WHERE id=1"));review_db.commit()
    one=queue(review_db,'{"message":"erase-owned@example.invalid"')
    two=queue(review_db,'{"state":')
    assert replay_erasure.delete('/users/1/erase').status_code==200
    with Session(review_db.get_bind()) as db:
        assert db.execute(text('SELECT COUNT(*) FROM ws_events WHERE id=:id'),{'id':one}).scalar_one()==0
        assert db.execute(text('SELECT COUNT(*) FROM ws_events WHERE id=:id'),{'id':two}).scalar_one()==1

@pytest.mark.parametrize('kind',['deep','oversized'])
def test_default_limits_refuse_large_or_deep_replay_truthfully(replay_erasure,review_db,kind):
    from core import privacy_replay
    if kind=='oversized':payload=json.dumps({'data':'x'*(privacy_replay.MAX_PAYLOAD_BYTES+1)})
    else:
        payload={'data':'owned'}
        for _ in range(privacy_replay.MAX_DEPTH+2):payload={'nested':payload}
        payload=json.dumps(payload)
    queued=queue(review_db,payload)
    assert replay_erasure.delete('/users/1/erase').status_code==503
    with Session(review_db.get_bind()) as db:
        assert bool(db.execute(text('SELECT is_active FROM users WHERE id=1')).scalar_one())
        assert db.execute(text('SELECT COUNT(*) FROM ws_events WHERE id=:id'),{'id':queued}).scalar_one()==1

def test_denied_erasure_never_inspects_replay(replay_erasure,review_db,monkeypatch):
    from core.dependencies import get_current_user
    from core import privacy_replay
    replay_erasure.app.dependency_overrides[get_current_user]=lambda:{'id':1,'role':'viewer','is_active':True,'group_id':1}
    def prohibited(*args,**kwargs):pytest.fail('Denied erasure inspected replay')
    monkeypatch.setattr(privacy_replay,'erase_identifying_replay',prohibited)
    assert replay_erasure.delete('/users/1/erase').status_code==403

@pytest.mark.parametrize('failure',['audit','commit','committed_then_error'])
def test_final_erasure_uncertainty_never_reports_success(replay_erasure,review_db,monkeypatch,failure):
    from modules.organizations import routes_sessions
    queued=queue(review_db,json.dumps({'data':{'actor_user_id':1}}))
    if failure=='audit':
        def audit(*args,**kwargs):raise RuntimeError('synthetic privacy audit fault')
        monkeypatch.setattr(routes_sessions,'log_audit',audit)
    else:
        original=Session.commit
        def commit(self):
            if failure=='committed_then_error':original(self)
            raise RuntimeError('synthetic privacy commit uncertainty')
        monkeypatch.setattr(Session,'commit',commit)
    response=replay_erasure.delete('/users/1/erase')
    assert response.status_code>=400
    with Session(review_db.get_bind()) as db:
        committed=failure=='committed_then_error'
        assert bool(db.execute(text('SELECT is_active FROM users WHERE id=1')).scalar_one()) is (not committed)
        assert db.execute(text('SELECT COUNT(*) FROM ws_events WHERE id=:id'),{'id':queued}).scalar_one()==int(not committed)
