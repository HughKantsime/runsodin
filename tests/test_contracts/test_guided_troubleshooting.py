"""Executable security and failure-path contracts for guided troubleshooting."""
import ftplib
import ipaddress
import secrets
import socket
from types import SimpleNamespace
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from modules.system import troubleshooting as t
from modules.system.sanitize import sanitize_log_line, sanitize_structure

@pytest.mark.parametrize('template',[
    'Authorization: Bearer {}', '{{"Authorization": "Bearer {}"}}', 'Authorization: Basic {}', 'access_code={}',
    '{{"access_code": "{}"}}', "password='{}'", 'PASS {}', 'USER {}',
    'http://user:{}@example.test', 'api_key={}', 'token={}',
])
def test_credentials_filtered(template):
    secret=secrets.token_urlsafe(24)
    assert secret not in sanitize_log_line(template.format(secret))


def test_error_buffer_recursive_filter():
    secret=secrets.token_urlsafe(24)
    assert secret not in str(sanitize_structure({'token':secret,'Authorization':'Bearer '+secret,'detail':[f'PASS {secret}']}))

@pytest.mark.parametrize('address',['127.0.0.1','169.254.169.254','::1','::ffff:127.0.0.1','fe80::1','0.0.0.0','224.0.0.1'])
def test_dns_resolution_rejects_forbidden_destinations(monkeypatch,address):
    monkeypatch.setattr(t,'resolve_addresses',lambda *a:[(None,None,None,None,(address,0))])
    with pytest.raises(ValueError): t.safe_destination('saved-printer.test')


def test_dns_safe_address_is_pinned(monkeypatch):
    monkeypatch.setattr(t,'resolve_addresses',lambda *a:[(None,None,None,None,('192.168.40.20',0))])
    assert t.safe_destination('printer.test:7125')==('192.168.40.20',7125)


class Peer:
    def __init__(self,fail=None): self.sock=None;self.fail=fail;self.commands=[];self.data=b'';self.closed=False
    def maybe(self,stage):
        if self.fail==stage: raise ftplib.error_perm('530 rejected') if stage=='auth' else OSError('secret must not leave server')
    def connect(self,**kw):self.maybe('tls')
    def login(self,**kw):self.maybe('auth')
    def prot_p(self):self.maybe('protection')
    def set_pasv(self,value):pass
    def storbinary(self,command,stream,**kw):self.commands.append(command);self.maybe('upload');self.data=stream.read()
    def retrbinary(self,command,callback,**kw):self.commands.append(command);self.maybe('retrieve');callback(self.data if self.fail!='mismatch' else b'wrong')
    def delete(self,name):self.commands.append('DELE '+name);self.maybe('cleanup');return '250 Deleted'
    def close(self):self.closed=True

@pytest.mark.parametrize('stage',['tls','auth','protection','upload','retrieve','mismatch','cleanup',None])
def test_transfer_stages_and_cleanup_are_honest(stage):
    peer=Peer(stage);results=t.ftps_checks('192.168.40.20',secrets.token_urlsafe(24),True,lambda:peer)
    assert peer.closed
    assert 'secret must not leave server' not in str(results)
    if stage is None: assert all(r['status']=='pass' for r in results)
    else: assert any(r['status']=='fail' for r in results)
    if stage in ('upload','retrieve','mismatch','cleanup',None):
        assert any(c.startswith('DELE odin-diagnostic-') for c in peer.commands)
        paths=[c.split(' ',1)[1] for c in peer.commands]
        assert len(set(paths))==1 and paths[0].endswith('.txt')
    assert not any('print' in c.lower() for c in peer.commands)


def test_control_check_never_writes_file():
    peer=Peer();t.ftps_checks('192.168.40.20',secrets.token_urlsafe(24),False,lambda:peer)
    assert peer.commands==[]


def test_lock_rejects_concurrent_diagnostic():
    identifier=secrets.randbelow(1000000000)
    with t.printer_lock(identifier):
        with pytest.raises(Exception) as error:
            with t.printer_lock(identifier):pass
        assert error.value.status_code==409

@pytest.mark.parametrize('principal',[None,{'role':'viewer'}, {'role':'operator'}, {'role':'admin','group_id':3}])
@pytest.mark.parametrize('path',['server','printer','report'])
def test_auth_denies_non_superadmin_before_checks(principal,path,monkeypatch):
    from core.dependencies import get_current_user
    from core.db import get_db
    app=FastAPI();app.include_router(t.router)
    app.dependency_overrides[get_current_user]=lambda:principal
    app.dependency_overrides[get_db]=lambda:None
    monkeypatch.setattr(t,'server_checks',lambda *_:pytest.fail('unauthorized check'))
    monkeypatch.setattr(t,'printer_checks',lambda *_:pytest.fail('unauthorized network'))
    result=TestClient(app).post('/system/troubleshooting/'+path,json={'printer_id':1})
    assert result.status_code in (401,403)


def test_report_filters_description_and_logs_opt_in(monkeypatch):
    monkeypatch.setattr(t,'server_checks',lambda _:t.envelope([]))
    secret=secrets.token_urlsafe(24)
    result=t.support_report(t.ReportRequest(problem='access_code='+secret),{},None)
    assert secret not in str(result) and 'recent_logs' not in result


