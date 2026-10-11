"""Real scoped credentials against maintenance records and global templates."""
import pytest
from sqlalchemy import text
from tests.test_contracts.test_api_token_delegation_behavior import token_client,review_db

@pytest.fixture
def maintenance_token_client(token_client,review_db):
    from modules.system import routes_maintenance
    client,issue=token_client;client.app.include_router(routes_maintenance.router)
    review_db.execute(text("INSERT INTO maintenance_tasks(id,name,interval_print_hours,is_active) VALUES(199,'Synthetic scoped service',10,1)"));review_db.commit()
    return client,issue

@pytest.mark.parametrize('path',['/maintenance/tasks','/maintenance/logs','/maintenance/status'])
def test_unrelated_credential_cannot_read_maintenance(maintenance_token_client,path):
    client,issue=maintenance_token_client
    assert client.get(path,headers=issue(['write:jobs'])).status_code==403

@pytest.mark.parametrize('scope',['read','read:printers','write:jobs'])
def test_narrow_token_cannot_seed_global_maintenance_templates(maintenance_token_client,review_db,scope):
    client,issue=maintenance_token_client
    before=tuple(review_db.execute(text('SELECT id,name FROM maintenance_tasks ORDER BY id')).all())
    response=client.post('/maintenance/seed-defaults',headers=issue([scope]))
    assert response.status_code==403
    assert tuple(review_db.execute(text('SELECT id,name FROM maintenance_tasks ORDER BY id')).all())==before

from tests.test_contracts.test_maintenance_behavior import maintenance_client

@pytest.mark.parametrize('changes',[
 {'name':''},{'name':'   '},{'name':'x'*201},{'printer_model_filter':'x'*101},
 {'interval_print_hours':-1},{'interval_days':0},{'estimated_cost':-1},{'estimated_downtime_min':-1},
])
def test_invalid_maintenance_template_create_is_atomic(maintenance_client,review_db,changes):
    client,_=maintenance_client
    before=tuple(review_db.execute(text('SELECT id,name FROM maintenance_tasks ORDER BY id')).all())
    response=client.post('/api/v1/maintenance/tasks',json={'name':'Synthetic invalid task',**changes})
    assert response.status_code==422,response.text
    review_db.expire_all()
    assert tuple(review_db.execute(text('SELECT id,name FROM maintenance_tasks ORDER BY id')).all())==before

@pytest.mark.parametrize('changes',[{'name':None},{'name':''},{'interval_print_hours':-1},{'interval_days':0},{'estimated_cost':-1}])
def test_invalid_maintenance_template_patch_is_atomic(maintenance_client,review_db,changes):
    client,_=maintenance_client
    before=tuple(review_db.execute(text('SELECT id,name,interval_print_hours,interval_days,estimated_cost FROM maintenance_tasks ORDER BY id')).all())
    response=client.patch('/api/v1/maintenance/tasks/101',json=changes)
    assert response.status_code==422,response.text
    review_db.expire_all()
    assert tuple(review_db.execute(text('SELECT id,name,interval_print_hours,interval_days,estimated_cost FROM maintenance_tasks ORDER BY id')).all())==before

@pytest.mark.parametrize('dry',[False,True])
@pytest.mark.parametrize('changes',[{'task_name':''},{'cost':-1},{'downtime_minutes':-1}])
def test_invalid_maintenance_log_rejected_before_preview_or_write(maintenance_client,review_db,changes,dry):
    client,_=maintenance_client
    before=review_db.execute(text('SELECT COUNT(*) FROM maintenance_logs')).scalar_one()
    response=client.post('/api/v1/maintenance/logs',json={'printer_id':9,'task_name':'Synthetic service',**changes},headers={'X-Dry-Run':'true'} if dry else {})
    assert response.status_code==422,response.text
    assert review_db.execute(text('SELECT COUNT(*) FROM maintenance_logs')).scalar_one()==before

