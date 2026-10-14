"""Pure configured-target equality and credential-safe fingerprints."""
import pytest
from core.dispatch_identity import build_identity

KEY=b'synthetic fingerprint key material for tests only'


def creds(**changes):
    value={'api_type':'moonraker','ip':'Printer.EXAMPLE.test.','port':80,'is_active':True,'org_id':1,'api_key':'synthetic private marker','bed_x_mm':256,'bed_y_mm':256,'nozzle_diameter':.4,'active_materials':['PLA','PETG']}
    value.update(changes);return value


@pytest.mark.parametrize('left,right',[('Printer.EXAMPLE.test.','printer.example.test'),('[2001:0db8::1]','2001:db8:0:0:0:0:0:1')])
def test_canonical_configured_origin_aliases(left,right):
    assert build_identity(creds(ip=left),KEY)==build_identity(creds(ip=right,port='80'),KEY)


def test_http_protocols_same_origin_have_shared_target_but_different_config():
    left=build_identity(creds(),KEY);right=build_identity(creds(api_type='prusalink'),KEY)
    assert left.target_key==right.target_key and left.configuration_key!=right.configuration_key


def test_bambu_serial_case_does_not_mutate_transport_snapshot():
    data=creds(api_type='bambu',serial='Synthetic-01')
    assert build_identity(data,KEY).target_key==build_identity(creds(api_type='bambu',serial='SYNTHETIC-01',ip='another.test'),KEY).target_key
    assert data['serial']=='Synthetic-01'


@pytest.mark.parametrize('changes',[{'ip':'http://localhost'},{'ip':'user@localhost'},{'ip':'localhost/path'},{'ip':'localhost?query'},{'ip':'localhost#fragment'},{'ip':' localhost'},{'ip':'localhost:80'},{'ip':'bad\nhost'},{'port':True},{'port':0},{'port':65536},{'scheme':'ftp'},{'is_active':1},{'org_id':False},{'bed_x_mm':float('nan')},{'bed_y_mm':float('inf')},{'nozzle_diameter':True},{'api_key':object()},{'active_materials':[object()]}])
def test_invalid_origin_or_typed_configuration_refuses(changes,caplog):
    assert build_identity(creds(**changes),KEY) is None
    assert 'synthetic private marker' not in caplog.text


@pytest.mark.parametrize('changes',[{'api_key':'changed synthetic'},{'password':'changed synthetic'},{'org_id':2},{'is_active':False},{'bed_x_mm':255},{'nozzle_diameter':.6},{'active_materials':['ABS']}])
def test_configuration_changes_invalidate_key(changes):
    assert build_identity(creds(),KEY).configuration_key!=build_identity(creds(**changes),KEY).configuration_key


def test_null_zero_order_and_secret_key_semantics():
    assert build_identity(creds(bed_x_mm=None),KEY)!=build_identity(creds(bed_x_mm=0),KEY)
    assert build_identity(creds(active_materials=['PETG','PLA','PLA']),KEY)==build_identity(creds(),KEY)
    assert build_identity(creds(),KEY)!=build_identity(creds(),b'another synthetic fingerprint key material')
    assert 'synthetic private marker' not in repr(build_identity(creds(),KEY))


@pytest.mark.parametrize('key',[None,b'',b'short','not bytes'])
def test_missing_key_never_falls_back_to_plain_digest(key):
    assert build_identity(creds(),key) is None


@pytest.mark.parametrize('host',['[localhost]','[127.0.0.1]','localhost..','[2001:db8::1','2001:db8::1]','[fe80::1%en0]'])
def test_invalid_bracket_or_terminal_dot_spellings_refuse(host):
    assert build_identity(creds(ip=host),KEY) is None


def test_configuration_fingerprint_is_domain_separated(monkeypatch):
    import core.dispatch_identity as identity_module
    calls=[]
    original=identity_module.hmac.digest
    def record(key,message,digest):
        calls.append((key,message,digest))
        return original(key,message,digest)
    monkeypatch.setattr(identity_module.hmac,'digest',record)
    result=build_identity(creds(),KEY)
    assert result is not None
    assert calls[0]==(KEY,b'ODIN dispatch configuration key v1','sha256')
    assert calls[1][0]==original(KEY,b'ODIN dispatch configuration key v1','sha256')
    assert calls[1][1].startswith(b'ODIN dispatch configuration v1\x00')
    assert result.configuration_key==original(*calls[1]).hex()


def test_bambu_serial_case_change_invalidates_exact_transport_configuration():
    left=build_identity(creds(api_type='bambu',serial='Synthetic-01'),KEY)
    right=build_identity(creds(api_type='bambu',serial='SYNTHETIC-01'),KEY)
    assert left.target_key==right.target_key
    assert left.configuration_key!=right.configuration_key


@pytest.mark.parametrize('field,value',[('plug_host','changed.test'),('plug_auth_token','changed synthetic input'),('plug_type','mqtt'),('plug_entity_id','changed'),('plug_auto_on',False)])
def test_plug_configuration_changes_invalidate_fingerprint(field,value):
    original={'plug_type':'tasmota','plug_host':'plug.test','plug_auth_token':None,'plug_entity_id':None,'plug_auto_on':True}
    left=build_identity(creds(plug_configuration=original),KEY)
    right=build_identity(creds(plug_configuration={**original,field:value}),KEY)
    assert left is not None and right is not None
    assert left.target_key==right.target_key and left.configuration_key!=right.configuration_key


def test_explicit_sharing_flag_is_typed_and_bound():
    assert build_identity(creds(shared=True),KEY).configuration_key!=build_identity(creds(shared=False),KEY).configuration_key
    assert build_identity(creds(shared=1),KEY) is None
