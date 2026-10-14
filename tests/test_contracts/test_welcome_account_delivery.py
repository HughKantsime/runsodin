"""Actual account/mail outcome boundaries, private substituted transports."""
import secrets
from types import SimpleNamespace
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session
from fastapi.testclient import TestClient
from tests.test_contracts.test_password_policy_behavior import policy_client,token_client,review_db,interactive_headers


def body(welcome=True):
    return {'username':'synthetic_welcome_new','email':'welcome@example.invalid',
        'password':secrets.token_urlsafe(24)+'aA1','role':'viewer','send_welcome_email':welcome}


def snapshot(db):
    with Session(db.get_bind()) as fresh:
        return (fresh.execute(text("SELECT COUNT(*) FROM users WHERE username='synthetic_welcome_new'")).scalar_one(),
            fresh.execute(text("SELECT COUNT(*) FROM audit_logs WHERE action='user.created'")).scalar_one())


def test_welcome_only_after_independently_visible_account_and_audit(policy_client,review_db,monkeypatch):
    from modules.organizations import routes_users
    observed=[]
    def mail(*args):observed.append(snapshot(review_db));return True
    monkeypatch.setattr(routes_users,'_send_odin_email',mail)
    response=policy_client.post('/users',headers=interactive_headers(),json=body())
    assert observed==[(1,1)]
    assert response.status_code==200
    assert response.json()=={'status':'created','welcome_email':'sent'}


@pytest.mark.parametrize('outcome',['false','none','exception'])
def test_unconfirmed_mail_leaves_created_account_without_retry(policy_client,review_db,monkeypatch,outcome):
    from modules.organizations import routes_users
    calls=[]
    def mail(*args):
        calls.append('mail')
        if outcome=='exception':raise RuntimeError('synthetic private delivery marker')
        return False if outcome=='false' else None
    monkeypatch.setattr(routes_users,'_send_odin_email',mail)
    response=policy_client.post('/users',headers=interactive_headers(),json=body())
    assert response.status_code==200
    data=response.json()
    assert data['status']=='created' and data.get('welcome_email')=='unconfirmed'
    assert 'created' in data.get('message','').lower() and 'reset' in data.get('message','').lower()
    assert 'private delivery marker' not in response.text
    assert snapshot(review_db)==(1,1) and calls==['mail']


def test_audit_failure_never_sends_welcome(policy_client,review_db,monkeypatch):
    from modules.organizations import routes_users
    calls=[]
    monkeypatch.setattr(routes_users,'_send_odin_email',lambda *args:calls.append('mail') or True)
    def fail(*args,**kwargs):raise RuntimeError('synthetic private audit marker')
    monkeypatch.setattr(routes_users,'log_audit',fail)
    with TestClient(policy_client.app,raise_server_exceptions=False) as client:
        response=client.post('/users',headers=interactive_headers(),json=body())
    assert calls==[]
    assert response.status_code==503 and 'check' in response.json()['detail'].lower()
    assert snapshot(review_db)==(0,0)


@pytest.mark.parametrize('committed',[False,True])
def test_commit_uncertainty_does_not_send_or_duplicate(policy_client,review_db,monkeypatch,committed):
    from core.db import get_db
    from modules.organizations import routes_users
    calls=[]
    monkeypatch.setattr(routes_users,'_send_odin_email',lambda *args:calls.append('mail') or True)
    class CommitFault(Session):
        def commit(self):
            if committed:super().commit()
            raise RuntimeError('synthetic private commit marker')
    def database():
        with CommitFault(review_db.get_bind()) as db:yield db
    policy_client.app.dependency_overrides[get_db]=database
    with TestClient(policy_client.app,raise_server_exceptions=False) as client:
        response=client.post('/users',headers=interactive_headers(),json=body())
    assert calls==[]
    assert response.status_code==503 and 'check' in response.json()['detail'].lower()
    assert 'private commit marker' not in response.text
    assert snapshot(review_db)==((1,1) if committed else (0,0))


def test_no_welcome_keeps_existing_success_shape(policy_client):
    response=policy_client.post('/users',headers=interactive_headers(),json=body(False))
    assert response.status_code==200 and response.json()=={'status':'created'}


