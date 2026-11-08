"""Exact local worker operation schemas. Correlation is not authorization."""
from dataclasses import dataclass,field
import copy
import ipaddress
import math
import re
import time
from urllib.parse import urlsplit
from core.sender_ipc import ProtocolError,_validate

IDENTITY_HEX={'claim_token':32,'registration_key':32,'origin_key':32,
    'runtime_incarnation':32,'physical_key':64,'configuration_key':64,'payload_sha256':64}
IDENTITY_KEYS=set(IDENTITY_HEX)|{'printer_id','job_id','owner_org_id'}
ENVELOPE={'version','operation','operation_id','deadline','identity'}
OPERATIONS={'status_idle','power_on'}


@dataclass(frozen=True)
class ValidatedMessage:
    _message:dict=field(repr=False)

    @property
    def message(self):
        # Callers receive an isolated copy, never the saved correlation anchor.
        return copy.deepcopy(self._message)


def _require(condition):
    if not condition:raise ProtocolError()


def _keys(value,required,optional=()):
    _require(type(value) is dict and set(required)<=set(value)<=set(required)|set(optional))


def _string(value,*,nonempty=False,controls=False,limit=4096):
    _require(type(value) is str and len(value)<=limit and (bool(value) or not nonempty))
    if controls:_require(not any(ord(char)<32 or ord(char)==127 for char in value))


def _host(value):
    _string(value,nonempty=True,controls=True,limit=253)
    if ':' in value:
        try:ipaddress.IPv6Address(value)
        except Exception:raise ProtocolError() from None
    else:_require(re.fullmatch(r'[A-Za-z0-9_.-]+',value) is not None)


def _port(value):_require(type(value) is int and 1<=value<=65535)


def _deadline(value):
    _require(type(value) in (int,float) and math.isfinite(value) and value>time.monotonic())


def _identity(value):
    _keys(value,IDENTITY_KEYS)
    for key,size in IDENTITY_HEX.items():
        _require(type(value[key]) is str and re.fullmatch('[0-9a-f]{%d}'%size,value[key]) is not None)
    for key in ('printer_id','job_id'):_require(type(value[key]) is int and value[key]>0)
    _require(value['owner_org_id'] is None or type(value['owner_org_id']) is int and value['owner_org_id']>0)


def _status_inputs(value):
    _require(type(value) is dict)
    api=value.get('api_type')
    _require(type(api) is str and api in ('bambu','moonraker','prusalink'))
    if api=='bambu':
        _keys(value,{'api_type','ip','serial','access_code'})
        _string(value['serial'],nonempty=True,limit=256)
        _require(re.fullmatch(r'[A-Za-z0-9_-]+',value['serial']) is not None)
        _string(value['access_code'])
    else:
        _keys(value,{'api_type','ip'},{'port','scheme','api_key','username','password'})
        if 'port' in value:_port(value['port'])
        if 'scheme' in value:_require(type(value['scheme']) is str and value['scheme'] in ('http','https'))
        for key in ('api_key','username','password'):
            if key in value:_string(value[key],controls=key=='api_key')
    _host(value['ip'])


def _power_inputs(value):
    _require(type(value) is dict)
    plug=value.get('type');_require(type(plug) is str and plug in ('tasmota','homeassistant','mqtt'))
    if plug=='mqtt':
        _keys(value,{'type','entity_id','connection'})
        _string(value['entity_id'],nonempty=True,controls=True,limit=512)
        _require(value['entity_id']==value['entity_id'].strip() and not any(c in value['entity_id'] for c in '+#'))
        connection=value['connection']
        _keys(connection,{'host','port','username','password','use_tls'})
        _host(connection['host']);_port(connection['port'])
        _string(connection['username'],controls=True);_string(connection['password'],controls=True)
        _require(type(connection['use_tls']) is bool)
    elif plug=='tasmota':
        _keys(value,{'type','host'});_string(value['host'],nonempty=True,controls=True,limit=512)
        url=urlsplit('http://'+value['host'])
        _require(url.hostname is not None and url.username is None and url.password is None
                 and not url.path and not url.query and not url.fragment)
        _host(url.hostname)
        if url.port is not None:_port(url.port)
    else:
        _keys(value,{'type','host','entity_id','auth_token'})
        _string(value['host'],nonempty=True,controls=True,limit=2048)
        url=urlsplit(value['host'])
        _require(url.scheme in ('http','https') and url.hostname is not None and url.username is None
                 and url.password is None and not url.query and not url.fragment)
        _host(url.hostname)
        if url.port is not None:_port(url.port)
        _string(value['entity_id'],nonempty=True,limit=256)
        _require(re.fullmatch(r'switch\.[A-Za-z0-9_]+',value['entity_id']) is not None)
        _string(value['auth_token'],controls=True)


def _envelope(message,body):
    _validate(message);_keys(message,ENVELOPE|{body})
    _require(type(message['version']) is int and message['version']==1)
    _require(type(message['operation']) is str and message['operation'] in OPERATIONS)
    _require(type(message['operation_id']) is str and re.fullmatch('[0-9a-f]{32}',message['operation_id']) is not None)
    _deadline(message['deadline']);_identity(message['identity'])


def validate_request(message):
    try:
        _envelope(message,'inputs')
        (_status_inputs if message['operation']=='status_idle' else _power_inputs)(message['inputs'])
        result=ValidatedMessage(copy.deepcopy(message));_deadline(result.message['deadline'])
        return result
    except Exception:raise ProtocolError() from None


def _result(message,original):
    _envelope(message,'result')
    for key in ENVELOPE:
        _require(type(message[key]) is type(original[key]) and message[key]==original[key])
    # Identity itself was schema checked, including all scalar types.
    result=message['result']
    if original['operation']=='status_idle':
        _keys(result,{'idle'});_require(type(result['idle']) is bool)
    else:
        _keys(result,{'status','power_state'})
        status=result['status'];state=result['power_state']
        _require(type(status) is str and status in ('accepted','refused','uncertain'))
        _require(state is None or type(state) is bool)
        if original['inputs']['type']=='mqtt':_require(state is None)
        elif status=='accepted':_require(state is True)
        elif status=='refused':_require(state is False or state is None)
        if status=='uncertain':_require(state is None)


def validate_result(message,request):
    try:
        _require(type(request) is ValidatedMessage)
        original=validate_request(request.message).message
        _result(message,original)
        result=ValidatedMessage(copy.deepcopy(message));_deadline(original['deadline'])
        return result
    except Exception:raise ProtocolError() from None


def make_result(request,result):
    try:
        original=validate_request(request.message).message
        message={key:copy.deepcopy(original[key]) for key in ENVELOPE}
        message['result']=result
        return validate_result(message,request)
    except Exception:raise ProtocolError() from None
