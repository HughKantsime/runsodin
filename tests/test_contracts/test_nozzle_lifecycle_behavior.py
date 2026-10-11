"""Nozzle lifecycle actual HTTP/DB permissions and atomic mutation checks."""
import pytest
from sqlalchemy import text
from tests.test_contracts.test_api_token_delegation_behavior import token_client,review_db

@pytest.fixture
def nozzle_client(token_client,review_db):
    from modules.printers import routes_nozzle
    client,issue=token_client;client.app.include_router(routes_nozzle.router)
    review_db.execute(text("INSERT INTO printers(id,name,model,machine_type,is_active,api_type,org_id,shared) VALUES(199,'Synthetic foreign nozzle','Synthetic','Synthetic',1,'bambu',2,0)"))
    review_db.execute(text("INSERT INTO nozzle_lifecycle(id,printer_id,nozzle_type,nozzle_diameter,notes,print_hours_accumulated,print_count) VALUES(199,199,'hardened_steel',0.4,'Synthetic foreign install',12,3)"))
    review_db.commit()
    return client,issue

@pytest.mark.parametrize('method,path,payload',[
    ('GET','/printers/199/nozzle',None),('GET','/printers/199/nozzle/history',None),
    ('POST','/printers/199/nozzle',{'nozzle_type':'brass','nozzle_diameter':0.6}),
    ('PATCH','/printers/199/nozzle/199/retire',None),
])
def test_foreign_nozzle_access_does_not_read_or_mutate(nozzle_client,review_db,method,path,payload):
    client,issue=nozzle_client
    before=review_db.execute(text('SELECT id,removed_at FROM nozzle_lifecycle ORDER BY id')).all()
    response=client.request(method,path,headers=issue(['read','write']),json=payload)
    assert response.status_code==404
    after=review_db.execute(text('SELECT id,removed_at FROM nozzle_lifecycle ORDER BY id')).all()
    assert after==before

@pytest.mark.parametrize('method,path,payload',[
    ('GET','/printers/199/nozzle',None),('GET','/printers/199/nozzle/history',None),
    ('POST','/printers/199/nozzle',{'nozzle_type':'brass','nozzle_diameter':0.6}),
    ('PATCH','/printers/199/nozzle/199/retire',None),
])
def test_unrelated_scope_cannot_read_or_mutate_nozzles(nozzle_client,review_db,method,path,payload):
    client,issue=nozzle_client
    review_db.execute(text('UPDATE printers SET org_id=1 WHERE id=199'));review_db.commit()
    before=review_db.execute(text('SELECT id,removed_at FROM nozzle_lifecycle ORDER BY id')).all()
    response=client.request(method,path,headers=issue(['write:jobs']),json=payload)
    assert response.status_code==403
    assert review_db.execute(text('SELECT id,removed_at FROM nozzle_lifecycle ORDER BY id')).all()==before


def test_install_and_retire_preserve_complete_history(nozzle_client,review_db):
    client,issue=nozzle_client
    review_db.execute(text("UPDATE users SET role='operator' WHERE id=3"))
    review_db.execute(text('UPDATE printers SET org_id=1 WHERE id=199'));review_db.commit()
    headers=issue(['read:printers','write:printers'])
    created=client.post('/printers/199/nozzle',headers=headers,json={'nozzle_type':'brass','nozzle_diameter':0.6,'notes':'Synthetic new nozzle'})
    assert created.status_code==200
    identifier=created.json()['id']
    assert identifier!=199
    previous=review_db.execute(text('SELECT removed_at,print_hours_accumulated,print_count FROM nozzle_lifecycle WHERE id=199')).one()
    assert previous[0] is not None and previous[1:]==(12,3)
    current=client.get('/printers/199/nozzle',headers=headers).json()
    assert current['id']==identifier and current['nozzle_diameter']==0.6
    retired=client.patch(f'/printers/199/nozzle/{identifier}/retire',headers=headers)
    assert retired.status_code==200 and retired.json()['removed_at'] is not None
    timestamp=retired.json()['removed_at']
    assert client.patch(f'/printers/199/nozzle/{identifier}/retire',headers=headers).status_code==400
    assert client.get('/printers/199/nozzle',headers=headers).json() is None
    history=client.get('/printers/199/nozzle/history',headers=headers).json()
    assert {r['id'] for r in history}=={199,identifier}
    assert next(r for r in history if r['id']==identifier)['removed_at']==timestamp


