"""Exact private operation identity, synthetic inputs only."""
import copy
import time
import pytest
from core import sender_operation_protocol as protocol
from core.sender_ipc import ProtocolError


def request(operation='status_idle',inputs=None):
    return {'version':1,'operation':operation,'operation_id':'8'*32,'deadline':time.monotonic()+10,
        'identity':{'claim_token':'1'*32,'registration_key':'2'*32,'origin_key':'3'*32,
            'runtime_incarnation':'4'*32,'physical_key':'5'*64,'configuration_key':'6'*64,
            'payload_sha256':'7'*64,'printer_id':1,'job_id':2,'owner_org_id':None},
        'inputs':inputs or {'api_type':'moonraker','ip':'127.0.0.1','port':80,'api_key':''}}


@pytest.mark.parametrize('inputs',[
    {'api_type':'bambu','ip':'127.0.0.1','serial':'TEST_SERIAL','access_code':''},
    {'api_type':'moonraker','ip':'::1','port':80,'scheme':'http','api_key':''},
    {'api_type':'prusalink','ip':'127.0.0.1','username':'maker','password':''},
])
def test_current_status_input_shapes(inputs):
    validated=protocol.validate_request(request(inputs=inputs))
    assert validated.message['inputs']==inputs
    assert '127.0.0.1' not in repr(validated)


@pytest.mark.parametrize('inputs',[
    {'type':'tasmota','host':'127.0.0.1:8080'},
    {'type':'homeassistant','host':'http://127.0.0.1:8123','entity_id':'switch.test','auth_token':''},
    {'type':'mqtt','entity_id':'test/power','connection':{'host':'127.0.0.1','port':1883,'username':'','password':'','use_tls':False}},
])
def test_current_power_input_shapes(inputs):
    assert protocol.validate_request(request('power_on',inputs)).message['inputs']==inputs


@pytest.mark.parametrize('field', ['claim_token','registration_key','origin_key','runtime_incarnation',
    'physical_key','configuration_key','payload_sha256','printer_id','job_id','owner_org_id'])
def test_result_requires_every_identity_field(field):
    original=protocol.validate_request(request())
    result=protocol.make_result(original,{'idle':True}).message
    result['identity'][field]='wrong' if field not in ('printer_id','job_id','owner_org_id') else 99
    with pytest.raises(ProtocolError):protocol.validate_result(result,original)


@pytest.mark.parametrize('field,value',[('version',True),('version',2),('operation','eval'),
    ('operation_id','8'*31),('deadline',True),('deadline',float('inf')),('deadline',0),('extra',1)])
def test_invalid_request_envelope(field,value):
    original=request();original[field]=value
    with pytest.raises(ProtocolError):protocol.validate_request(original)


@pytest.mark.parametrize('field,value',[('job_id',True),('printer_id',0),('owner_org_id',True),
    ('claim_token','A'*32),('registration_key',None),('physical_key','x'*64)])
def test_invalid_identity(field,value):
    original=request();original['identity'][field]=value
    with pytest.raises(ProtocolError):protocol.validate_request(original)


@pytest.mark.parametrize('inputs',[
    {'api_type':'elegoo','ip':'127.0.0.1'},
    {'api_type':'moonraker','ip':'127.0.0.1','port':True},
    {'api_type':'moonraker','ip':'127.0.0.1','scheme':'ftp'},
    {'api_type':'moonraker','ip':'127.0.0.1','callback':'eval'},
    {'api_type':'bambu','ip':'127.0.0.1','serial':'x/#','access_code':''},
    {'api_type':'prusalink','ip':'127.0.0.1','api_key':'x\n'},
])
def test_bad_status_inputs(inputs):
    with pytest.raises(ProtocolError):protocol.validate_request(request(inputs=inputs))


def test_copy_isolation():
    raw=request();validated=protocol.validate_request(raw)
    raw['inputs']['ip']='changed';raw['identity']['job_id']=99
    assert validated.message['inputs']['ip']=='127.0.0.1'
    assert validated.message['identity']['job_id']==2


