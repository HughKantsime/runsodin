"""Actual CSV parsing, scope and role behavior on isolated HTTP routes."""
import csv
import io
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session
from tests.test_contracts.test_education_review_workflow import review_db

@pytest.fixture
def export_client(review_db):
    from core.db import get_db
    from core.dependencies import get_current_user
    from modules.reporting.routes import exports
    for identifier, org, name in [(101,1,'Own, quoted "name"'),(102,2,'Foreign private'),(103,None,'Shared')]:
        review_db.execute(text("INSERT INTO models(id,name,org_id) VALUES(:id,:name,:org)"),{"id":identifier,"name":name,"org":org})
        review_db.execute(text("INSERT INTO jobs(id,item_name,status,charged_to_org_id,quantity,hold) VALUES(:id,:name,'pending',:org,1,0)"),{"id":identifier,"name":name,"org":org})
    review_db.execute(text("INSERT INTO filament_library(id,brand,name,material) VALUES(101,'Synthetic','Test','PLA')"))
    for identifier, org in [(101,1),(102,2),(103,None)]:
        review_db.execute(text("INSERT INTO spools(id,filament_id,status,org_id,remaining_weight_g) VALUES(:id,101,'active',:org,87.25)"),{"id":identifier,"org":org})
        review_db.execute(text("INSERT INTO spool_usage(id,spool_id,job_id,weight_used_g,notes) VALUES(:id,:id,:id,12.75,'Synthetic usage')"),{"id":identifier})
    review_db.commit()
    app=FastAPI();app.include_router(exports.router)
    def request_db():
        with Session(review_db.get_bind()) as s: yield s
    app.dependency_overrides[get_db]=request_db
    identity={"id":3,"role":"operator","group_id":1,"is_active":True}
    app.dependency_overrides[get_current_user]=lambda:identity
    return TestClient(app), identity

@pytest.mark.parametrize("kind",["jobs","models","spools","filament-usage"])
@pytest.mark.parametrize("role,org,expected",[("admin",1,{"101","103"}),("operator",1,{"101","103"}),("operator",None,{"103"}),("admin",None,{"101","102","103"})])
def test_csv_export_exact_rows_and_scopes(export_client,kind,role,org,expected):
    client, identity = export_client
    identity.update(role=role,group_id=org)
    response=client.get('/export/'+kind)
    assert response.status_code==200,response.text
    assert response.headers['content-type'].startswith('text/csv')
    assert 'attachment' in response.headers['content-disposition']
    rows=list(csv.DictReader(io.StringIO(response.text)))
    seeded=[r for r in rows if r['ID'] in {'101','102','103'}]
    assert {r['ID'] for r in seeded} == expected
    if kind in ('jobs','models') and '101' in expected:
        name_field='Item Name' if kind=='jobs' else 'Name'
        assert next(r for r in seeded if r['ID']=='101')[name_field] == 'Own, quoted "name"'
    if kind=='filament-usage':
        assert all(float(r['Weight Used (g)'])==12.75 for r in seeded)

@pytest.mark.parametrize("kind",["jobs","models","spools","filament-usage"])
def test_viewer_cannot_export(export_client,kind):
    client, identity=export_client;identity['role']='viewer'
    assert client.get('/export/'+kind).status_code==403


def test_job_status_filter_is_applied_to_csv(export_client):
    client, identity=export_client
    response=client.get('/export/jobs?status=completed')
    assert response.status_code==200,response.text
    assert list(csv.DictReader(io.StringIO(response.text)))==[]