def test_duplicate_active_nozzles_fail_closed_without_repair(nozzle_client,review_db):
    client,issue=nozzle_client
    review_db.execute(text('UPDATE printers SET org_id=1 WHERE id=199'))
    review_db.execute(text("INSERT INTO nozzle_lifecycle(id,printer_id,nozzle_type,nozzle_diameter) VALUES(299,199,'brass',0.6)"));review_db.commit()
    before=review_db.execute(text('SELECT id,removed_at FROM nozzle_lifecycle ORDER BY id')).all()
    response=client.post('/printers/199/nozzle',headers=issue(['write:printers']),json={'nozzle_type':'brass','nozzle_diameter':0.8})
    assert response.status_code==409
    assert review_db.execute(text('SELECT id,removed_at FROM nozzle_lifecycle ORDER BY id')).all()==before


def test_nozzle_history_has_stable_order_for_equal_install_timestamps(nozzle_client,review_db):
    client,issue=nozzle_client
    review_db.execute(text('UPDATE printers SET org_id=1 WHERE id=199'))
    review_db.execute(text("UPDATE nozzle_lifecycle SET installed_at='2026-10-10 12:00:00',removed_at='2026-10-10 12:01:00' WHERE id=199"))
    review_db.execute(text("INSERT INTO nozzle_lifecycle(id,printer_id,nozzle_type,nozzle_diameter,installed_at,print_hours_accumulated,print_count) VALUES(299,199,'brass',0.6,'2026-10-10 12:00:00',0,0)"));review_db.commit()
    history=client.get('/printers/199/nozzle/history',headers=issue(['read:printers'])).json()
    assert [r['id'] for r in history]==[299,199]

@pytest.mark.parametrize('method,path', [('GET','/printers/199/nozzle'),('GET','/printers/199/nozzle/history')])
@pytest.mark.parametrize('role,user_org,printer_org,shared,expected',[
 ('viewer',1,1,False,200),('viewer',1,2,True,200),('viewer',1,2,False,404),
 ('operator',None,None,False,200),('operator',None,2,False,404),('admin',1,2,False,404),('admin',None,2,False,200),
])
def test_nozzle_read_resource_visibility(nozzle_client,review_db,method,path,role,user_org,printer_org,shared,expected):
    client,issue=nozzle_client
    review_db.execute(text('UPDATE users SET role=:role,group_id=:org WHERE id=3'),{'role':role,'org':user_org})
    review_db.execute(text('UPDATE printers SET org_id=:org,shared=:shared WHERE id=199'),{'org':printer_org,'shared':shared});review_db.commit()
    assert client.request(method,path,headers=issue(['read:printers'])).status_code==expected

@pytest.mark.parametrize('action',['install','retire'])
@pytest.mark.parametrize('role,org,shared,scope,expected',[
 ('operator',1,False,'write:printers',200),('viewer',1,False,'write:printers',403),
 ('admin',2,True,'write:printers',404),('operator',1,False,'read:printers',403),
])
def test_nozzle_mutation_policy_and_denied_state(nozzle_client,review_db,action,role,org,shared,scope,expected):
    client,issue=nozzle_client
    review_db.execute(text('UPDATE users SET role=:role WHERE id=3'),{'role':role})
    review_db.execute(text('UPDATE printers SET org_id=:org,shared=:shared WHERE id=199'),{'org':org,'shared':shared});review_db.commit()
    before=review_db.execute(text('SELECT id,removed_at FROM nozzle_lifecycle ORDER BY id')).all()
    headers=issue([scope])
    response=(client.post('/printers/199/nozzle',headers=headers,json={'nozzle_type':'brass','nozzle_diameter':0.6}) if action=='install' else client.patch('/printers/199/nozzle/199/retire',headers=headers))
    assert response.status_code==expected
    if expected!=200:assert review_db.execute(text('SELECT id,removed_at FROM nozzle_lifecycle ORDER BY id')).all()==before

@pytest.mark.parametrize('payload',[
 {'nozzle_diameter':0},{'nozzle_diameter':-0.4},{'nozzle_diameter':'NaN'},
 {'nozzle_diameter':'Infinity'},{'nozzle_diameter':True},{'nozzle_type':'x'*21},
])
def test_invalid_nozzle_install_preserves_existing_active_record(nozzle_client,review_db,payload):
    client,issue=nozzle_client
    review_db.execute(text('UPDATE printers SET org_id=1 WHERE id=199'));review_db.commit()
    before=review_db.execute(text('SELECT id,removed_at FROM nozzle_lifecycle ORDER BY id')).all()
    response=client.post('/printers/199/nozzle',headers=issue(['write:printers']),json=payload)
    assert response.status_code==422
    assert review_db.execute(text('SELECT id,removed_at FROM nozzle_lifecycle ORDER BY id')).all()==before

@pytest.mark.parametrize('method,path,payload',[
 ('GET','/printers/999999/nozzle',None),('GET','/printers/999999/nozzle/history',None),
 ('POST','/printers/999999/nozzle',{'nozzle_diameter':0.4}),('PATCH','/printers/999999/nozzle/199/retire',None),
])
def test_missing_nozzle_parent_returns_not_found(nozzle_client,method,path,payload):
    client,issue=nozzle_client
    assert client.request(method,path,headers=issue(['read:printers','write:printers']),json=payload).status_code==404