@pytest.mark.parametrize('payload',[{'idle':1},{'idle':True,'cleanup':True},{'message':'network error'},{}])
def test_bad_status_results(payload):
    with pytest.raises(ProtocolError):protocol.make_result(protocol.validate_request(request()),payload)


@pytest.mark.parametrize('field,value',[('operation_id','9'*32),('operation','power_on'),('deadline',1),('version',2),('extra',True)])
def test_result_envelope_drift(field,value):
    original=protocol.validate_request(request());result=protocol.make_result(original,{'idle':True}).message
    result[field]=value
    with pytest.raises(ProtocolError):protocol.validate_result(result,original)


@pytest.mark.parametrize('status,state',[('accepted',None),('accepted',False),('uncertain',True),('refused','false')])
def test_invalid_http_power_result(status,state):
    original=protocol.validate_request(request('power_on',{'type':'tasmota','host':'127.0.0.1'}))
    with pytest.raises(ProtocolError):protocol.make_result(original,{'status':status,'power_state':state})


def test_mqtt_accepted_does_not_claim_physical_power():
    original=protocol.validate_request(request('power_on',{'type':'mqtt','entity_id':'test/power',
        'connection':{'host':'127.0.0.1','port':1883,'username':'','password':'','use_tls':False}}))
    result=protocol.make_result(original,{'status':'accepted','power_state':None})
    assert protocol.validate_result(result.message,original).message['result']['power_state'] is None


@pytest.mark.parametrize('field,value',[('port',True),('port',0),('port',65536),('host','host/path'),
    ('username',None),('password',1),('use_tls',1),('extra',True)])
def test_malformed_broker_snapshot(field,value):
    raw=request('power_on',{'type':'mqtt','entity_id':'test/power',
        'connection':{'host':'127.0.0.1','port':1883,'username':'','password':'','use_tls':False}})
    raw['inputs']['connection'][field]=value
    with pytest.raises(ProtocolError):protocol.validate_request(raw)


@pytest.mark.parametrize('field',['job_id','printer_id','owner_org_id'])
def test_result_numeric_identity_type_substitution(field):
    raw=request();raw['identity']['owner_org_id']=3
    original=protocol.validate_request(raw)
    result=protocol.make_result(original,{'idle':True}).message
    result['identity'][field]=float(raw['identity'][field])
    with pytest.raises(ProtocolError):protocol.validate_result(result,original)


def test_result_deadline_type_substitution():
    raw=request();raw['deadline']=int(raw['deadline'])
    original=protocol.validate_request(raw);result=protocol.make_result(original,{'idle':True}).message
    result['deadline']=float(raw['deadline'])
    with pytest.raises(ProtocolError):protocol.validate_result(result,original)


def test_result_late_after_success_refused(monkeypatch):
    raw=request();original=protocol.validate_request(raw)
    result=protocol.make_result(original,{'idle':True}).message
    monkeypatch.setattr(protocol.time,'monotonic',lambda:raw['deadline'])
    with pytest.raises(ProtocolError):protocol.validate_result(result,original)


@pytest.mark.parametrize('operation,inputs',[
    ('status_idle',{'type':'tasmota','host':'127.0.0.1'}),
    ('power_on',{'api_type':'moonraker','ip':'127.0.0.1'}),
])
def test_operation_input_mismatch(operation,inputs):
    with pytest.raises(ProtocolError):protocol.validate_request(request(operation,inputs))


def test_validated_request_access_cannot_redefine_result_anchor():
    saved=protocol.validate_request(request())
    exposed=saved.message;exposed['identity']['job_id']=99
    result=protocol.make_result(saved,{'idle':True}).message
    assert result['identity']['job_id']==2
    result['identity']['job_id']=99
    with pytest.raises(ProtocolError):protocol.validate_result(result,saved)


@pytest.mark.parametrize('field,value',[('username','bad\x00'),('password','bad\n')])
def test_mqtt_credential_controls_refused_before_worker(field,value):
    raw=request('power_on',{'type':'mqtt','entity_id':'test/power',
        'connection':{'host':'127.0.0.1','port':1883,'username':'','password':'','use_tls':False}})
    raw['inputs']['connection'][field]=value
    with pytest.raises(ProtocolError):protocol.validate_request(raw)
