"""Narrow credentials must not mutate unrelated resources through real routes."""
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session
from tests.test_contracts.test_api_token_delegation_behavior import token_client, review_db

@pytest.fixture
def resource_client(token_client,review_db,monkeypatch):
    from modules.inventory.routes import spools
    from modules.printers import routes_controls
    client,issue=token_client
    client.app.include_router(spools.router)
    client.app.include_router(routes_controls.router)
    calls=[]
    monkeypatch.setattr(routes_controls,'_send_printer_command',lambda printer,action: calls.append(action) or True)
    review_db.execute(text("INSERT INTO filament_library(id,brand,name,material) VALUES(71,'Synthetic','Scope test','PLA')"))
    review_db.execute(text("INSERT INTO spools(id,filament_id,qr_code,initial_weight_g,remaining_weight_g,status,org_id) VALUES(71,71,'SYNTHETIC-SCOPE',1000,900,'active',1)"))
    review_db.execute(text("UPDATE printers SET gcode_state='RUNNING',print_stage='Printing' WHERE id=9"))
    review_db.commit()
    return client,issue,calls

@pytest.mark.parametrize('operation',['create','update','delete'])
@pytest.mark.parametrize('scope',['read:spools','write:jobs','write:printers'])
def test_unrelated_token_cannot_mutate_spools(resource_client,review_db,operation,scope):
    client,issue,_=resource_client;headers=issue([scope])
    with Session(review_db.get_bind()) as db:
        before=tuple(db.execute(text('SELECT id,status,notes FROM spools ORDER BY id')).all())
    if operation=='create':response=client.post('/spools',headers=headers,json={'filament_id':71,'initial_weight_g':1000})
    elif operation=='update':response=client.patch('/spools/71',headers=headers,json={'notes':'Unexpected mutation'})
    else:response=client.delete('/spools/71',headers=headers)
    assert response.status_code==403
    with Session(review_db.get_bind()) as db:
        unchanged=tuple(db.execute(text('SELECT id,status,notes FROM spools ORDER BY id')).all())==before
        assert unchanged

@pytest.mark.parametrize('scope',['read:printers','write:jobs'])
def test_narrow_token_cannot_send_stop_command(resource_client,review_db,scope):
    client,issue,calls=resource_client
    response=client.post('/printers/9/stop',headers=issue([scope]))
    assert response.status_code==403
    assert calls==[]
    with Session(review_db.get_bind()) as db:
        assert db.execute(text('SELECT gcode_state FROM printers WHERE id=9')).scalar_one()=='RUNNING'

@pytest.mark.parametrize('scope',['write:spools','write','admin','interactive'])
@pytest.mark.parametrize('operation',['create','update','delete'])
def test_authorized_spool_mutations_remain_usable(resource_client,review_db,scope,operation):
    from tests.test_contracts.test_api_token_delegation_behavior import interactive_headers
    client,issue,_=resource_client
    headers=interactive_headers() if scope=='interactive' else issue([scope])
    if operation=='create':response=client.post('/spools',headers=headers,json={'filament_id':71,'initial_weight_g':1000})
    elif operation=='update':response=client.patch('/spools/71',headers=headers,json={'notes':'Authorized mutation'})
    else:response=client.delete('/spools/71',headers=headers)
    assert response.status_code==200
    with Session(review_db.get_bind()) as db:
        if operation=='create':assert db.execute(text('SELECT COUNT(*) FROM spools')).scalar_one()==2
        elif operation=='update':assert db.execute(text('SELECT notes FROM spools WHERE id=71')).scalar_one()=='Authorized mutation'
        else:assert db.execute(text('SELECT status FROM spools WHERE id=71')).scalar_one()=='archived'

@pytest.mark.parametrize('scope',['write:printers','write','admin','interactive'])
def test_authorized_stop_reaches_only_substituted_sender(resource_client,review_db,scope):
    from tests.test_contracts.test_api_token_delegation_behavior import interactive_headers
    client,issue,calls=resource_client
    headers=interactive_headers() if scope=='interactive' else issue([scope])
    response=client.post('/printers/9/stop',headers=headers)
    assert response.status_code==200
    assert calls==['stop_print']
    with Session(review_db.get_bind()) as db:
        assert db.execute(text('SELECT gcode_state FROM printers WHERE id=9')).scalar_one()=='IDLE'

