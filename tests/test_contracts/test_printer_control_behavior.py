"""HTTP printer controls with real auth/database, command transport substituted."""
import pytest
from sqlalchemy import text
from tests.test_contracts.test_api_token_delegation_behavior import token_client,review_db

@pytest.fixture
def control_client(token_client,review_db,monkeypatch):
    from modules.printers import routes_controls
    client,issue=token_client
    client.app.include_router(routes_controls.router)
    review_db.execute(text("INSERT INTO printers(id,name,model,machine_type,is_active,api_type,org_id,shared,gcode_state) VALUES(299,'Synthetic control','Synthetic','Synthetic',1,'bambu',1,0,'RUNNING')"))
    review_db.commit()
    calls=[]
    monkeypatch.setattr(routes_controls,'_bambu_command_direct',lambda printer,command,*args:(calls.append((printer.id,command,args)) or True))
    monkeypatch.setattr(routes_controls,'_send_printer_command',lambda printer,command:(calls.append((printer.id,command,())) or True))
    return client,issue,calls

@pytest.mark.parametrize('endpoint,body',[
    ('speed',{'speed':True}),('speed',{'speed':2.0}),
    ('fan',{'fan':'part_cooling','speed':True}),
    ('skip-objects',{'object_ids':[True]}),('skip-objects',{'object_ids':[-1]}),
    ('skip-objects',{'object_ids':['1']}),('skip-objects',{'object_ids':[1.5]}),
    ('skip-objects',{'object_ids':[{}]}),
])
def test_invalid_command_values_are_rejected_before_transport(control_client,endpoint,body):
    client,issue,calls=control_client
    response=client.post(f'/printers/299/{endpoint}',headers=issue(['write']),json=body)
    assert response.status_code==422
    assert calls==[]

@pytest.mark.parametrize('endpoint,body,command,args',[
    ('speed',{'speed':1},'set_print_speed',(1,)),
    ('speed',{'speed':4},'set_print_speed',(4,)),
    ('fan',{'fan':'part_cooling','speed':0},'set_fan_speed',('part_cooling',0)),
    ('fan',{'fan':'chamber','speed':255},'set_fan_speed',('chamber',255)),
    ('skip-objects',{'object_ids':[0,1]},'skip_objects',([0,1],)),
])
def test_valid_command_values_reach_transport_once(control_client,endpoint,body,command,args):
    client,issue,calls=control_client
    assert client.post(f'/printers/299/{endpoint}',headers=issue(['write']),json=body).status_code==200
    assert calls==[(299,command,args)]

@pytest.mark.parametrize('endpoint,body',[
    ('stop',None),('pause',None),('resume',None),('clear-errors',None),
    ('speed',{'speed':2}),('fan',{'fan':'chamber','speed':50}),('skip-objects',{'object_ids':[0]}),
])
def test_foreign_control_has_no_transport_or_state_effect(control_client,review_db,endpoint,body):
    client,issue,calls=control_client
    review_db.execute(text('UPDATE printers SET org_id=2 WHERE id=299'));review_db.commit()
    response=client.post(f'/printers/299/{endpoint}',headers=issue(['write','agent:write']),json=body)
    assert response.status_code==404
    assert calls==[]
    review_db.expire_all()
    assert review_db.execute(text('SELECT gcode_state FROM printers WHERE id=299')).scalar_one()=='RUNNING'


@pytest.mark.parametrize('protocol,command', [('bambu','stop_print'),('moonraker','cancel_print'),('prusalink','stop_print'),('elegoo','stop_print')])
@pytest.mark.parametrize('accepted',[True,False])
def test_stop_protocol_command_and_database_outcome(control_client,review_db,monkeypatch,protocol,command,accepted):
    from modules.printers import routes_controls
    client,issue,calls=control_client
    review_db.execute(text('UPDATE printers SET api_type=:protocol WHERE id=299'),{'protocol':protocol});review_db.commit()
    monkeypatch.setattr(routes_controls,'_send_printer_command',lambda printer,action:(calls.append((printer.id,action,())) or accepted))
    response=client.post('/printers/299/stop',headers=issue(['write:printers']))
    assert response.status_code==(200 if accepted else 503)
    assert calls==[(299,command,())]
    review_db.expire_all()
    assert review_db.execute(text('SELECT gcode_state FROM printers WHERE id=299')).scalar_one()==('IDLE' if accepted else 'RUNNING')