def test_welcome_html_escapes_admin_supplied_username(policy_client,monkeypatch):
    from modules.organizations import routes_users
    clean=[]
    def mail(db,to,subject,html):clean.append('<script>' not in html and '&lt;script&gt;' in html);return True
    monkeypatch.setattr(routes_users,'_send_odin_email',mail)
    selected=body();selected['username']='synthetic<script>inert</script>'
    response=policy_client.post('/users',headers=interactive_headers(),json=selected)
    assert response.status_code==200 and clean==[True]


@pytest.mark.parametrize('mode',['send_error','quit_error','close_error','refused','ok'])
def test_owned_smtp_cleanup_and_sanitized_delivery_result(monkeypatch,caplog,mode):
    from modules.organizations import routes_users
    calls=[]
    class Client:
        def __init__(self,*args,**kwargs):calls.append('open')
        def send_message(self,*args):
            calls.append('send')
            if mode=='send_error':raise RuntimeError('synthetic private SMTP marker')
            return {'welcome@example.invalid':(550,b'synthetic')} if mode=='refused' else {}
        def quit(self):
            calls.append('quit')
            if mode=='quit_error':raise RuntimeError('synthetic private SMTP marker')
        def close(self):
            calls.append('close')
            if mode=='close_error':raise RuntimeError('synthetic private SMTP marker')
    monkeypatch.setattr('smtplib.SMTP',Client)
    monkeypatch.setattr('core.itar.enforce_host_destination',lambda *args,**kwargs:None)
    config=SimpleNamespace(value={'enabled':True,'host':'127.0.0.1','use_tls':False})
    query=SimpleNamespace(filter=lambda *args:SimpleNamespace(first=lambda:config))
    db=SimpleNamespace(query=lambda *args:query)
    result=routes_users._send_odin_email(db,'welcome@example.invalid','Synthetic','<p>synthetic</p>')
    assert result is (mode=='ok')
    assert calls[-1]=='close' and calls.count('send')==1
    assert 'private SMTP marker' not in caplog.text


@pytest.mark.parametrize('refuse',[False,True])
def test_real_smtp_loopback_transport_and_recipient_refusal(refuse):
    import socketserver,threading
    from modules.organizations import routes_users
    finished=threading.Event();state={'data':0,'connections':0}
    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            state['connections']+=1;self.request.settimeout(2)
            def reply(value):self.wfile.write(value);self.wfile.flush()
            try:
                reply(b'220 synthetic ESMTP\r\n')
                while True:
                    raw=self.rfile.readline(4096)
                    if not raw:break
                    command=raw.split(b' ',1)[0].strip().upper()
                    if command in (b'EHLO',b'HELO',b'MAIL',b'RSET'):reply(b'250 synthetic\r\n')
                    elif command==b'RCPT':reply(b'550 synthetic refusal\r\n' if refuse else b'250 synthetic\r\n')
                    elif command==b'DATA':
                        reply(b'354 continue\r\n');size=0
                        while True:
                            line=self.rfile.readline(4096)
                            if line==b'.\r\n':break
                            if not line:raise RuntimeError('Incomplete synthetic DATA')
                            size+=len(line)
                            if size>65536:raise RuntimeError('Synthetic DATA oversized')
                        state['data']+=1;reply(b'250 accepted\r\n')
                    elif command==b'QUIT':reply(b'221 goodbye\r\n');break
                    else:reply(b'502 unsupported\r\n')
            finally:finished.set()
    server=socketserver.ThreadingTCPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    config=SimpleNamespace(value={'enabled':True,'host':'127.0.0.1','port':server.server_address[1],'use_tls':False})
    query=SimpleNamespace(filter=lambda *args:SimpleNamespace(first=lambda:config))
    db=SimpleNamespace(query=lambda *args:query)
    try:
        accepted=routes_users._send_odin_email(db,'synthetic@example.invalid','Synthetic','<p>Owned local peer only</p>')
        assert accepted is (not refuse)
        assert finished.wait(2)
        assert state=={'data':0 if refuse else 1,'connections':1}
    finally:
        server.shutdown();server.server_close();thread.join(timeout=2)
        assert not thread.is_alive()
