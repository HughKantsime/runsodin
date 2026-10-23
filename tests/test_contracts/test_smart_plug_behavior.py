"""Smart-plug HTTP authorization and state, with every device boundary substituted."""
import pytest
from sqlalchemy import text
from tests.test_contracts.test_printer_control_behavior import control_client,token_client,review_db

@pytest.fixture
def plug_client(control_client,review_db,monkeypatch):
    from modules.printers import routes_smart_plug
    client,issue,calls=control_client
    client.app.include_router(routes_smart_plug.router)
    for name,value in [('power_on',True),('power_off',False),('power_toggle',True),('get_energy',{'energy_kwh':1.25}),('get_power_state',True)]:
        monkeypatch.setattr(routes_smart_plug.smart_plug,name,lambda identifier,n=name,v=value,**kwargs:(calls.append((n,identifier)) or v))
    review_db.execute(text("UPDATE printers SET plug_type='mqtt',plug_host='synthetic/topic',plug_cooldown_minutes=0 WHERE id=299"));review_db.commit()
    return client,lambda scopes:{**issue(scopes),'X-Power-Request-Nonce':'a'*32},calls

@pytest.mark.parametrize('suffix',['','/energy','/state'])
def test_private_foreign_plug_reads_do_not_disclose_or_query_device(plug_client,review_db,suffix):
    client,issue,calls=plug_client
    review_db.execute(text('UPDATE printers SET org_id=2 WHERE id=299'));review_db.commit()
    assert client.get('/printers/299/plug'+suffix,headers=issue(['read:printers'])).status_code==404
    assert calls==[]

@pytest.mark.parametrize('suffix',['','/energy','/state'])
def test_unrelated_token_cannot_read_or_query_plug(plug_client,suffix):
    client,issue,calls=plug_client
    assert client.get('/printers/299/plug'+suffix,headers=issue(['write:jobs'])).status_code==403
    assert calls==[]

@pytest.mark.parametrize('method,suffix,body',[
    ('put','',{'type':'mqtt','host':'changed/topic'}),('delete','',None),
    ('post','/on',None),('post','/off',None),('post','/toggle',None),
])
def test_read_token_cannot_configure_or_power_plug(plug_client,review_db,method,suffix,body):
    client,issue,calls=plug_client
    response=getattr(client,method)('/printers/299/plug'+suffix,headers=issue(['read:printers']),**({'json':body} if body is not None else {}))
    assert response.status_code==403
    assert calls==[]
    review_db.expire_all()
    assert review_db.execute(text('SELECT plug_type,plug_host FROM printers WHERE id=299')).one()==('mqtt','synthetic/topic')


def test_plug_config_preserves_explicit_zero_cooldown(plug_client):
    client,issue,calls=plug_client
    response=client.get('/printers/299/plug',headers=issue(['read:printers']))
    assert response.status_code==200 and response.json()['cooldown_minutes']==0
    assert calls==[]


@pytest.mark.parametrize('suffix,expected,calls_expected',[
    ('',404,[]),('/energy',200,[('get_energy',299)]),('/state',200,[('get_power_state',299)]),
])
def test_shared_plug_status_visible_but_configuration_private(plug_client,review_db,suffix,expected,calls_expected):
    client,issue,calls=plug_client
    review_db.execute(text('UPDATE printers SET org_id=2,shared=1 WHERE id=299'));review_db.commit()
    assert client.get('/printers/299/plug'+suffix,headers=issue(['read:printers'])).status_code==expected
    assert calls==calls_expected


@pytest.mark.parametrize('action,value',[('on',True),('off',False),('toggle',True)])
def test_owned_resource_write_calls_plug_once(plug_client,action,value):
    client,issue,calls=plug_client
    response=client.post('/printers/299/plug/'+action,headers=issue(['write:printers']))
    assert response.status_code==200 and response.json()['power_state'] is value
    assert calls==[('power_'+action,299)]