@pytest.mark.parametrize('scope',['read:spools','read','admin','agent:read','agent:write'])
def test_spool_list_accepts_documented_read_credentials(resource_client,scope):
    client,issue,_=resource_client
    response=client.get('/spools',headers=issue([scope]))
    assert response.status_code==200
    assert any(row['id']==71 for row in response.json())

@pytest.mark.parametrize('scope',['read:jobs','read:printers','write:jobs'])
def test_spool_list_denies_unrelated_credentials(resource_client,scope):
    client,issue,_=resource_client
    response=client.get('/spools',headers=issue([scope]))
    assert response.status_code==403

@pytest.mark.parametrize('helper',['role','scope','any'])
@pytest.mark.parametrize('required,granted,expected',[
    ('write',['write:jobs'],403),('read',['read:spools'],403),
    ('write:spools',['write:jobs'],403),('read:spools',['read:jobs'],403),
    ('write:spools',['write:spools'],200),('read:spools',['read:spools'],200),
    ('write:spools',['write'],200),('read:spools',['read'],200),
    ('write:spools',['read'],403),('read:spools',['write'],403),
    ('agent:write',['write'],403),('write:spools',['agent:write'],403),
    ('agent:write',['agent:write'],200),('agent:read',['agent:write'],403),
    ('write:spools',['admin'],200),('read:spools',[],403),
])
def test_three_scope_helpers_share_directional_policy(helper,required,granted,expected):
    from fastapi import FastAPI,Depends
    from fastapi.testclient import TestClient
    from core.dependencies import get_current_user
    from core.rbac import require_role,require_scope,require_any_scope
    from core.errors import OdinError
    from fastapi.responses import JSONResponse
    guard={'role':lambda:require_role('operator',scope=required),
           'scope':lambda:require_scope(required),'any':lambda:require_any_scope(required)}[helper]()
    app=FastAPI()
    app.dependency_overrides[get_current_user]=lambda:{'id':3,'role':'admin','_auth_kind':'user_api_token','_token_scopes':granted}
    @app.exception_handler(OdinError)
    async def structured_error(request,error):return JSONResponse(status_code=error.status,content=error.to_envelope())
    @app.post('/guarded')
    def guarded(current_user=Depends(guard)):return {'authorized':True}
    response=TestClient(app).post('/guarded')
    assert response.status_code==expected

@pytest.fixture
def crud_resource_client(resource_client,review_db,monkeypatch):
    from modules.jobs.routes import jobs_crud
    from modules.models_library.routes import models_crud
    from modules.printers import routes_crud
    from core.registry import registry
    from modules.organizations.services import EducationPolicyService
    monkeypatch.setitem(registry._providers,'EducationPolicyProvider',EducationPolicyService())
    client,issue,calls=resource_client
    for router in (jobs_crud.router,models_crud.router,routes_crud.router):client.app.include_router(router)
    review_db.execute(text("INSERT INTO models(id,name,org_id,default_filament_type) VALUES(71,'Synthetic scope model',1,'PLA')"))
    review_db.execute(text("INSERT INTO jobs(id,item_name,status,charged_to_org_id,quantity,quantity_on_bed,hold,is_locked) VALUES(71,'Synthetic scope job','pending',1,1,1,0,0)"))
    review_db.execute(text("UPDATE printers SET slot_count=4,tags='[]',timelapse_enabled=0,shared=0 WHERE id=9"))
    review_db.commit()
    return client,issue,calls

