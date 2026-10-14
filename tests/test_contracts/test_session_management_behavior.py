"""Real JWT session listing/revocation using owned database and HTTP routes."""
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session
from tests.test_contracts.test_api_token_delegation_behavior import token_client
from tests.test_contracts.test_education_review_workflow import review_db


def tracked_session(db,user=3,device='Synthetic desktop'):
    from core.auth import create_access_token
    from core.session_tracking import record_session
    username={1:'student',3:'admin',4:'outsider',5:'foreign'}[user]
    token=create_access_token({'sub':username,'role':'admin' if user in (3,5) else 'viewer'})
    assert record_session(db,user,token,'192.0.2.1',device) is True
    from core.auth import decode_token
    import jwt
    from core import auth
    jti=jwt.decode(token,auth.SECRET_KEY,algorithms=[auth.ALGORITHM])['jti']
    session_id=db.execute(text('SELECT id FROM active_sessions WHERE token_jti=:jti'),{'jti':jti}).scalar_one()
    return {'Authorization':'Bearer '+token},session_id


def test_list_scoped_devices_and_current_identification(token_client,review_db):
    client,_=token_client
    current,first=tracked_session(review_db,device='Synthetic desktop')
    _,second=tracked_session(review_db,device='Synthetic phone')
    tracked_session(review_db,5,device='Foreign device')
    response=client.get('/sessions',headers=current)
    assert response.status_code==200
    rows=response.json()
    assert {r['id'] for r in rows}=={first,second}
    assert sum(r['is_current'] for r in rows)==1
    assert next(r for r in rows if r['id']==first)['is_current'] is True
    assert {r['user_agent'] for r in rows}=={'Synthetic desktop','Synthetic phone'}
    assert all('token_jti' not in r and 'access_token' not in r for r in rows)


def test_individual_revoke_immediately_denies_target_and_keeps_other(token_client,review_db):
    client,_=token_client
    current,_=tracked_session(review_db)
    target,session_id=tracked_session(review_db)
    assert client.delete('/sessions/'+str(session_id),headers=current).status_code==200
    assert client.get('/sessions',headers=target).status_code==401
    assert client.get('/sessions',headers=current).status_code==200
    with Session(review_db.get_bind()) as fresh:
        assert fresh.execute(text('SELECT COUNT(*) FROM active_sessions WHERE id=:id'),{'id':session_id}).scalar_one()==0
        assert fresh.execute(text('SELECT COUNT(*) FROM token_blacklist')).scalar_one()==1


def test_revoke_all_preserves_current_and_other_user(token_client,review_db):
    client,_=token_client
    current,first=tracked_session(review_db)
    target,_=tracked_session(review_db)
    foreign,foreign_id=tracked_session(review_db,5)
    response=client.delete('/sessions',headers=current)
    assert response.status_code==200 and response.json()['revoked']==1
    assert client.get('/sessions',headers=target).status_code==401
    assert client.get('/sessions',headers=current).status_code==200
    assert client.get('/sessions',headers=foreign).status_code==200
    with Session(review_db.get_bind()) as fresh:
        assert {r[0] for r in fresh.execute(text('SELECT id FROM active_sessions'))}=={first,foreign_id}


def test_foreign_session_revoke_cannot_cross_user_boundary(token_client,review_db):
    client,_=token_client
    current,_=tracked_session(review_db)
    foreign,session_id=tracked_session(review_db,5)
    assert client.delete('/sessions/'+str(session_id),headers=current).status_code==404
    assert client.get('/sessions',headers=foreign).status_code==200
    with Session(review_db.get_bind()) as fresh:
        assert fresh.execute(text('SELECT COUNT(*) FROM token_blacklist')).scalar_one()==0
