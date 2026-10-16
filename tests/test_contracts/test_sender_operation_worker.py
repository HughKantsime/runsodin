"""Fixed worker code with synthetic protocols and owned loopback peers only."""
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import pytest
from core import sender_ipc as ipc
from core import sender_operation_protocol as protocol
from core import sender_operation_worker as worker
from core.sender_process_group import OwnedSenderGroup
from tests.test_contracts.test_sender_operation_protocol import request
from tests.test_contracts.test_sender_process_group import cleanup_for_test


@pytest.mark.parametrize('api',['bambu','moonraker','prusalink'])
def test_fixed_readiness_handler(api,monkeypatch):
    if api=='bambu':
        inputs={'api_type':api,'ip':'127.0.0.1','serial':'TEST','access_code':''}
        target='modules.printers.dispatch_bambu_readiness.fresh_bambu_status_is_idle'
    else:
        inputs={'api_type':api,'ip':'127.0.0.1'}
        target='modules.printers.dispatch_readiness.fresh_http_status_is_idle'
    calls=[]
    def acquire(actual,deadline):calls.append(actual);return True
    monkeypatch.setattr(target,acquire)
    raw=request(inputs=inputs)
    result=worker.execute_request(raw)
    assert result.message['result']=={'idle':True} and calls==[inputs]
    assert protocol.validate_result(result.message,protocol.validate_request(raw))


def test_malformed_request_does_not_enter_handler(monkeypatch):
    monkeypatch.setattr('modules.printers.dispatch_readiness.fresh_http_status_is_idle',lambda *args:pytest.fail('Malformed request acted'))
    raw=request();raw['inputs']['callable']='eval'
    with pytest.raises(ipc.ProtocolError):worker.execute_request(raw)


def test_handler_exception_has_fixed_false_not_private_text(monkeypatch):
    def acquire(*args):raise RuntimeError('private synthetic marker')
    monkeypatch.setattr('modules.printers.dispatch_readiness.fresh_http_status_is_idle',acquire)
    result=worker.execute_request(request())
    assert result.message['result']=={'idle':False}
    assert 'private' not in repr(result)


@pytest.mark.parametrize('kind',['tasmota','homeassistant'])
def test_fixed_http_power_operation(kind,monkeypatch):
    target='modules.printers.plug_http_protocol.'+('tasmota_power' if kind=='tasmota' else 'ha_switch')
    calls=[]
    def power(*args,**kwargs):calls.append(args);return True
    monkeypatch.setattr(target,power)
    inputs={'type':kind,'host':'127.0.0.1'} if kind=='tasmota' else {'type':kind,'host':'http://127.0.0.1','entity_id':'switch.test','auth_token':''}
    result=worker.execute_request(request('power_on',inputs))
    assert result.message['result']=={'status':'accepted','power_state':True}
    assert calls[0][-1] == ('ON' if kind=='tasmota' else 'turn_on')


def test_mqtt_snapshot_reconstructed_and_no_physical_observation(monkeypatch):
    calls=[]
    def power(snapshot,topic,deadline):
        calls.append((snapshot,topic));return 'accepted'
    monkeypatch.setattr('core.mqtt_power_protocol.send_owned_mqtt_on',power)
    inputs={'type':'mqtt','entity_id':'test/power','connection':{'host':'127.0.0.1','port':1883,'username':'','password':'','use_tls':False}}
    result=worker.execute_request(request('power_on',inputs))
    assert result.message['result']=={'status':'accepted','power_state':None}
    assert calls[0][0].host=='127.0.0.1' and calls[0][1]=='test/power'


def test_power_cleanup_exception_cannot_be_positive(monkeypatch):
    def power(*args,**kwargs):raise RuntimeError('synthetic cleanup failure')
    monkeypatch.setattr('modules.printers.plug_http_protocol.tasmota_power',power)
    result=worker.execute_request(request('power_on',{'type':'tasmota','host':'127.0.0.1'}))
    assert result.message['result']=={'status':'uncertain','power_state':None}


