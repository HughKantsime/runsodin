"""Fresh HTTP acquisition against owned loopback peers and failure boundaries."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
import pytest


@pytest.fixture(autouse=True)
def _loopback_device_peers(monkeypatch):
    # The device peers in this file listen on 127.0.0.1, which the production
    # device destination policy blocks; exempt exactly that address here.
    from tests.support.loopback_devices import allow_loopback_devices
    allow_loopback_devices(monkeypatch)
from modules.printers import dispatch_readiness as module


@pytest.fixture
def peer(monkeypatch):
    state = {'body':b'{"printer":{"state":"IDLE"},"job":null}', 'status':200, 'calls':[]}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_GET(self):
            state['calls'].append((self.path,self.headers.get('X-Api-Key'),self.headers.get('Authorization')))
            status = state['status']
            if state.get('digest') and not self.headers.get('Authorization'):
                status = 401
            self.send_response(status)
            if status == 401:
                self.send_header('WWW-Authenticate','Digest realm="synthetic", nonce="synthetic", algorithm=MD5, qop="auth"')
            if status == 302:
                self.send_header('Location','/unexpected')
            body = state.get('challenge_body',state['body']) if status == 401 else state['body']
            self.send_header('Content-Length',str(len(body)))
            self.end_headers(); self.wfile.write(body)
    server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread = threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    monkeypatch.setattr('core.itar.enforce_request_destination',lambda url:None)
    creds = {'api_type':'prusalink','ip':'127.0.0.1','port':server.server_port,'api_key':'synthetic-key'}
    try: yield state,creds
    finally:
        server.shutdown();server.server_close();thread.join(timeout=3)
        assert not thread.is_alive()


@pytest.mark.parametrize('protocol', ['prusalink','moonraker'])
def test_actual_fresh_http_idle_and_exact_authenticated_origin(peer,protocol):
    state,creds = peer;creds['api_type'] = protocol
    if protocol == 'moonraker':
        state['body'] = json.dumps({'result':{'status':{'webhooks':{'state':'ready'},'print_stats':{'state':'standby'},'virtual_sdcard':{'is_active':False}}}}).encode()
    assert module.fresh_http_status_is_idle(creds,time.monotonic()+3)
    path = '/api/v1/status' if protocol == 'prusalink' else '/printer/objects/query?webhooks&print_stats&virtual_sdcard'
    assert state['calls'] == [(path,'synthetic-key',None)]


def test_actual_digest_challenge_stays_same_status_origin(peer):
    state,creds = peer;state['digest'] = True
    creds.update(api_key='',username='synthetic-user',password='synthetic-password')
    assert module.fresh_http_status_is_idle(creds,time.monotonic()+3)
    assert len(state['calls']) == 2
    assert all(call[0] == '/api/v1/status' and call[1] is None for call in state['calls'])
    assert state['calls'][0][2] is None
    assert state['calls'][1][2].startswith('Digest ')


def test_oversized_digest_challenge_refused_before_authenticated_retry(peer):
    state,creds = peer;state.update(digest=True,challenge_body=b'x'*65537)
    creds.update(api_key='',username='synthetic-user',password='synthetic-password')
    assert not module.fresh_http_status_is_idle(creds,time.monotonic()+3)
    assert len(state['calls']) == 1


@pytest.mark.parametrize('failure', ['redirect','oversize','malformed','busy','unknown','http_error'])
def test_actual_response_refusals_without_redirect_or_fallback(peer,failure):
    state,creds = peer
    if failure == 'redirect':state['status'] = 302
    elif failure == 'oversize':state['body'] = b' '*65537
    elif failure == 'malformed':state['body'] = b'{'
    elif failure == 'busy':state['body'] = b'{"printer":{"state":"PRINTING"}}'
    elif failure == 'unknown':state['body'] = b'{}'
    else:state['status'] = 503
    assert not module.fresh_http_status_is_idle(creds,time.monotonic()+3)
    assert len(state['calls']) == 1


def test_policy_refusal_before_any_network(peer,monkeypatch):
    state,creds = peer
    def deny(url): raise RuntimeError('synthetic private marker')
    monkeypatch.setattr('core.itar.enforce_request_destination',deny)
    assert not module.fresh_http_status_is_idle(creds,time.monotonic()+3)
    assert state['calls'] == []


@pytest.mark.parametrize('field,value', [('ip','user@host'),('ip','host/path'),('ip','host?x'),('ip','host\n'),('port',True),('port',0),('port','80'),('api_key',None),('api_key','bad\nkey'),('scheme','ftp')])
def test_invalid_origin_credentials_refused_before_network(peer,field,value):
    state,creds = peer;creds[field] = value
    assert not module.fresh_http_status_is_idle(creds,time.monotonic()+3)
    assert state['calls'] == []


def test_late_stream_refused_and_resources_closed(monkeypatch):
    clock = [10.0];closed = []
    class Response:
        status_code = 200
        def __enter__(self):return self
        def __exit__(self,*args):closed.append('response')
        def iter_content(self,chunk_size):
            clock[0] = 20.0
            yield b'{"printer":{"state":"IDLE"}}'
    class Client:
        def __enter__(self):return self
        def __exit__(self,*args):closed.append('client')
        def get(self,url,**kwargs):
            assert self.trust_env is False
            assert kwargs['allow_redirects'] is False and kwargs['stream'] is True
            assert kwargs['timeout'] == 5
            return Response()
    monkeypatch.setattr(module.requests,'Session',Client)
    monkeypatch.setattr(module.time,'monotonic',lambda:clock[0])
    monkeypatch.setattr('core.itar.enforce_request_destination',lambda url:None)
    assert not module.fresh_http_status_is_idle({'api_type':'prusalink','ip':'127.0.0.1','port':1,'api_key':'synthetic'},15)
    assert closed == ['response','client']


@pytest.mark.parametrize('deadline', [0,False,None,float('nan'),float('inf'),10**1000])
def test_invalid_or_expired_deadline_never_acquires_client(monkeypatch,deadline):
    def forbidden():raise AssertionError('No network client for invalid deadline')
    monkeypatch.setattr(module.requests,'Session',forbidden)
    assert not module.fresh_http_status_is_idle({'api_type':'prusalink','ip':'127.0.0.1','api_key':'synthetic'},deadline)