def test_wrong_parent_retirement_does_not_change_nozzle(nozzle_client,review_db):
    client,issue=nozzle_client
    before=review_db.execute(text('SELECT removed_at FROM nozzle_lifecycle WHERE id=199')).scalar_one()
    assert client.patch('/printers/9/nozzle/199/retire',headers=issue(['write:printers'])).status_code==404
    assert review_db.execute(text('SELECT removed_at FROM nozzle_lifecycle WHERE id=199')).scalar_one()==before


def test_duplicate_current_nozzles_are_not_silently_selected(nozzle_client,review_db):
    client,issue=nozzle_client
    review_db.execute(text('UPDATE printers SET org_id=1 WHERE id=199'))
    review_db.execute(text("INSERT INTO nozzle_lifecycle(id,printer_id,nozzle_type) VALUES(299,199,'brass')"));review_db.commit()
    assert client.get('/printers/199/nozzle',headers=issue(['read:printers'])).status_code==409


def test_unknown_nozzle_details_remain_supported(nozzle_client,review_db):
    client,issue=nozzle_client
    review_db.execute(text('UPDATE printers SET org_id=1 WHERE id=199'));review_db.commit()
    response=client.post('/printers/199/nozzle',headers=issue(['write:printers']),json={'nozzle_type':None,'nozzle_diameter':None})
    assert response.status_code==200 and response.json()['nozzle_diameter'] is None


def test_install_flush_failure_rolls_back_retirement_and_new_record(nozzle_client,review_db,monkeypatch):
    from sqlalchemy.orm import Session
    client,issue=nozzle_client
    review_db.execute(text('UPDATE printers SET org_id=1 WHERE id=199'));review_db.commit()
    headers=issue(['write:printers'])
    before=review_db.execute(text('SELECT id,removed_at FROM nozzle_lifecycle ORDER BY id')).all()
    def fail_after_flush(db):
        db.flush()
        raise RuntimeError('Synthetic commit interruption')
    monkeypatch.setattr(Session,'commit',fail_after_flush)
    with pytest.raises(RuntimeError,match='Synthetic commit interruption'):
        client.post('/printers/199/nozzle',headers=headers,json={'nozzle_diameter':0.6})
    with Session(review_db.get_bind()) as fresh:
        assert fresh.execute(text('SELECT id,removed_at FROM nozzle_lifecycle ORDER BY id')).all()==before

@pytest.mark.parametrize('action',['install','retire'])
def test_ownership_change_before_lock_does_not_mutate_nozzle(nozzle_client,review_db,monkeypatch,action):
    from sqlalchemy.orm import Session
    client,issue=nozzle_client
    review_db.execute(text('UPDATE printers SET org_id=1 WHERE id=199'));review_db.commit()
    headers=issue(['write:printers'])
    before=review_db.execute(text('SELECT id,removed_at FROM nozzle_lifecycle ORDER BY id')).all()
    original=Session.execute;changed=[]
    def change_owner_before_lock(db,statement,*args,**kwargs):
        if 'UPDATE printers SET id=id' in str(statement) and not changed:
            changed.append(True)
            review_db.execute(text('UPDATE printers SET org_id=2,shared=0 WHERE id=199'));review_db.commit()
        return original(db,statement,*args,**kwargs)
    monkeypatch.setattr(Session,'execute',change_owner_before_lock)
    response=(client.post('/printers/199/nozzle',headers=headers,json={'nozzle_diameter':0.6}) if action=='install' else client.patch('/printers/199/nozzle/199/retire',headers=headers))
    assert changed==[True]
    assert response.status_code==404
    with Session(review_db.get_bind()) as fresh:
        assert fresh.execute(text('SELECT id,removed_at FROM nozzle_lifecycle ORDER BY id')).all()==before
        assert fresh.execute(text('SELECT org_id FROM printers WHERE id=199')).scalar_one()==2

@pytest.mark.parametrize('path', ['/printers/199/nozzle', '/printers/199/nozzle/history'])
def test_legacy_unknown_nozzle_usage_remains_unknown(nozzle_client, review_db, path):
    client, issue = nozzle_client
    review_db.execute(text('UPDATE printers SET org_id=1 WHERE id=199'))
    review_db.execute(text('UPDATE nozzle_lifecycle SET print_hours_accumulated=NULL,print_count=NULL WHERE id=199'))
    review_db.commit()
    response = client.get(path, headers=issue(['read:printers']))
    assert response.status_code == 200
    record = response.json()[0] if path.endswith('/history') else response.json()
    assert record['print_hours_accumulated'] is None
    assert record['print_count'] is None