def test_real_tls_transfer_roundtrip_and_fresh_cleanup(tmp_path):
    """No mocked sockets: encrypted upload/download plus a new delete session."""
    import ssl
    import threading
    from tests.test_contracts.test_bambu_ftps_transfer import _server_context
    from modules.printers.telemetry.bambu.ftp_upload import _ImplicitFTPS
    tls=_server_context(tmp_path)
    tls.minimum_version=tls.maximum_version=ssl.TLSVersion.TLSv1_2
    listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen();listener.settimeout(5)
    port=listener.getsockname()[1];files={};commands=[];errors=[];sessions=[]
    def serve():
        try:
            for _ in range(2):
                raw,_=listener.accept()
                with tls.wrap_socket(raw,server_side=True) as control:
                    control.settimeout(5);stream=control.makefile('r');control.sendall(b'220 Test FTPS\r\n');passive=None
                    try:
                        while line:=stream.readline():
                            verb,_,arg=line.strip().partition(' ');commands.append(verb)
                            if verb=='USER':response='331 Password'
                            elif verb=='PASS':response='230 Accepted'
                            elif verb in ('PBSZ','PROT','TYPE'):response='200 Accepted'
                            elif verb=='PASV':
                                passive=socket.socket();passive.bind(('127.0.0.1',0));passive.listen();passive.settimeout(5)
                                p=passive.getsockname()[1];response=f'227 Passive (127,0,0,1,{p//256},{p%256})'
                            elif verb in ('STOR','RETR'):
                                assert arg.startswith('odin-diagnostic-') and arg.endswith('.txt')
                                control.sendall(b'150 Data\r\n');data,_=passive.accept()
                                with tls.wrap_socket(data,server_side=True) as encrypted:
                                    encrypted.settimeout(5);sessions.append(encrypted.session_reused)
                                    assert encrypted.session_reused
                                    if verb=='STOR':
                                        chunks=[]
                                        while chunk:=encrypted.recv(1024):chunks.append(chunk)
                                        files[arg]=b''.join(chunks)
                                    else: encrypted.sendall(files[arg])
                                    encrypted.unwrap().close()
                                passive.close();passive=None;response='226 Done'
                            elif verb=='DELE':
                                assert arg in files;del files[arg];response='250 Removed'
                            else:response='502 Unsupported'
                            control.sendall((response+'\r\n').encode())
                    finally:
                        stream.close()
                        if passive:passive.close()
        except Exception as error:errors.append(type(error).__name__)
        finally:listener.close()
    thread=threading.Thread(target=serve,daemon=True);thread.start()
    class LocalFTPS(_ImplicitFTPS):
        def connect(self,host='',**kwargs): return super().connect(host,port=port,**kwargs)
    results=t.ftps_checks('127.0.0.1',secrets.token_urlsafe(24),True,LocalFTPS)
    thread.join(6)
    assert not thread.is_alive() and not errors
    assert all(r['status']=='pass' for r in results),results
    assert not files and sessions==[True,True]
    assert commands.count('USER')==2 and commands.count('DELE')==1


def test_read_only_admin_token_cannot_run_printer_diagnostic(monkeypatch):
    from core.dependencies import get_current_user
    from core.db import get_db
    app=FastAPI();app.include_router(t.router)
    app.dependency_overrides[get_current_user]=lambda:{'role':'admin','group_id':None,'_token_scopes':['read']}
    app.dependency_overrides[get_db]=lambda:None
    monkeypatch.setattr(t,'printer_checks',lambda *_:pytest.fail('read-only token must not reach network'))
    assert TestClient(app).post('/system/troubleshooting/printer',json={'printer_id':1}).status_code==403


def test_legacy_zip_filters_recent_errors_and_nested_config(monkeypatch):
    import asyncio,io,json,zipfile
    from modules.system import routes_admin,diagnostics
    secret=secrets.token_urlsafe(24)
    class Result:
        def __init__(self,rows=()):self.rows=rows
        def scalar(self):return 0
        def fetchall(self):return self.rows
    class DB:
        def execute(self,statement):
            query=str(statement)
            return Result([('smtp_url','http://user:'+secret+'@example.test')]) if 'system_config' in query else Result()
    monkeypatch.setattr(diagnostics,'_read_tail',lambda *_:[f'ERROR {{"Authorization": "Bearer {secret}"}}',f'WARNING PASS {secret}'])
    monkeypatch.setattr(routes_admin,'sql',SimpleNamespace(is_sqlite=False))
    response=routes_admin.download_support_bundle({},DB())
    async def collect():return b''.join([chunk async for chunk in response.body_iterator])
    archive=zipfile.ZipFile(io.BytesIO(asyncio.run(collect())))
    content='\n'.join(archive.read(name).decode() for name in archive.namelist())
    assert secret not in content and '[REDACTED]' in content


def test_report_logs_are_fixed_bounded_and_filtered(monkeypatch):
    from modules.system import diagnostics
    secret=secrets.token_urlsafe(24);paths=[]
    monkeypatch.setattr(t,'server_checks',lambda _:t.envelope([]))
    def tail(path,limit):
        paths.append((path,limit));return ['PASS '+secret]
    monkeypatch.setattr(diagnostics,'_read_tail',tail)
    result=t.support_report(t.ReportRequest(include_logs=True),{},None)
    assert len(paths)==6 and all(limit==100 for _,limit in paths)
    assert secret not in str(result)
    assert set(result['recent_logs'])=={'backend','mqtt_monitor','moonraker_monitor','prusalink_monitor','elegoo_monitor','supervisord'}


def test_existing_log_viewer_remains_working_and_filters_secrets(tmp_path,monkeypatch):
    from modules.system import routes_admin
    secret=secrets.token_urlsafe(24);path=tmp_path/'backend.log';path.write_text('ERROR PASS '+secret+'\n')
    monkeypatch.setitem(routes_admin._LOG_FILES,'backend',str(path))
    result=routes_admin.get_logs('backend',200,None,None,{})
    assert result['lines'] and secret not in str(result)
