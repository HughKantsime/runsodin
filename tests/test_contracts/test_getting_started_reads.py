"""Bounded checklist reads use the selected organization and retain authorization."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import event, text

from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401


@pytest.fixture
def checklist_client(review_db):
    from core.db import get_db
    from core.dependencies import get_current_user
    from modules.jobs.routes import presets
    from modules.models_library.routes import models_crud, print_files

    for identifier, org in ((101, 1), (102, 2), (103, None)):
        review_db.execute(text("INSERT INTO models(id,name,org_id,default_filament_type) VALUES(:id,:name,:org,'PLA')"),
                          {'id': identifier, 'name': f'Model {identifier}', 'org': org})
        review_db.execute(text("INSERT INTO print_files(id,filename,org_id,uploaded_at) VALUES(:id,'part.3mf',:org,:uploaded)"),
                          {'id': identifier, 'org': org, 'uploaded': f'2026-11-{30 - (identifier - 100):02} 12:00:00'})
        review_db.execute(text("INSERT INTO printers(id,name,org_id,api_type) VALUES(:id,:name,:org,'bambu')"),
                          {'id': identifier, 'org': org, 'name': f'Printer {identifier}'})
        review_db.execute(text("INSERT INTO print_jobs(id,printer_id,status,started_at) VALUES(:id,:id,'completed',:started)"),
                          {'id': identifier, 'started': f'2026-11-{30 - (identifier - 100):02} 12:00:00'})
    review_db.commit()
    app = FastAPI()
    for router in (models_crud.router, print_files.router, presets.router):
        app.include_router(router)
    actor = {'id': 3, 'role': 'admin', 'group_id': None, 'is_active': True}
    app.dependency_overrides[get_current_user] = lambda: actor
    app.dependency_overrides[get_db] = lambda: review_db
    with TestClient(app) as client:
        yield client, actor


@pytest.mark.parametrize('path', ['/models', '/print-files', '/print-jobs'])
def test_selected_org_filters_before_limit(checklist_client, path):
    client, _ = checklist_client
    response = client.get(path, params={'org_id': 2, 'limit': 1, 'include_scheduled': True})
    assert response.status_code == 200, response.text
    assert [row['id'] for row in response.json()] == [102]


@pytest.mark.parametrize('path', ['/models', '/print-files', '/print-jobs'])
@pytest.mark.parametrize('group,expected', [(1, {101, 103}), (None, {101, 102, 103})])
def test_default_scope_is_unchanged(checklist_client, path, group, expected):
    client, actor = checklist_client
    actor['group_id'] = group
    response = client.get(path)
    assert response.status_code == 200, response.text
    assert {row['id'] for row in response.json() if row['id'] >= 101} == expected


@pytest.mark.parametrize('path', ['/models', '/print-files', '/print-jobs'])
def test_org_member_cannot_select_foreign_scope(checklist_client, path):
    client, actor = checklist_client
    actor['group_id'] = 1
    response = client.get(path, params={'org_id': 2})
    assert response.status_code == 200, response.text
    assert {row['id'] for row in response.json() if row['id'] >= 101} == {101, 103}


def test_model_existence_read_is_bounded_in_sql(checklist_client, review_db):
    client, _ = checklist_client
    statements = []
    def capture(_conn, _cursor, statement, parameters, _context, _many):
        if 'FROM models' in statement:
            statements.append(statement)
    event.listen(review_db.get_bind(), 'before_cursor_execute', capture)
    try:
        response = client.get('/models?limit=1')
    finally:
        event.remove(review_db.get_bind(), 'before_cursor_execute', capture)
    assert response.status_code == 200, response.text
    assert len(response.json()) == 1
    assert statements and all('LIMIT' in sql for sql in statements)


@pytest.mark.parametrize('limit', [0, -1, 101])
def test_model_limit_is_validated(checklist_client, limit):
    client, _ = checklist_client
    assert client.get('/models', params={'limit': limit}).status_code == 422