@pytest.mark.parametrize('endpoint,body', [('stop',None),('clear-errors',None),('speed',{'speed':2}),('fan',{'fan':'chamber','speed':50}),('skip-objects',{'object_ids':[0]})])
def test_read_token_cannot_command_printer(control_client,endpoint,body):
    client,issue,calls=control_client
    assert client.post(f'/printers/299/{endpoint}',headers=issue(['read:printers']),json=body).status_code==403
    assert calls==[]


@pytest.mark.parametrize('endpoint,body',[
    ('speed',{'speed':0}),('speed',{'speed':5}),('speed',{}),
    ('fan',{'fan':'chamber','speed':-1}),('fan',{'fan':'chamber','speed':256}),
    ('fan',{'fan':'unsupported','speed':50}),
    ('skip-objects',{'object_ids':[]}),('skip-objects',{'object_ids':'0'}),('skip-objects',{}),
])
def test_invalid_command_bounds_do_not_reach_transport(control_client,endpoint,body):
    client,issue,calls=control_client
    assert client.post(f'/printers/299/{endpoint}',headers=issue(['write:printers']),json=body).status_code==422
    assert calls==[]


@pytest.mark.parametrize('endpoint,body', [('clear-errors',None),('speed',{'speed':2}),('fan',{'fan':'chamber','speed':50}),('skip-objects',{'object_ids':[0]})])
@pytest.mark.parametrize('scopes',[['write:jobs'],['agent:write']])
def test_unrelated_or_agent_scope_does_not_authorize_ordinary_controls(control_client,endpoint,body,scopes):
    client,issue,calls=control_client
    assert client.post(f'/printers/299/{endpoint}',headers=issue(scopes),json=body).status_code==403
    assert calls==[]


@pytest.mark.parametrize('scopes',[['read:printers'],['write:jobs'],['agent:write']])
def test_light_command_requires_printer_write_scope(control_client,review_db,monkeypatch,scopes):
    client,issue,calls=control_client
    from modules.printers import routes_controls
    from modules.printers.adapters import bambu
    from modules.printers.telemetry.bambu import session
    def forbidden(*args,**kwargs):
        calls.append(('forbidden-light-boundary',))
        raise AssertionError('Light boundary reached without authorization')
    review_db.execute(text("UPDATE printers SET api_host='192.0.2.1',api_key='synthetic-placeholder' WHERE id=299"));review_db.commit()
    monkeypatch.setattr(routes_controls.crypto,'decrypt',forbidden)
    monkeypatch.setattr(bambu,'BambuPrinter',forbidden)
    monkeypatch.setattr(session,'run_command',forbidden)
    assert client.post('/printers/299/lights',headers=issue(scopes)).status_code==403
    assert calls==[]


@pytest.mark.parametrize('endpoint,body,command,args',[
    ('clear-errors',None,'clear_print_errors',()),
    ('speed',{'speed':2},'set_print_speed',(2,)),
    ('fan',{'fan':'auxiliary','speed':100},'set_fan_speed',('auxiliary',100)),
    ('skip-objects',{'object_ids':[1,0]},'skip_objects',([1,0],)),
])
def test_resource_write_token_commands_once(control_client,endpoint,body,command,args):
    client,issue,calls=control_client
    assert client.post(f'/printers/299/{endpoint}',headers=issue(['write:printers']),json=body).status_code==200
    assert calls==[(299,command,args)]