@pytest.mark.parametrize('resource,path,body,table,column',[
    ('jobs','/jobs/71',{'item_name':'Authorized update'},'jobs','item_name'),
    ('models','/models/71',{'name':'Authorized update'},'models','name'),
    ('printers','/printers/9',{'name':'Authorized update'},'printers','name'),
])
@pytest.mark.parametrize('credential',['matching','unrelated','read_only','broad','interactive'])
def test_resource_updates_use_actual_auth_and_matching_permission(crud_resource_client,review_db,resource,path,body,table,column,credential):
    from tests.test_contracts.test_api_token_delegation_behavior import interactive_headers
    client,issue,_=crud_resource_client
    scopes={'matching':['write:'+resource],'unrelated':['write:spools'],'read_only':['read:'+resource],'broad':['write']}
    headers=interactive_headers() if credential=='interactive' else issue(scopes[credential])
    identifier=9 if resource=='printers' else 71
    with Session(review_db.get_bind()) as db:before=db.execute(text(f'SELECT {column} FROM {table} WHERE id=:id'),{'id':identifier}).scalar_one()
    response=client.patch(path,headers=headers,json=body)
    allowed=credential in ('matching','broad','interactive')
    assert response.status_code==(200 if allowed else 403)
    with Session(review_db.get_bind()) as db:
        assert db.execute(text(f'SELECT {column} FROM {table} WHERE id=:id'),{'id':identifier}).scalar_one()==('Authorized update' if allowed else before)

@pytest.mark.parametrize('scope',['read:spools','write:spools'])
def test_resource_scope_keeps_role_floor(resource_client,review_db,scope):
    client,issue,calls=resource_client
    headers=issue([scope]);review_db.execute(text("UPDATE users SET role='viewer' WHERE id=3"));review_db.commit()
    response=client.patch('/spools/71',headers=headers,json={'notes':'Denied'})
    assert response.status_code==403
    assert calls==[]

@pytest.mark.parametrize('scope',['write:spools','admin'])
def test_resource_scope_keeps_foreign_tenant_hidden(resource_client,review_db,scope):
    client,issue,_=resource_client
    review_db.execute(text('UPDATE spools SET org_id=2 WHERE id=71'));review_db.commit()
    response=client.patch('/spools/71',headers=issue([scope]),json={'notes':'Denied'})
    assert response.status_code==404
    with Session(review_db.get_bind()) as db:assert db.execute(text('SELECT notes FROM spools WHERE id=71')).scalar_one() is None

@pytest.fixture
def foreign_auxiliary(crud_resource_client,review_db):
    from modules.printers.models import FilamentSlot,Printer
    review_db.add(Printer(id=71,name="Foreign scope printer",org_id=2))
    review_db.flush()
    review_db.execute(text('UPDATE models SET org_id=2 WHERE id=71'))
    review_db.add(FilamentSlot(printer_id=71,slot_number=1,color='Original',filament_type='PLA'))
    review_db.execute(text("INSERT INTO print_files(id,model_id,filename) VALUES(71,71,'synthetic.gcode.3mf')"))
    review_db.commit()
    return crud_resource_client

@pytest.mark.parametrize('operation',['slot_read','slot_update','variant_read','variant_delete'])
def test_matching_scope_cannot_access_foreign_auxiliary_resource(foreign_auxiliary,review_db,operation):
    client,issue,_=foreign_auxiliary
    if operation=='slot_read':response=client.get('/printers/71/slots',headers=issue(['read:printers']))
    elif operation=='slot_update':response=client.patch('/printers/71/slots/1',headers=issue(['write:printers']),json={'color':'Denied'})
    elif operation=='variant_read':response=client.get('/models/71/variants',headers=issue(['read:models']))
    else:response=client.delete('/models/71/variants/71',headers=issue(['write:models']))
    assert response.status_code==404
    with Session(review_db.get_bind()) as db:
        assert db.execute(text('SELECT color FROM filament_slots WHERE printer_id=71 AND slot_number=1')).scalar_one()=='Original'
        assert db.execute(text('SELECT COUNT(*) FROM print_files WHERE id=71')).scalar_one()==1