@pytest.fixture
def peer():
    state={'calls':[]}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):
            state['calls'].append(self.path)
            body=(b'{"POWER":"ON"}' if self.path.startswith('/cm?') else
                b'{"printer":{"state":"IDLE"},"job":null}')
            self.send_response(200);self.send_header('Content-Length',str(len(body)))
            self.end_headers();self.wfile.write(body)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:yield server.server_port,state
    finally:
        server.shutdown();server.server_close();thread.join(timeout=2)
        assert not thread.is_alive()


@pytest.mark.parametrize('operation',['status_idle','power_on'])
def test_actual_fixed_owned_worker_through_private_frames(peer,operation,monkeypatch):
    port,state=peer
    # The device double listens on 127.0.0.1, which production blocks.
    from tests.support.loopback_devices import allow_loopback_devices
    allow_loopback_devices(monkeypatch)
    inputs=({'api_type':'prusalink','ip':'127.0.0.1','port':port,'api_key':''} if operation=='status_idle'
        else {'type':'tasmota','host':f'127.0.0.1:{port}'})
    raw=request(operation,inputs);raw['deadline']=time.monotonic()+5
    validated=protocol.validate_request(raw)
    r,w=os.pipe();rr,rw=os.pipe();group=None
    for fd in (r,w,rr,rw):os.set_blocking(fd,False)
    try:
        group=OwnedSenderGroup([sys.executable,'-m','core.sender_operation_worker','{ODIN_INPUT_FD}','{ODIN_RESULT_FD}'],
            defer_start=True,channels={'input':r,'result':rw})
        os.close(r);r=None;os.close(rw);rw=None
        assert state['calls']==[]
        assert group.arm().worker_started
        ipc.send(w,validated.message,raw['deadline']);os.close(w);w=None
        result=protocol.validate_result(ipc.receive(rr,raw['deadline']),validated)
        expected={'idle':True} if operation=='status_idle' else {'status':'accepted','power_state':True}
        assert result.message['result']==expected
        assert len(state['calls'])==1
        assert group.wait_worker(raw['deadline']).worker_exited_normally
        assert group.retire().group_empty
    finally:
        if group is not None:group.retire();cleanup_for_test(group)
        for fd in (r,w,rr,rw):
            if fd is not None:os.close(fd)


def test_status_cleanup_failure_after_idle_observation(monkeypatch):
    closed=[]
    class Response:
        status_code=200
        def __enter__(self):return self
        def __exit__(self,*args):closed.append('response');raise RuntimeError('synthetic cleanup marker')
        def iter_content(self,**kwargs):yield b'{"printer":{"state":"IDLE"},"job":null}'
    class Client:
        def __enter__(self):return self
        def __exit__(self,*args):closed.append('client')
        def get(self,*args,**kwargs):return Response()
    monkeypatch.setattr('requests.Session',Client)
    raw=request(inputs={'api_type':'prusalink','ip':'127.0.0.1','api_key':''})
    assert worker.execute_request(raw).message['result']=={'idle':False}
    assert closed==['response','client']


def test_mqtt_cleanup_failure_after_puback(monkeypatch):
    from types import SimpleNamespace
    calls=[]
    class Client:
        def __init__(self,*args,**kwargs):pass
        def connect(self,*args,**kwargs):return 0
        def loop_start(self):self.on_connect(self,None,None,0);return 0
        def publish(self,*args,**kwargs):
            calls.append('published')
            return SimpleNamespace(rc=0,wait_for_publish=lambda **kwargs:None,is_published=lambda:True)
        def disconnect(self):calls.append('disconnect');raise RuntimeError('synthetic cleanup marker')
        def loop_stop(self):calls.append('stop')
    monkeypatch.setattr('paho.mqtt.client.Client',Client)
    raw=request('power_on',{'type':'mqtt','entity_id':'test/power',
        'connection':{'host':'127.0.0.1','port':1883,'username':'','password':'','use_tls':False}})
    assert worker.execute_request(raw).message['result']=={'status':'uncertain','power_state':None}
    assert calls==['published','disconnect','stop']
