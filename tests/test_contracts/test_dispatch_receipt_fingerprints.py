"""Identity evidence must change for every plug/broker/name authority input."""
import secrets
from dataclasses import replace
import pytest
from core.dispatch_receipts import power_fingerprint,filename_fingerprint,generic_filename_token
from core.mqtt_connection_snapshot import MqttConnectionSnapshot

@pytest.mark.parametrize('field,value',[
    ('plug_type','mqtt'),('plug_host','new.example.test'),('plug_entity_id','changed'),
    ('plug_auth_token','synthetic replacement'),('plug_auto_on',False),
])
def test_each_raw_plug_input_changes_prepared_power_identity(field,value):
    key=secrets.token_bytes(32);token='a'*32
    plug={'plug_type':'tasmota','plug_host':'owned.example.test','plug_entity_id':None,'plug_auth_token':'synthetic original','plug_auto_on':True}
    assert power_fingerprint(key,token,plug,None)!=power_fingerprint(key,token,{**plug,field:value},None)

@pytest.mark.parametrize('field,value',[
    ('host','new.example.test'),('port',8883),('username','replacement'),
    ('password','synthetic replacement'),('use_tls',True),
])
def test_each_broker_input_changes_prepared_power_identity(field,value):
    key=secrets.token_bytes(32);broker=MqttConnectionSnapshot('owned.example.test',1883,'synthetic','synthetic original',False)
    assert power_fingerprint(key,'a'*32,{},broker)!=power_fingerprint(key,'a'*32,{},replace(broker,**{field:value}))


def test_attempt_key_filename_and_purpose_are_separate():
    key=secrets.token_bytes(32);name='odin-dispatch-'+'a'*32+'.gcode'
    baseline=filename_fingerprint(key,'a'*32,name)
    assert baseline!=filename_fingerprint(key,'a'*32,name+'.3mf')
    assert baseline!=filename_fingerprint(key,'b'*32,name)
    assert baseline!=filename_fingerprint(secrets.token_bytes(32),'a'*32,name)
    assert baseline!=power_fingerprint(key,'a'*32,{},None)

@pytest.mark.parametrize('name',[None,42,'a.gcode','/path/odin-dispatch-'+'a'*32+'.gcode','odin-dispatch-'+'A'*32+'.gcode','odin-dispatch-'+'a'*32+'.gcode.extra','odin-dispatch-'+'a'*32+'.exe','odin-'+'a'*32+'.3mf'])
def test_generic_namespace_requires_complete_validated_shape(name):
    assert generic_filename_token(name) is None

@pytest.mark.parametrize('suffix',['.gcode','.bgcode','.3mf'])
def test_supported_exact_generic_suffixes(suffix):
    assert generic_filename_token('odin-dispatch-'+'a'*32+suffix)=='a'*32


@pytest.mark.parametrize('namespace',['odin-','odin-dispatch-'])
@pytest.mark.parametrize('suffix',['.3mf','.gcode','.bgcode'])
def test_bambu_exact_extensionless_nonce_requires_original_hmac(namespace,suffix):
    from core.dispatch_receipts import observed_filename_matches
    key=secrets.token_bytes(32);token='a'*32;name=namespace+'b'*32
    expected=filename_fingerprint(key,token,name+suffix)
    assert observed_filename_matches(key,token,'bambu',name,expected)
    assert not observed_filename_matches(key,'c'*32,'bambu',name,expected)
    assert not observed_filename_matches(key,token,'moonraker',name,expected)
    assert not observed_filename_matches(key,token,'prusalink',name,expected)
    assert not observed_filename_matches(key,token,'bambu',name+'.exe',expected)
    assert not observed_filename_matches(key,token,'bambu',name+suffix+'.gcode',expected)
    other=next(s for s in ('.3mf','.gcode','.bgcode') if s!=suffix)
    assert not observed_filename_matches(key,token,'bambu',name+other,expected)


@pytest.mark.parametrize('name',[None,42,'pilot','odin-'+'A'*32,'/path/odin-'+'a'*32,'prefix-odin-'+'a'*32,'odin-'+'a'*32+'-suffix'])
def test_no_bambu_extension_reconstruction_for_other_names(name):
    from core.dispatch_receipts import observed_filename_matches
    key=secrets.token_bytes(32)
    expected=filename_fingerprint(key,'b'*32,str(name)+'.3mf')
    assert not observed_filename_matches(key,'b'*32,'bambu',name,expected)


def test_generic_admission_remains_strict_while_bambu_wire_token_can_omit_suffix():
    from core.dispatch_receipts import generic_observed_token
    name='odin-dispatch-'+'a'*32
    assert generic_filename_token(name) is None
    assert generic_observed_token('bambu',name)=='a'*32
    assert generic_observed_token('moonraker',name) is None
    assert generic_observed_token('prusalink',name) is None
    assert generic_observed_token('bambu',name+'.exe') is None