@pytest.mark.parametrize('suffix',['/energy','/state'])
def test_missing_plug_printer_does_not_query_device(plug_client,suffix):
    client,issue,calls=plug_client
    assert client.get('/printers/999999/plug'+suffix,headers=issue(['read:printers'])).status_code==404
    assert calls==[]


@pytest.mark.parametrize('action',['on','off','toggle'])
def test_shared_printer_does_not_grant_plug_write(plug_client,review_db,action):
    client,issue,calls=plug_client
    review_db.execute(text('UPDATE printers SET org_id=2,shared=1 WHERE id=299'));review_db.commit()
    assert client.post('/printers/299/plug/'+action,headers=issue(['write:printers'])).status_code==404
    assert calls==[]


def test_owned_configuration_update_read_and_remove(plug_client,review_db):
    client,issue,calls=plug_client
    headers=issue(['write:printers'])
    response=client.put('/printers/299/plug',headers=headers,json={'type':'mqtt','host':'synthetic/new-topic','auto_on':False,'auto_off':True,'cooldown_minutes':0})
    assert response.status_code==200
    data=client.get('/printers/299/plug',headers=issue(['read:printers'])).json()
    assert data['host']=='synthetic/new-topic' and data['type']=='mqtt'
    assert data['auto_on'] is False and data['auto_off'] is True and data['cooldown_minutes']==0
    assert client.delete('/printers/299/plug',headers=headers).status_code==200
    review_db.expire_all()
    row=review_db.execute(text('SELECT plug_type,plug_host,plug_auth_token,plug_power_state FROM printers WHERE id=299')).one()
    assert row==(None,None,None,None)
    assert calls==[]


@pytest.mark.parametrize('action',['on','off','toggle'])
def test_no_device_result_is_failure_not_success(plug_client,monkeypatch,action):
    from modules.printers import routes_smart_plug
    client,issue,calls=plug_client
    monkeypatch.setattr(routes_smart_plug.smart_plug,'power_'+action,lambda identifier,**kwargs:(calls.append(('unavailable',identifier)) or None))
    response=client.post('/printers/299/plug/'+action,headers=issue(['write:printers']))
    assert response.status_code==400
    assert response.json()['detail']=='No smart plug configured or plug unreachable'
    assert calls==[('unavailable',299)]


@pytest.mark.parametrize('method,suffix,body',[
    ('put','',{'type':'mqtt','host':'forbidden/topic'}),('delete','',None),
    ('post','/on',None),('post','/off',None),('post','/toggle',None),
])
@pytest.mark.parametrize('credential',['viewer','unrelated'])
def test_role_and_unrelated_scope_denials_preserve_configuration(plug_client,review_db,method,suffix,body,credential):
    client,issue,calls=plug_client
    if credential=='viewer':
        review_db.execute(text("UPDATE users SET role='viewer' WHERE id=3"));review_db.commit()
    headers=issue(['write:printers'] if credential=='viewer' else ['write:jobs'])
    response=getattr(client,method)('/printers/299/plug'+suffix,headers=headers,**({'json':body} if body is not None else {}))
    assert response.status_code==403 and calls==[]
    review_db.expire_all()
    assert review_db.execute(text('SELECT plug_type,plug_host FROM printers WHERE id=299')).one()==('mqtt','synthetic/topic')


def test_read_token_cannot_change_global_energy_rate(plug_client,review_db):
    client,issue,calls=plug_client
    before=review_db.execute(text("SELECT value FROM system_config WHERE key='energy_cost_per_kwh'")).scalar_one_or_none()
    response=client.put('/settings/energy-rate',headers=issue(['read:printers']),json={'energy_cost_per_kwh':0.45})
    assert response.status_code==403
    review_db.expire_all()
    assert review_db.execute(text("SELECT value FROM system_config WHERE key='energy_cost_per_kwh'")).scalar_one_or_none()==before