@pytest.mark.parametrize('scope',['read','agent:read','agent:write','admin','interactive'])
@pytest.mark.parametrize('path',['/maintenance/logs','/maintenance/status'])
def test_authorized_maintenance_read_credentials_remain_usable(maintenance_token_client,path,scope):
    from tests.test_contracts.test_api_token_delegation_behavior import interactive_headers
    client,issue=maintenance_token_client
    headers=interactive_headers() if scope=='interactive' else issue([scope])
    assert client.get(path,headers=headers).status_code==200

@pytest.mark.parametrize('scope',['agent:read','agent:write','admin','interactive'])
def test_task_listing_preserves_documented_agent_contract(maintenance_token_client,scope):
    from tests.test_contracts.test_api_token_delegation_behavior import interactive_headers
    client,issue=maintenance_token_client
    headers=interactive_headers() if scope=='interactive' else issue([scope])
    response=client.get('/maintenance/tasks',headers=headers)
    assert response.status_code==200 and any(r['id']==199 for r in response.json())

@pytest.mark.parametrize('scope',['agent:write','admin','interactive'])
def test_agent_service_log_preserves_actual_execution_and_persistence(maintenance_token_client,review_db,scope):
    from tests.test_contracts.test_api_token_delegation_behavior import interactive_headers
    client,issue=maintenance_token_client
    headers=interactive_headers() if scope=='interactive' else issue([scope])
    response=client.post('/maintenance/logs',headers=headers,json={'printer_id':9,'task_id':199,'task_name':'Synthetic scoped service'})
    assert response.status_code==200,response.text
    assert review_db.execute(text("SELECT COUNT(*) FROM maintenance_logs WHERE task_name='Synthetic scoped service'")).scalar_one()==1
    assert response.json()['next_actions']


def test_viewer_agent_write_cannot_log_service(maintenance_token_client,review_db):
    client,issue=maintenance_token_client
    review_db.execute(text("UPDATE users SET role='viewer' WHERE id=3"));review_db.commit()
    headers=issue(['agent:write'])
    before=review_db.execute(text('SELECT COUNT(*) FROM maintenance_logs')).scalar_one()
    response=client.post('/maintenance/logs',headers=headers,json={'printer_id':9,'task_name':'Denied viewer service'})
    assert response.status_code in (401,403)
    assert review_db.execute(text('SELECT COUNT(*) FROM maintenance_logs')).scalar_one()==before

@pytest.mark.parametrize('field',['interval_print_hours','estimated_cost'])
@pytest.mark.parametrize('value',['Infinity','NaN'])
def test_nonfinite_template_values_are_rejected_before_write(maintenance_client,review_db,field,value):
    client,_=maintenance_client
    before=review_db.execute(text('SELECT COUNT(*) FROM maintenance_tasks')).scalar_one()
    response=client.post('/api/v1/maintenance/tasks',json={'name':'Synthetic nonfinite',field:value})
    assert response.status_code==422
    assert review_db.execute(text('SELECT COUNT(*) FROM maintenance_tasks')).scalar_one()==before

@pytest.mark.parametrize('scope',['write','admin','interactive'])
def test_authorized_template_seeding_remains_sequentially_idempotent(maintenance_token_client,review_db,scope):
    from tests.test_contracts.test_api_token_delegation_behavior import interactive_headers
    client,issue=maintenance_token_client
    headers=interactive_headers() if scope=='interactive' else issue([scope])
    assert client.post('/maintenance/seed-defaults',headers=headers).status_code==200
    after=tuple(review_db.execute(text('SELECT id,name FROM maintenance_tasks ORDER BY id')).all())
    assert len(after)>1
    assert client.post('/maintenance/seed-defaults',headers=headers).status_code==200
    assert tuple(review_db.execute(text('SELECT id,name FROM maintenance_tasks ORDER BY id')).all())==after


def test_optional_template_intervals_can_be_cleared_without_changing_name(maintenance_client,review_db):
    client,_=maintenance_client
    response=client.patch('/api/v1/maintenance/tasks/101',json={'interval_print_hours':None,'interval_days':None})
    assert response.status_code==200
    row=review_db.execute(text('SELECT name,interval_print_hours,interval_days FROM maintenance_tasks WHERE id=101')).one()
    assert tuple(row)==('Synthetic service',None,None)
