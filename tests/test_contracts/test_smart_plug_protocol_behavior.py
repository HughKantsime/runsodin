"""Actual protocol parsers/configuration; HTTP device boundary substituted."""
import json
import pytest
from sqlalchemy import text
from tests.test_contracts.test_education_review_workflow import review_db

@pytest.mark.parametrize('kind,payload',[
    ('tasmota',{}),('tasmota',{'POWER':'UNKNOWN'}),('tasmota',{'POWER':False}),('tasmota',{'POWER':0}),
    ('homeassistant',{}),('homeassistant',{'state':'unavailable'}),('homeassistant',{'state':'unknown'}),('homeassistant',{'state':False}),
])
def test_unknown_protocol_state_is_not_reported_as_power_off(monkeypatch,kind,payload):
    from modules.printers import smart_plug
    monkeypatch.setattr(smart_plug,'_http_request',lambda *args,**kwargs:(200,json.dumps(payload)))
    result=smart_plug.tasmota_power('192.0.2.1','') if kind=='tasmota' else smart_plug.ha_get_state('http://192.0.2.1','switch.synthetic','synthetic')
    assert result is None


@pytest.mark.parametrize('kind,state,expected',[('tasmota','ON',True),('tasmota','OFF',False),('homeassistant','on',True),('homeassistant','off',False)])
def test_explicit_protocol_state_preserves_true_and_false(monkeypatch,kind,state,expected):
    from modules.printers import smart_plug
    payload={'POWER':state} if kind=='tasmota' else {'state':state}
    monkeypatch.setattr(smart_plug,'_http_request',lambda *args,**kwargs:(200,json.dumps(payload)))
    result=smart_plug.tasmota_power('192.0.2.1','') if kind=='tasmota' else smart_plug.ha_get_state('http://192.0.2.1','switch.synthetic','synthetic')
    assert result is expected


def test_actual_plug_config_preserves_zero_cooldown(review_db,monkeypatch):
    from modules.printers import smart_plug
    review_db.execute(text("INSERT INTO printers(id,name,plug_type,plug_host,plug_cooldown_minutes) VALUES(599,'Synthetic zero','tasmota','192.0.2.1',0)"));review_db.commit()
    monkeypatch.setattr(smart_plug,'engine',review_db.get_bind())
    assert smart_plug.get_plug_config(599)['cooldown_minutes']==0


@pytest.mark.parametrize('kind',['tasmota','homeassistant'])
def test_unknown_device_read_preserves_prior_database_observation(review_db,monkeypatch,kind):
    from modules.printers import smart_plug
    review_db.execute(text("INSERT INTO printers(id,name,plug_type,plug_host,plug_entity_id,plug_power_state) VALUES(599,'Synthetic unknown',:kind,'http://192.0.2.1','switch.synthetic',1)"),{'kind':kind});review_db.commit()
    monkeypatch.setattr(smart_plug,'engine',review_db.get_bind())
    monkeypatch.setattr(smart_plug,'_http_request',lambda *args,**kwargs:(200,json.dumps({'state':'unavailable'} if kind=='homeassistant' else {})))
    assert smart_plug.get_power_state(599) is None
    with review_db.get_bind().connect() as fresh:
        assert fresh.execute(text('SELECT plug_power_state FROM printers WHERE id=599')).scalar_one()==1


@pytest.mark.parametrize('accepted',[True,False])
def test_home_assistant_queries_measured_state_only_after_accepted_service(monkeypatch,accepted):
    from modules.printers import smart_plug
    calls=[]
    def request(url,method='GET',headers=None,data=None,**kwargs):
        calls.append((url,method,json.loads(data) if data else None))
        if method=='POST':return (200 if accepted else 503),'[]'
        return 200,'{"state":"off"}'
    monkeypatch.setattr(smart_plug,'_http_request',request)
    result=smart_plug.ha_switch('http://192.0.2.1/','switch.synthetic','synthetic','turn_off')
    assert result is (False if accepted else None)
    assert calls[0]==('http://192.0.2.1/api/services/switch/turn_off','POST',{'entity_id':'switch.synthetic'})
    assert len(calls)==(2 if accepted else 1)
    if accepted:assert calls[1]==('http://192.0.2.1/api/states/switch.synthetic','GET',None)


def test_mqtt_plug_command_reaches_configured_runtime_publisher(monkeypatch):
    from types import SimpleNamespace
    from modules.printers import smart_plug
    from modules.notifications import mqtt_republish
    calls=[]
    client=SimpleNamespace(publish=lambda topic,payload,**kwargs:(calls.append((topic,payload)) or SimpleNamespace(rc=0,wait_for_publish=lambda **kwargs:None,is_published=lambda:True)))
    monkeypatch.setattr(mqtt_republish,'_get_client',lambda:client)
    monkeypatch.setattr(mqtt_republish,'_client',client)
    smart_plug.mqtt_power('synthetic/plug/command','ON')
    assert calls==[('synthetic/plug/command','ON')]


@pytest.mark.parametrize('failure',['transport','policy'])
def test_http_error_does_not_disclose_private_connection_details(monkeypatch,caplog,failure):
    import urllib.request
    from modules.printers import smart_plug
    from core import itar
    def refused(*args,**kwargs):
        if failure=='policy':raise itar.ItarOutboundBlocked('Synthetic private marker')
        raise RuntimeError('Synthetic private marker')
    monkeypatch.setattr(itar,'enforce_request_destination',refused if failure=='policy' else lambda url:None)
    monkeypatch.setattr(urllib.request,'build_opener',lambda *args: type('SyntheticOpener',(),{'open':staticmethod(refused)})())
    status,body=smart_plug._http_request('http://127.0.0.1/synthetic')
    assert status==0 and 'Synthetic private marker' not in body
    assert 'Synthetic private marker' not in caplog.text
