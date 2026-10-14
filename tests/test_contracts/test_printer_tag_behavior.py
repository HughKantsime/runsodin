"""Printer tag persistence and bulk ownership through actual scoped authentication."""
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session
from tests.test_contracts.test_api_token_delegation_behavior import token_client,review_db

@pytest.fixture
def tag_client(token_client,review_db):
    from modules.printers import routes_crud
    client,issue=token_client
    client.app.include_router(routes_crud.router)
    for pid,org in [(399,1),(400,2)]:
        review_db.execute(text("INSERT INTO printers(id,name,org_id,is_active,tags,shared,slot_count,timelapse_enabled) VALUES(:id,:name,:org,1,'[\"existing\"]',0,4,0)"),{'id':pid,'org':org,'name':f'Synthetic tag printer {pid}'})
    review_db.commit()
    return client,issue


def stored(review_db,pid):
    with Session(review_db.get_bind()) as fresh:
        from modules.printers.models import Printer
        p=fresh.get(Printer,pid)
        return list(p.tags),p.is_active


def test_bulk_tag_is_persisted_in_fresh_session(tag_client,review_db):
    client,issue=tag_client
    response=client.post('/printers/bulk-update',headers=issue(['write:printers']),json={'printer_ids':[399],'action':'add_tag','tag':'new'})
    assert response.status_code==200
    assert stored(review_db,399)[0]==['existing','new']


@pytest.mark.parametrize('action',['add_tag','enable','disable'])
def test_bulk_foreign_printer_refused_without_partial_local_mutation(tag_client,review_db,action):
    client,issue=tag_client
    if action=='enable':
        review_db.execute(text('UPDATE printers SET is_active=0 WHERE id IN (399,400)'));review_db.commit()
    before={pid:stored(review_db,pid) for pid in (399,400)}
    response=client.post('/printers/bulk-update',headers=issue(['write:printers']),json={'printer_ids':[399,400],'action':action,'tag':'new'})
    assert response.status_code==404
    assert {pid:stored(review_db,pid) for pid in (399,400)}==before


@pytest.mark.parametrize('body',[
    {'printer_ids':[True],'action':'disable'},
    {'printer_ids':[399.0],'action':'disable'},
    {'printer_ids':['399'],'action':'disable'},
    {'printer_ids':[-1],'action':'disable'},
    {'printer_ids':[0],'action':'disable'},
    {'printer_ids':[2**63],'action':'disable'},
    {'printer_ids':[],'action':'disable'},
    {'printer_ids':[399]*1001,'action':'disable'},
    {'printer_ids':[399],'action':'invalid'},
    {'printer_ids':[399],'action':'add_tag','tag':True},
    {'printer_ids':[399],'action':'add_tag','tag':' '},
    {'printer_ids':[399],'action':'add_tag','tag':'x'*129},
])
def test_invalid_bulk_body_has_no_mutation(tag_client,review_db,body):
    client,issue=tag_client;before=stored(review_db,399)
    response=client.post('/printers/bulk-update',headers=issue(['write:printers']),json=body)
    assert response.status_code==400
    assert stored(review_db,399)==before


@pytest.mark.parametrize('tag',['existing','  new  '])
def test_bulk_duplicates_count_unique_processed_targets_and_keep_tags(tag_client,review_db,tag):
    client,issue=tag_client
    response=client.post('/printers/bulk-update',headers=issue(['write:printers']),json={'printer_ids':[399,399],'action':'add_tag','tag':tag})
    assert response.status_code==200 and response.json()['affected']==1
    assert stored(review_db,399)[0]==(['existing'] if tag=='existing' else ['existing','new'])


def test_missing_target_refuses_whole_batch(tag_client,review_db):
    client,issue=tag_client;before=stored(review_db,399)
    assert client.post('/printers/bulk-update',headers=issue(['write:printers']),json={'printer_ids':[399,999999],'action':'disable'}).status_code==404
    assert stored(review_db,399)==before


@pytest.mark.parametrize('scopes',[['read:printers'],['write:jobs'],['agent:write']])
def test_bulk_scope_denial_keeps_targets(tag_client,review_db,scopes):
    client,issue=tag_client;before=stored(review_db,399)
    assert client.post('/printers/bulk-update',headers=issue(scopes),json={'printer_ids':[399],'action':'disable'}).status_code==403
    assert stored(review_db,399)==before


@pytest.mark.parametrize('role',['operator','viewer'])
def test_bulk_requires_admin_even_with_write_scope(tag_client,review_db,role):
    client,issue=tag_client
    review_db.execute(text('UPDATE users SET role=:role WHERE id=3'),{'role':role});review_db.commit()
    assert client.post('/printers/bulk-update',headers=issue(['write:printers']),json={'printer_ids':[399],'action':'disable'}).status_code==403
    assert stored(review_db,399)[1] is True


