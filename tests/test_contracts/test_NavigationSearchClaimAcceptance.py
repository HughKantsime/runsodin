"""Real hashed-token HTTP search against owned SQLite only."""
import json,secrets
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from tests.test_contracts.test_education_review_workflow import review_db

@pytest.fixture
def search_client(review_db,monkeypatch):
    from core.auth import hash_password
    from core.db import get_db
    from modules.system.routes_admin import router
    monkeypatch.delenv('API_KEY',raising=False)
    app=FastAPI();app.include_router(router)
    app.dependency_overrides[get_db]=lambda:review_db
    # Foreign rows inserted first exceed category limit: filtering must precede limit.
    for org,count in [(2,7),(1,1),(None,1)]:
        for i in range(count):
            args={'name':f'NavUnique {org}-{i}','org':org}
            review_db.execute(text('INSERT INTO models(name,org_id) VALUES(:name,:org)'),args)
            review_db.execute(text('INSERT INTO products(name,org_id) VALUES(:name,:org)'),args)
            review_db.execute(text("INSERT INTO orders(order_number,customer_name,status,org_id) VALUES(:name,'Fixture','pending',:org)"),args)
            review_db.execute(text('INSERT INTO printers(name,org_id) VALUES(:name,:org)'),args)
            fid=review_db.execute(text("INSERT INTO filament_library(brand,name) VALUES('',:name) RETURNING id"),args).scalar_one()
            review_db.execute(text('INSERT INTO spools(filament_id,vendor,org_id) VALUES(:fid,:name,:org)'),{**args,'fid':fid})
            review_db.execute(text("INSERT INTO jobs(item_name,status,charged_to_org_id) VALUES(:name,'pending',:org)"),args)
    review_db.commit()
    def issue(scopes,group=1):
        review_db.execute(text('UPDATE users SET group_id=:group WHERE id=3'),{'group':group})
        raw='odin_'+secrets.token_urlsafe(32)
        review_db.execute(text("INSERT INTO api_tokens(user_id,name,token_hash,token_prefix,scopes) VALUES(3,'Search fixture',:hash,:prefix,:scopes)"),{'hash':hash_password(raw),'prefix':raw[:10],'scopes':json.dumps(scopes)})
        review_db.commit()
        return {'X-API-Key':raw}
    return TestClient(app),issue

@pytest.mark.parametrize('scopes,visible',[
    (['read:models'],['models']),(['read:printers'],['printers']),(['read:jobs'],['jobs']),(['read:spools'],['spools']),
    (['agent:read'],['jobs','spools','printers','orders']),(['agent:write'],['jobs','spools','printers','orders']),
    (['read'],['models','jobs','spools','printers','products']),(['admin'],['models','jobs','spools','printers','products','orders']),
])
def test_search_hashed_token_exact_category_grants_and_tenant_filter(search_client,scopes,visible):
    client,issue=search_client
    response=client.get('/search?q=NavUnique',headers=issue(scopes))
    assert response.status_code==200
    data=response.json()
    assert set(data)=={'models','jobs','spools','printers','orders','products'}
    for category,items in data.items():
        if category in visible:
            assert len(items)==2
            assert {item['name'].strip() for item in items}=={'NavUnique 1-0','NavUnique None-0'}
        else:assert items==[]

def test_empty_hashed_token_is_rejected_by_actual_authentication(search_client):
    client,issue=search_client
    assert client.get('/search?q=NavUnique',headers=issue([])).status_code==401

@pytest.mark.parametrize('group,count',[(None,5),(2,5)])
def test_superadmin_and_foreign_tenant_search(search_client,group,count):
    client,issue=search_client
    data=client.get('/search?q=NavUnique',headers=issue(['admin'],group)).json()
    for category,items in data.items():
        assert len(items)==count
        if group==2:assert all(item['name'].strip().startswith(('NavUnique 2-','NavUnique None-')) for item in items)

def test_unassigned_viewer_and_empty_scoped_principal_do_not_bypass(search_client,review_db):
    from modules.system.routes_admin import global_search
    unassigned=global_search('NavUnique',review_db,{'id':1,'role':'viewer','group_id':None})
    assert all(len(items)==1 and items[0]['name'].strip()=='NavUnique None-0' for items in unassigned.values())
    assert all(items==[] for items in global_search('NavUnique',review_db,{'id':1,'role':'viewer','group_id':1,'_auth_kind':'user_api_token','_token_scopes':[]}).values())
