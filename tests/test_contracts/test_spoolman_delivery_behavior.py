"""Configured Spoolman integration against a real, isolated local HTTP peer."""
import json
import threading
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import pytest

@pytest.fixture
def spoolman_peer(monkeypatch):
    from modules.inventory import services
    received=[];status=[200]
    class Handler(BaseHTTPRequestHandler):
        def do_PUT(self):  # Spoolman declares PUT /api/v1/spool/{id}/use
            received.append((self.path,json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
            self.send_response(status[0])
            if status[0]==302:self.send_header('Location','/redirected-must-not-receive')
            self.end_headers();self.wfile.write(b'{}')
        def log_message(self,*args):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    monkeypatch.setattr(services.settings,'spoolman_url',f'http://127.0.0.1:{server.server_port}')
    monkeypatch.delenv('ODIN_ITAR_MODE',raising=False)
    try:yield services,received,status
    finally:server.shutdown();server.server_close();thread.join(timeout=2)


def test_configured_local_spoolman_receives_fractional_usage(spoolman_peer):
    services,received,_=spoolman_peer
    errors=services.push_consumption_to_spoolman([{'spoolman_spool_id':42,'grams':12.75,'job_id':17}])
    assert errors==[],errors
    assert received==[('/api/v1/spool/42/use',{'use_weight':12.75})]


def test_spoolman_http_failure_is_reported_without_resend(spoolman_peer):
    services,received,status=spoolman_peer;status[0]=503
    errors=services.push_consumption_to_spoolman([{'spoolman_spool_id':42,'grams':12.75,'job_id':17}])
    assert len(errors)==1 and 'HTTP 503' in errors[0],errors
    assert len(received)==1


def test_unlinked_zero_negative_usage_does_not_call_peer(spoolman_peer):
    services,received,_=spoolman_peer
    assert services.push_consumption_to_spoolman([{'spoolman_spool_id':None,'grams':12}, {'spoolman_spool_id':42,'grams':0}, {'spoolman_spool_id':42,'grams':-1}])==[]
    assert received==[]


def test_redirect_is_reported_without_following(spoolman_peer):
    services,received,status=spoolman_peer;status[0]=302
    errors=services.push_consumption_to_spoolman([{'spoolman_spool_id':42,'grams':1,'job_id':17}])
    assert len(errors)==1 and 'HTTP 302' in errors[0]
    assert len(received)==1

@pytest.mark.parametrize('identity',[True,False,-1,0,'42','../../metadata'])
def test_invalid_identity_stops_before_dns(monkeypatch,identity):
    from modules.inventory.services import _post_spoolman_usage
    import socket
    calls=[]
    monkeypatch.setattr(socket,'getaddrinfo',lambda *a,**k:calls.append(a))
    with pytest.raises(ValueError):_post_spoolman_usage('http://spoolman.example.invalid',identity,1)
    assert calls==[]

@pytest.mark.parametrize('address',['169.254.169.254','0.0.0.0','224.0.0.1','fd00:ec2::254','100.100.100.200','::ffff:169.254.169.254'])
def test_forbidden_destination_fails_before_http(monkeypatch,address):
    from modules.inventory.services import _post_spoolman_usage
    from core.webhook_utils import WebhookSSRFError
    import socket,httpx
    monkeypatch.setattr(socket,'getaddrinfo',lambda *a,**k:[(socket.AF_INET,socket.SOCK_STREAM,6,'',(address,80))])
    calls=[];monkeypatch.setattr(httpx,'Client',lambda **k:calls.append(k))
    with pytest.raises(WebhookSSRFError):_post_spoolman_usage('http://configured.example.invalid',42,1)
    assert calls==[]


def test_public_destination_blocked_in_itar_before_http(monkeypatch):
    from modules.inventory.services import _post_spoolman_usage
    from core.webhook_utils import WebhookSSRFError
    import socket,httpx
    monkeypatch.setenv('ODIN_ITAR_MODE','1')
    monkeypatch.setattr(socket,'getaddrinfo',lambda *a,**k:[(socket.AF_INET,socket.SOCK_STREAM,6,'',('8.8.8.8',80))])
    calls=[];monkeypatch.setattr(httpx,'Client',lambda **k:calls.append(k))
    with pytest.raises(WebhookSSRFError):_post_spoolman_usage('http://configured.example.invalid',42,1)
    assert calls==[]


def test_dns_rebinding_is_pinned_to_validated_local_peer(spoolman_peer,monkeypatch):
    services,received,_=spoolman_peer
    from core import webhook_utils
    from urllib.parse import urlsplit
    import socket
    port=urlsplit(services.settings.spoolman_url).port
    monkeypatch.setattr(services.settings,'spoolman_url',f'http://spoolman-peer.example.invalid:{port}')
    calls=[];original=webhook_utils._real_getaddrinfo
    def resolver(host,port,*a,**k):
        if host=='spoolman-peer.example.invalid':
            calls.append(host)
            return [(socket.AF_INET,socket.SOCK_STREAM,6,'',('127.0.0.1' if len(calls)==1 else '169.254.169.254',port))]
        return original(host,port,*a,**k)
    monkeypatch.setattr(webhook_utils,'_real_getaddrinfo',resolver)
    monkeypatch.setenv('HTTP_PROXY','http://127.0.0.1:1')
    assert services.push_consumption_to_spoolman([{'spoolman_spool_id':42,'grams':1,'job_id':17}])==[]
    assert len(calls)==1
    assert received==[('/api/v1/spool/42/use',{'use_weight':1.0})]

@pytest.mark.parametrize('itar,second',[(False,'metadata'),(True,'public')])
def test_mixed_dns_answers_are_all_validated(monkeypatch,itar,second):
    from modules.inventory.services import _post_spoolman_usage
    from core.webhook_utils import WebhookSSRFError
    import socket,httpx
    if itar:monkeypatch.setenv('ODIN_ITAR_MODE','1')
    else:monkeypatch.delenv('ODIN_ITAR_MODE',raising=False)
    bad='8.8.8.8' if second=='public' else '169.254.169.254'
    monkeypatch.setattr(socket,'getaddrinfo',lambda *a,**k:[(socket.AF_INET,socket.SOCK_STREAM,6,'',(ip,80)) for ip in ['127.0.0.1',bad]])
    calls=[];monkeypatch.setattr(httpx,'Client',lambda **k:calls.append(k))
    with pytest.raises(WebhookSSRFError):_post_spoolman_usage('http://configured.example.invalid',42,1)
    assert calls==[]


def test_dual_stack_localhost_is_supported(spoolman_peer,monkeypatch):
    services,received,_=spoolman_peer
    from core import webhook_utils
    from urllib.parse import urlsplit
    import socket
    port=urlsplit(services.settings.spoolman_url).port
    monkeypatch.setattr(services.settings,'spoolman_url',f'http://localhost:{port}')
    original=webhook_utils._real_getaddrinfo
    def resolver(host,port,*args,**kwargs):
        if host=='localhost':return [(socket.AF_INET6,socket.SOCK_STREAM,6,'',('::1',port,0,0)),(socket.AF_INET,socket.SOCK_STREAM,6,'',('127.0.0.1',port))]
        return original(host,port,*args,**kwargs)
    monkeypatch.setattr(webhook_utils,'_real_getaddrinfo',resolver)
    assert services.push_consumption_to_spoolman([{'spoolman_spool_id':42,'grams':1,'job_id':17}])==[]
    assert len(received)==1