@pytest.mark.parametrize('global_admin',[False,True])
def test_null_org_and_global_admin_bulk_positive(tag_client,review_db,global_admin):
    client,issue=tag_client
    if global_admin:review_db.execute(text('UPDATE users SET group_id=NULL WHERE id=3'))
    else:review_db.execute(text('UPDATE printers SET org_id=NULL WHERE id=400'))
    review_db.commit()
    response=client.post('/printers/bulk-update',headers=issue(['write:printers']),json={'printer_ids':[399,400],'action':'disable'})
    assert response.status_code==200 and response.json()['affected']==2
    assert all(stored(review_db,pid)[1] is False for pid in (399,400))


@pytest.mark.parametrize('bad_json',['{}','[1]','broken'])
def test_corrupt_tags_refuse_atomically_without_disclosure(tag_client,review_db,bad_json):
    client,issue=tag_client
    review_db.execute(text('UPDATE printers SET org_id=1,tags=:tags WHERE id=400'),{'tags':bad_json});review_db.commit()
    response=client.post('/printers/bulk-update',headers=issue(['write:printers']),json={'printer_ids':[399,400],'action':'add_tag','tag':'new'})
    assert response.status_code in (409,503)
    assert stored(review_db,399)[0]==['existing']
    assert 'broken' not in response.text
    assert review_db.execute(text('SELECT tags FROM printers WHERE id=400')).scalar_one()==bad_json



def test_bulk_commit_failure_rolls_back_flushed_tags_without_disclosure(tag_client,review_db,monkeypatch):
    client,issue=tag_client;headers=issue(['write:printers'])
    review_db.execute(text('UPDATE printers SET org_id=1 WHERE id=400'));review_db.commit()
    def refused(session):
        session.flush()
        raise RuntimeError('Synthetic private failure marker')
    monkeypatch.setattr(Session,'commit',refused)
    response=client.post('/printers/bulk-update',headers=headers,json={'printer_ids':[399,400],'action':'add_tag','tag':'new'})
    assert response.status_code==503
    assert 'private failure' not in response.text
    assert all(stored(review_db,pid)[0]==['existing'] for pid in (399,400))


def test_ownership_changed_before_writer_boundary_is_rechecked(tag_client,review_db):
    from sqlalchemy import event
    client,issue=tag_client;headers=issue(['write:printers'])
    engine=review_db.get_bind();changed=[False]
    def change_owner(conn,cursor,statement,parameters,context,executemany):
        if statement.startswith('UPDATE printers SET name=name') and not changed[0]:
            changed[0]=True
            with engine.begin() as writer:
                writer.execute(text('UPDATE printers SET org_id=2 WHERE id=399'))
    event.listen(engine,'before_cursor_execute',change_owner)
    try:
        response=client.post('/printers/bulk-update',headers=headers,json={'printer_ids':[399],'action':'disable'})
        assert response.status_code==404 and changed[0]
        assert stored(review_db,399)[1] is True
    finally:
        event.remove(engine,'before_cursor_execute',change_owner)



def test_tag_catalog_and_filter_hide_private_foreign_tags(tag_client,review_db):
    client,issue=tag_client
    review_db.execute(text('UPDATE printers SET tags=:tags WHERE id=400'),{'tags':'["foreign-only"]'});review_db.commit()
    headers=issue(['read:printers'])
    response=client.get('/printers/tags',headers=headers)
    assert response.status_code==200 and response.json()==['existing']
    response=client.get('/printers',headers=headers,params={'tag':'foreign-only'})
    assert response.status_code==200 and response.json()==[]


def test_tags_can_be_replaced_and_cleared_through_individual_patch(tag_client,review_db):
    client,issue=tag_client;headers=issue(['write:printers'])
    for tags in [['categorize','fixture'],[]]:
        response=client.patch('/printers/399',headers=headers,json={'tags':tags})
        assert response.status_code==200 and response.json()['tags']==tags
        assert stored(review_db,399)[0]==tags


def test_unrelated_patch_preserves_camera_and_explicit_timelapse_toggle_persists(tag_client,review_db):
    client,issue=tag_client;headers=issue(['write:printers'])
    review_db.execute(text("UPDATE printers SET camera_url='http://192.0.2.1/snapshot',camera_enabled=1,timelapse_enabled=1 WHERE id=399"));review_db.commit()
    response=client.patch('/printers/399',headers=headers,json={'tags':['edited'],'timelapse_enabled':False})
    assert response.status_code==200
    assert response.json()['timelapse_enabled'] is False
    with Session(review_db.get_bind()) as fresh:
        row=fresh.execute(text('SELECT camera_url,camera_enabled,timelapse_enabled FROM printers WHERE id=399')).one()
        assert row[0]=='http://192.0.2.1/snapshot' and row[1]==1 and row[2]==0