@pytest.mark.parametrize('rate',[-1,True,'invalid',None,float('nan'),float('inf')])
def test_invalid_energy_rate_does_not_poison_global_config(plug_client,review_db,rate):
    import json
    client,issue,calls=plug_client
    before=review_db.execute(text("SELECT value FROM system_config WHERE key='energy_cost_per_kwh'")).scalar_one_or_none()
    response=client.put('/settings/energy-rate',headers={**issue(['admin']),'Content-Type':'application/json'},content=json.dumps({'energy_cost_per_kwh':rate}))
    assert response.status_code==422
    review_db.expire_all()
    assert review_db.execute(text("SELECT value FROM system_config WHERE key='energy_cost_per_kwh'")).scalar_one_or_none()==before


@pytest.mark.parametrize('rate',[0,0.12,2])
def test_valid_energy_rate_round_trip_including_zero(plug_client,rate):
    client,issue,calls=plug_client
    response=client.put('/settings/energy-rate',headers=issue(['admin']),json={'energy_cost_per_kwh':rate})
    assert response.status_code==200 and response.json()['energy_cost_per_kwh']==rate
    response=client.get('/settings/energy-rate',headers=issue(['read:printers']))
    assert response.status_code==200 and response.json()['energy_cost_per_kwh']==rate


@pytest.mark.parametrize('stored',['invalid','NaN','Infinity','-1'])
def test_legacy_invalid_energy_rate_returns_controlled_conflict(plug_client,review_db,stored):
    client,issue,calls=plug_client
    review_db.execute(text("INSERT OR REPLACE INTO system_config(key,value) VALUES('energy_cost_per_kwh',:value)"),{'value':stored});review_db.commit()
    response=client.get('/settings/energy-rate',headers=issue(['read:printers']))
    assert response.status_code==409
    assert response.json()['detail']=='Stored energy rate is invalid'


@pytest.mark.parametrize('body',[[],None,{'energy_cost_per_kwh':10**400}])
def test_invalid_energy_object_and_oversized_integer_do_not_write(plug_client,review_db,body):
    import json
    client,issue,calls=plug_client
    before=review_db.execute(text("SELECT value FROM system_config WHERE key='energy_cost_per_kwh'")).scalar_one_or_none()
    response=client.put('/settings/energy-rate',headers={**issue(['admin']),'Content-Type':'application/json'},content=json.dumps(body))
    assert response.status_code==422
    review_db.expire_all()
    assert review_db.execute(text("SELECT value FROM system_config WHERE key='energy_cost_per_kwh'")).scalar_one_or_none()==before


@pytest.mark.parametrize('action,command,state', [('on','turn_on',True),('off','turn_off',False),('toggle','toggle',True)])
def test_homeassistant_manual_commands_accept_nonce_without_fence(control_client, review_db, monkeypatch, action, command, state):
    from modules.printers import smart_plug, routes_smart_plug
    client, issue, calls = control_client
    client.app.include_router(routes_smart_plug.router)
    monkeypatch.setattr(smart_plug, 'engine', review_db.get_bind())
    review_db.execute(text("UPDATE printers SET plug_type='homeassistant',plug_entity_id='switch.fixture' WHERE id=299"))
    review_db.commit()
    monkeypatch.setattr(smart_plug, 'ha_switch', lambda *args: calls.append(args) or state)
    config = {'type':'homeassistant','host':'http://192.0.2.1','entity_id':'switch.fixture','auth_token':None}
    monkeypatch.setattr(smart_plug, 'get_plug_config', lambda pid: config)
    response = client.post('/printers/299/plug/'+action, headers={**issue(['write:printers']), 'X-Power-Request-Nonce':'a'*32})
    assert response.status_code == 200 and response.json() == {'power_state':state}
    assert calls == [(config['host'],'switch.fixture',None,command)]
    assert review_db.execute(text('SELECT count(*) FROM power_target_operations')).scalar_one() == 0
