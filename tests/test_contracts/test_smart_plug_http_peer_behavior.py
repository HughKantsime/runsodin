"""Real plug HTTP transport against owned peers; no physical plug commands."""
import threading
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import pytest


@pytest.fixture(autouse=True)
def _loopback_plug_peers(monkeypatch):
    # The plug peers in this file listen on 127.0.0.1, which the production
    # device destination policy blocks; exempt exactly that address here
    # (and in spawned workers). test_cert_A_destination_policy covers the policy.
    from tests.support.loopback_devices import allow_loopback_devices
    allow_loopback_devices(monkeypatch)
from modules.printers import smart_plug, plug_http_protocol


@pytest.fixture(params=[smart_plug, plug_http_protocol], ids=['manual-wrapper','worker-protocol'])
def protocol(request):
    return request.param


@pytest.fixture
def plug_peer(monkeypatch):
    state={'redirect':False,'source':[],'sink':[],'body':None,'proxy_port':None,'response_gate':None}
    class Sink(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):
            state['sink'].append(self.headers.get('Authorization'))
            self.send_response(200);self.end_headers();self.wfile.write(b'{"POWER":"ON","state":"on"}')
    sink=ThreadingHTTPServer(('127.0.0.1',0),Sink)
    class Source(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):self.do_GET()
        def do_GET(self):
            length=int(self.headers.get('Content-Length','0'))
            if length:self.rfile.read(length)
            state['source'].append((self.command,self.path,self.headers.get('Authorization')))
            if state['response_gate'] is not None:
                state['response_gate'].wait(timeout=10)
            if state['redirect'] and not self.path.startswith('/api/states/'):
                self.send_response(302);self.send_header('Location',f'http://127.0.0.1:{sink.server_port}/unexpected');self.end_headers();return
            self.send_response(200);self.end_headers()
            try:
                self.wfile.write(state['body'] if state['body'] is not None else (b'{"state":"on"}' if self.path.startswith('/api/states/') else b'{"POWER":"ON"}'))
            except (BrokenPipeError, ConnectionResetError):
                pass
    source=ThreadingHTTPServer(('127.0.0.1',0),Source)
    state['proxy_port']=sink.server_port
    threads=[threading.Thread(target=server.serve_forever,daemon=True) for server in (source,sink)]
    for thread in threads:thread.start()
    monkeypatch.setattr('core.itar.enforce_request_destination',lambda url:None)
    try:yield state,f'127.0.0.1:{source.server_port}'
    finally:
        for server in (source,sink):server.shutdown();server.server_close()
        for thread in threads:thread.join(timeout=3);assert not thread.is_alive()


def test_actual_tasmota_on_request_and_observed_state(plug_peer,protocol):
    state,host=plug_peer
    assert protocol.tasmota_power(host,'ON') is True
    assert state['source']==[('GET','/cm?cmnd=Power%20ON',None)]
    assert state['sink']==[]


def test_actual_ha_service_and_readback_authentication(plug_peer,protocol):
    state,host=plug_peer
    assert protocol.ha_switch('http://'+host,'switch.synthetic','synthetic-token','turn_on') is True
    assert state['source']==[('POST','/api/services/switch/turn_on','Bearer synthetic-token'),('GET','/api/states/switch.synthetic','Bearer synthetic-token')]
    assert state['sink']==[]


@pytest.mark.parametrize('kind',['tasmota','homeassistant'])
def test_plug_command_redirect_refused_without_second_origin(plug_peer,protocol,kind):
    state,host=plug_peer;state['redirect']=True
    result=protocol.tasmota_power(host,'ON') if kind=='tasmota' else protocol.ha_switch('http://'+host,'switch.synthetic','synthetic-token','turn_on')
    assert result is None
    assert state['sink']==[]
    assert len(state['source'])==1


@pytest.mark.parametrize('body',[b'x'*65537,b'\xff\xfe'])
def test_owned_invalid_response_is_unconfirmed_without_body_echo(plug_peer,protocol,body):
    state,host=plug_peer;state['body']=body
    status,message=getattr(protocol,'_http_request',getattr(protocol,'http_request',None))('http://'+host+'/status')
    assert status==0
    assert message in ('HTTP response exceeds limit','HTTP request failed')
    assert len(state['source'])==1
    assert state['sink']==[]


def test_environment_proxy_cannot_receive_plug_credentials(plug_peer,protocol,monkeypatch):
    state,host=plug_peer
    for key in ('no_proxy','NO_PROXY'):
        monkeypatch.setenv(key,'')
    for key in ('http_proxy','HTTP_PROXY'):
        monkeypatch.setenv(key,f"http://127.0.0.1:{state['proxy_port']}")
    assert protocol.ha_switch('http://'+host,'switch.synthetic','synthetic-token','turn_on') is True
    assert len(state['source'])==2
    assert state['sink']==[]


def test_protocol_import_has_no_database_or_runtime_side_effects():
    import os
    import subprocess
    import sys
    from pathlib import Path
    root=Path(__file__).resolve().parents[2]
    script="""
import sys
from modules.printers import plug_http_protocol
for name in ('core.db','core.crypto','modules.printers.smart_plug','modules.notifications.mqtt_republish','sqlalchemy'):
    assert name not in sys.modules, name
"""
    result=subprocess.run([sys.executable,'-c',script],cwd=root,env={**os.environ,'PYTHONPATH':str(root/'backend')},capture_output=True,text=True,timeout=10)
    assert result.returncode==0,result.stderr
