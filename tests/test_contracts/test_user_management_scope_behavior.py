"""Real user-management mutations must respect the caller's scoped credential."""
import json
import secrets
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session
from tests.test_contracts.test_password_reset_behavior import reset_client,review_db,admin_headers,new_password
from tests.test_contracts.test_license_user_limits import signed_license


def scoped_headers(db,scopes):
    from core.auth import hash_password
    raw='odin_'+secrets.token_urlsafe(32)
    db.execute(text('INSERT INTO api_tokens(user_id,name,token_hash,token_prefix,scopes) VALUES(3,:name,:hash,:prefix,:scopes)'),{'name':'Synthetic user-management caller','hash':hash_password(raw),'prefix':raw[:10],'scopes':json.dumps(scopes)})
    db.commit()
    return {'X-API-Key':raw}


@pytest.mark.parametrize('scopes',[['read'],['write:jobs'],['agent:write']])
@pytest.mark.parametrize('operation',['role','delete'])
def test_non_admin_token_cannot_modify_accounts(reset_client,review_db,scopes,operation):
    client,_,_=reset_client;headers=scoped_headers(review_db,scopes)
    before=tuple(review_db.execute(text('SELECT id,role FROM users ORDER BY id')).all())
    if operation=='role':
        response=client.patch('/users/1',headers=headers,json={'role':'admin'})
    else:
        response=client.delete('/users/4',headers=headers)
    assert response.status_code==403
    with Session(review_db.get_bind()) as fresh:
        unchanged=tuple(fresh.execute(text('SELECT id,role FROM users ORDER BY id')).all())==before
        assert unchanged


def mutate(client,headers,operation,*,group=None):
    if operation=='create':
        body={'username':'Synthetic managed user','email':'managed@example.test','password':new_password(),'role':'viewer'}
        if group is not None:body['group_id']=group
        return client.post('/users',headers=headers,json=body)
    if operation=='import':
        content='username,email,password,role\nSynthetic imported user,imported@example.test,'+new_password()+',viewer\n'
        return client.post('/users/import',headers=headers,files={'file':('synthetic.csv',content,'text/csv')})
    if operation=='role':return client.patch('/users/1',headers=headers,json={'role':'admin'})
    return client.delete('/users/4',headers=headers)


def account_state(db):
    with Session(db.get_bind()) as fresh:
        return (tuple(fresh.execute(text('SELECT id,role,password_hash,is_active,group_id FROM users ORDER BY id')).all()),
                tuple(fresh.execute(text('SELECT id,token_jti FROM active_sessions ORDER BY id')).all()),
                fresh.execute(text('SELECT COUNT(*) FROM api_tokens')).scalar_one(),
                fresh.execute(text('SELECT COUNT(*) FROM audit_logs')).scalar_one())


@pytest.mark.parametrize('scopes',[['read'],['write'],['write:jobs'],['agent:write']])
@pytest.mark.parametrize('operation',['create','import','role','delete'])
def test_all_account_mutations_require_admin_token_scope(reset_client,review_db,signed_license,scopes,operation):
    signed_license(tier='pro',max_users=100,features=[])
    client,_,_=reset_client;headers=scoped_headers(review_db,scopes);before=account_state(review_db)
    response=mutate(client,headers,operation)
    assert response.status_code==403
    unchanged=account_state(review_db)==before
    assert unchanged


@pytest.mark.parametrize('credential',['interactive','admin_token'])
@pytest.mark.parametrize('operation',['create','import','role','delete'])
def test_authorized_account_mutations_remain_usable(reset_client,review_db,signed_license,credential,operation,monkeypatch):
    from modules.organizations import routes_users
    lm,_=signed_license(tier='pro',max_users=100,features=[])
    # Earlier license tests replace sys.modules; bind this real signed enforcement.
    monkeypatch.setattr(routes_users,'check_user_limit',lm.check_user_limit)
    client,_,_=reset_client
    headers=admin_headers() if credential=='interactive' else scoped_headers(review_db,['admin'])
    response=mutate(client,headers,operation)
    # Account creation mints a login (password), so it is credential material: an
    # interactive session only (security review HIGH 1). Role change and delete
    # alter an existing account's authorization, not its credentials, so an
    # admin-scoped token keeps doing them.
    if credential=='admin_token' and operation in ('create','import'):
        assert response.status_code==403
        assert response.json()['detail']=="API tokens cannot create or change credentials; sign in with a password session"
        with Session(review_db.get_bind()) as fresh:
            name='Synthetic managed user' if operation=='create' else 'Synthetic imported user'
            assert fresh.execute(text('SELECT COUNT(*) FROM users WHERE username=:n'),{'n':name}).scalar_one()==0
        return
    assert response.status_code==200
    with Session(review_db.get_bind()) as fresh:
        if operation in ('create','import'):
            username='Synthetic managed user' if operation=='create' else 'Synthetic imported user'
            row=fresh.execute(text('SELECT role,group_id FROM users WHERE username=:name'),{'name':username}).one()
            assert tuple(row)==('viewer',1)
        elif operation=='role':assert fresh.execute(text('SELECT role FROM users WHERE id=1')).scalar_one()=='admin'
        else:assert fresh.execute(text('SELECT COUNT(*) FROM users WHERE id=4')).scalar_one()==0


@pytest.mark.parametrize('operation',['create','role','delete'])
def test_grouped_admin_cannot_assign_or_mutate_foreign_accounts(reset_client,review_db,signed_license,operation):
    signed_license(tier='pro',max_users=100,features=[])
    client,_,_=reset_client;headers=scoped_headers(review_db,['admin']);before=account_state(review_db)
    if operation=='create':response=mutate(client,headers,operation,group=2)
    elif operation=='role':response=client.patch('/users/5',headers=headers,json={'role':'viewer'})
    else:response=client.delete('/users/5',headers=headers)
    assert response.status_code==403
    unchanged=account_state(review_db)==before
    assert unchanged