@pytest.mark.parametrize('operation',['slot_read','slot_update','variant_read','variant_delete'])
def test_matching_scope_can_access_owned_auxiliary_resource(foreign_auxiliary,review_db,operation):
    client,issue,_=foreign_auxiliary
    review_db.execute(text('UPDATE printers SET org_id=1 WHERE id=71'))
    review_db.execute(text('UPDATE models SET org_id=1 WHERE id=71'))
    review_db.commit()
    if operation=='slot_read':response=client.get('/printers/71/slots',headers=issue(['read:printers']))
    elif operation=='slot_update':response=client.patch('/printers/71/slots/1',headers=issue(['write:printers']),json={'color':'Authorized'})
    elif operation=='variant_read':response=client.get('/models/71/variants',headers=issue(['read:models']))
    else:response=client.delete('/models/71/variants/71',headers=issue(['write:models']))
    assert response.status_code==200
    with Session(review_db.get_bind()) as db:
        if operation=='slot_update':assert db.execute(text('SELECT color FROM filament_slots WHERE printer_id=71 AND slot_number=1')).scalar_one()=='Authorized'
        elif operation=='variant_delete':assert db.execute(text('SELECT COUNT(*) FROM print_files WHERE id=71')).scalar_one()==0

@pytest.mark.parametrize('operation',['live','reorder'])
def test_printer_auxiliary_operations_respect_all_tenants(foreign_auxiliary,review_db,monkeypatch,operation):
    from modules.printers import routes_status
    client,issue,_=foreign_auxiliary
    fetched=[]
    def fetch(identifier,db):fetched.append(identifier);return {'id':identifier}
    monkeypatch.setattr(routes_status,'_fetch_printer_live_status',fetch)
    # Synthetic unusable transport values, never consumed because fetch is replaced.
    review_db.execute(text("UPDATE printers SET api_host='printer.example.test',api_key='synthetic-unusable' WHERE id IN (9,71)"))
    review_db.execute(text('UPDATE printers SET display_order=10 WHERE id IN (9,71)'));review_db.commit()
    if operation=='live':
        response=client.get('/printers/live-status',headers=issue(['read:printers']))
        assert response.status_code==200
        assert fetched==[9]
        assert response.json()==[{'id':9}]
    else:
        response=client.post('/printers/reorder',headers=issue(['write:printers']),json={'printer_ids':[9,71]})
        assert response.status_code==404
        with Session(review_db.get_bind()) as db:
            assert list(db.execute(text('SELECT display_order FROM printers WHERE id IN (9,71) ORDER BY id')).scalars())==[10,10]

@pytest.mark.parametrize('ids,status', [([9,10],200),([9,9],400),([9,9999],404),([],200)])
def test_owned_reorder_preflights_all_ids(crud_resource_client,review_db,ids,status):
    client,issue,_=crud_resource_client
    review_db.execute(text('UPDATE printers SET display_order=10 WHERE id IN (9,10)'));review_db.commit()
    response=client.post('/printers/reorder',headers=issue(['write:printers']),json={'printer_ids':ids})
    assert response.status_code==status
    with Session(review_db.get_bind()) as db:
        actual=list(db.execute(text('SELECT display_order FROM printers WHERE id IN (9,10) ORDER BY id')).scalars())
        assert actual==([0,1] if ids==[9,10] else [10,10])

@pytest.mark.parametrize('shared,superadmin',[(True,False),(False,True)])
def test_live_status_retains_explicit_shared_and_superadmin_visibility(foreign_auxiliary,review_db,monkeypatch,shared,superadmin):
    from modules.printers import routes_status
    client,issue,_=foreign_auxiliary;fetched=[]
    def fetch(identifier,db):fetched.append(identifier);return {'id':identifier}
    monkeypatch.setattr(routes_status,'_fetch_printer_live_status',fetch)
    review_db.execute(text("UPDATE printers SET api_host='printer.example.test',api_key='synthetic-unusable',shared=:shared WHERE id=71"),{'shared':int(shared)})
    if superadmin:review_db.execute(text('UPDATE users SET group_id=NULL WHERE id=3'))
    review_db.commit()
    response=client.get('/printers/live-status',headers=issue(['read:printers']))
    assert response.status_code==200
    assert fetched==[71]
