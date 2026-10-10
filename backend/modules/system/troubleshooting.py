"""Explicit, bounded diagnostics. No diagnostic sends a print command."""
from __future__ import annotations
import fcntl
import hashlib
import io
import ipaddress
import json
import os
import re
import socket
import subprocess
import tempfile
import time
import threading
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session
from core.db import get_db
from core.rbac import require_superadmin, require_scope
from modules.system.sanitize import sanitize_log_line

router = APIRouter(tags=['Troubleshooting'])


def check(stage, status, message, next_step='', **details):
    return dict(stage=stage, status=status, message=message, next_step=next_step, details=details)


def envelope(checks):
    return {'diagnostic_id': uuid.uuid4().hex, 'checked_at': datetime.now(timezone.utc).isoformat(),
            'checks': checks, 'physical_print_verified': False}


def server_checks(db):
    from modules.system.diagnostics import _read_version
    from modules.organizations.services import upload_storage_probe
    UPLOAD_ROOT,storage=upload_storage_probe()
    checks = [check('version', 'pass', 'Application version read.', version=_read_version())]
    try:
        db.execute(text('SELECT 1')).scalar()
        checks.append(check('database', 'pass', 'Database query succeeded.'))
    except Exception:
        checks.append(check('database', 'fail', 'Database query failed.', 'Check database connectivity and server logs.'))
    checks.append(check('upload_storage', 'pass' if storage['status'] == 'ready' else 'fail',
                        'Upload filesystem and reserve measured.' if storage['status'] == 'ready' else 'Upload storage is not ready.',
                        '' if storage['status'] == 'ready' else 'Check the upload volume mount, free space and Education reserve setting.', storage_state=storage['status'], **{k:v for k,v in storage.items() if k!='status'}))
    try:
        # Parent-only capacity measurements must not certify a missing upload mount.
        if not UPLOAD_ROOT.is_dir():
            checks.append(check('upload_write', 'unknown', 'Upload directory does not exist yet.',
                                'Confirm the upload volume configuration; the first upload normally creates its directory.'))
        else:
            with tempfile.TemporaryFile(dir=UPLOAD_ROOT) as stream:
                payload=os.urandom(64);stream.write(payload);stream.flush();os.fsync(stream.fileno());stream.seek(0)
                if stream.read() != payload: raise OSError('write verification failed')
            checks.append(check('upload_write', 'pass', 'Temporary upload write/read check succeeded.'))
    except OSError:
        checks.append(check('upload_write', 'fail', 'Upload directory could not be written and read.', 'Check mount permissions and read-only/full-volume errors.'))
    try:
        result=subprocess.run(['supervisorctl', 'status'],capture_output=True,text=True,timeout=5)
        states=[line.split()[1] for line in result.stdout.splitlines() if len(line.split())>=2]
        healthy=bool(states) and result.returncode==0 and all(s=='RUNNING' for s in states)
        checks.append(check('services', 'pass' if healthy else 'fail',
                            'All supervised services are running.' if healthy else 'One or more supervised services are not running.',
                            '' if healthy else 'Review container service logs.', running=sum(s=='RUNNING' for s in states), total=len(states)))
    except (OSError, subprocess.TimeoutExpired):
        checks.append(check('services', 'unknown', 'Supervisor status could not be read.', 'Check container service status from the host.'))
    return envelope(checks)


def resolve_addresses(name,port):
    """A separate resolver process enforces a deadline even on broken DNS."""
    source="import sys,json,socket; host,port=json.load(sys.stdin); print(json.dumps(socket.getaddrinfo(host,port,type=socket.SOCK_STREAM)))"
    result=subprocess.run([sys.executable,'-c',source],input=json.dumps([name,port]),capture_output=True,text=True,timeout=3,check=True)
    return json.loads(result.stdout)


def safe_destination(host):
    """Resolve once, reject any forbidden result, then use only the pinned IP."""
    if not host or len(host)>255 or any(c in host for c in '/@?#'):
        raise ValueError('Invalid saved printer host')
    # Saved printer endpoints may include an explicit port.
    from urllib.parse import urlsplit
    parsed=urlsplit('//'+host)
    name=parsed.hostname
    if not name or parsed.username or parsed.password: raise ValueError('Invalid saved printer host')
    port=parsed.port
    addresses=resolve_addresses(name,port or 0)
    values=[]
    for address in addresses:
        ip=ipaddress.ip_address(address[4][0])
        effective=ip.ipv4_mapped if isinstance(ip,ipaddress.IPv6Address) and ip.ipv4_mapped else ip
        if effective.is_loopback or effective.is_link_local or effective.is_multicast or effective.is_unspecified or effective.is_reserved:
            raise ValueError('Printer address is blocked')
        values.append(str(ip))
    if not values: raise ValueError('Printer address unavailable')
    return values[0],port


@contextmanager
def printer_lock(printer_id):
    folder=Path(tempfile.gettempdir())/'odin-diagnostic-locks'
    folder.mkdir(mode=0o700,exist_ok=True)
    path=folder/(hashlib.sha256(str(printer_id).encode()).hexdigest()+'.lock')
    with path.open('a') as handle:
        try: fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: raise HTTPException(409,'A diagnostic is already running for this printer.') from None
        try: yield
        finally: fcntl.flock(handle,fcntl.LOCK_UN)


def failure(stage, error):
    # No exception strings leave the server: protocol replies can echo credentials.
    from modules.printers.telemetry.bambu.ftp_upload import ftplib
    if isinstance(error,(TimeoutError,socket.timeout)):
        return check(stage,'fail','The diagnostic timed out.','Check printer power, VLAN routing and firewall access.')
    if isinstance(error,ftplib.error_perm) and str(error).startswith('530'):
        return check(stage,'fail','Printer authentication was rejected.','Verify the saved printer access code and LAN access settings.')
    return check(stage,'fail','This communication stage failed.','Check saved credentials, printer LAN settings and the firewall for this channel.',error_type=type(error).__name__)


def ftps_checks(host, access_code, transfer=False, factory=None):
    from modules.printers.telemetry.bambu.ftp_upload import _ImplicitFTPS
    checks=[];ftp=None;created=False;remote='odin-diagnostic-'+uuid.uuid4().hex+'.txt'
    payload=b'ODIN non-printing diagnostic\n'+os.urandom(64)
    deadline=time.monotonic()+25
    def remaining():
        value=deadline-time.monotonic()
        if value<=0: raise TimeoutError()
        if ftp and ftp.sock: ftp.sock.settimeout(min(5,value))
        if ftp: ftp.timeout=min(5,value)
        return min(5,value)
    stage='ftps_tls'
    def interrupt():
        if ftp and ftp.sock:
            try: ftp.sock.shutdown(socket.SHUT_RDWR)
            except OSError: pass
    timer=threading.Timer(25,interrupt);timer.daemon=True;timer.start()
    try:
        ftp=(factory or _ImplicitFTPS)();ftp.trust_server_pasv_ipv4_address=False;ftp.connect(host=host,timeout=remaining())
        checks.append(check(stage,'pass','FTPS control TLS connection succeeded.'))
        stage='ftps_auth';remaining();ftp.login(user='bblp',passwd=access_code)
        checks.append(check(stage,'pass','FTPS authentication succeeded.'))
        stage='ftps_protection';remaining();ftp.prot_p();ftp.set_pasv(True)
        checks.append(check(stage,'pass','Protected data transfer negotiated.'))
        if transfer:
            stage='transfer_upload';remaining();created=True
            ftp.storbinary('STOR '+remote,io.BytesIO(payload),blocksize=1024,callback=lambda _: remaining())
            checks.append(check(stage,'pass','Disposable non-printing file uploaded.'))
            stage='transfer_verify';remaining();received=bytearray()
            def collect(chunk):
                remaining()
                if len(received)+len(chunk)>len(payload): raise ValueError('Diagnostic read exceeded expected bytes')
                received.extend(chunk)
            ftp.retrbinary('RETR '+remote,collect,blocksize=1024)
            if bytes(received)!=payload: raise ValueError('Diagnostic bytes differ')
            checks.append(check(stage,'pass','Retrieved file matches the diagnostic bytes.'))
    except Exception as error:
        checks.append(failure(stage,error))
    finally:
        timer.cancel()
        if ftp:
            if created:
                # Reconnect before deleting: an interrupted data transfer can
                # leave an unread reply on the original control connection.
                ftp.close()
                try:
                    cleanup_timer=threading.Timer(8,interrupt);cleanup_timer.daemon=True;cleanup_timer.start()
                    ftp=(factory or _ImplicitFTPS)()
                    ftp.connect(host=host,timeout=3)
                    ftp.login(user='bblp',passwd=access_code)
                    reply=ftp.delete(remote)
                    if not isinstance(reply,str) or not reply.startswith('250'):
                        raise OSError('Delete was not acknowledged')
                    checks.append(check('transfer_cleanup','pass','Disposable file removal acknowledged on a fresh connection.'))
                except Exception:
                    checks.append(check('transfer_cleanup','fail','Disposable file removal could not be verified.',
                                        'Remove only the diagnostic file named below using the printer file browser.', diagnostic_filename=remote))
                finally:
                    if 'cleanup_timer' in locals(): cleanup_timer.cancel()
            ftp.close()
    return checks


def printer_checks(printer,transfer=False):
    from core import crypto
    checks=[]
    try: host,port=safe_destination(printer.api_host)
    except (ValueError,OSError,subprocess.SubprocessError): return envelope([check('destination','fail','Saved printer address could not be safely resolved.','Check the saved printer address and DNS configuration.')])
    checks.append(check('destination','pass','Saved printer address resolved and validated.'))
    protocol=(printer.api_type or '').lower()
    if transfer and protocol!='bambu':
        return envelope(checks+[check('transfer','unsupported','A safe transfer diagnostic is currently available for Bambu LAN printers only.')])
    if protocol=='bambu':
        try:
            serial,code=crypto.decrypt(printer.api_key).split('|')
            if not serial or not code: raise ValueError()
        except Exception: return envelope(checks+[check('configuration','fail','Saved Bambu credentials are incomplete.','Update the saved serial and access code.')])
        if not transfer:
            from modules.printers.telemetry.bambu.adapter import BambuAdapterConfig
            from modules.printers.telemetry.bambu.session import read_status_once
            try:
                result=read_status_once(BambuAdapterConfig(printer_id=str(printer.id),serial=serial,host=host,port=8883,use_tls=True,access_code=code),timeout=8)
                checks.append(check('mqtt_status','pass' if result.success else 'fail',
                                    'Authenticated printer status received.' if result.success else 'No authenticated printer status received.',
                                    '' if result.success else 'Check LAN mode, saved serial/access code and TCP 8883 firewall access.'))
            except Exception as error: checks.append(failure('mqtt_status',error))
        checks.extend(ftps_checks(host,code,transfer))
    elif protocol in ('moonraker','prusalink'):
        import httpx
        try:
            key=crypto.decrypt(printer.api_key) if printer.api_key else ''
            auth=None
            if protocol=='prusalink' and '|' in key:
                username,password=key.split('|',1)
                auth=httpx.DigestAuth(username,password);headers={}
            else: headers={'X-Api-Key':key} if key else {}
            target=('['+host+']') if ':' in host else host
            target+=(':'+str(port or (7125 if protocol=='moonraker' else 80)))
            path='/printer/info' if protocol=='moonraker' else '/api/v1/status'
            with httpx.Client(timeout=5,trust_env=False,follow_redirects=False) as client:
                response=client.get('http://'+target+path,headers=headers,auth=auth)
            checks.append(check('http_status','pass' if response.status_code==200 else 'fail',
                                'Printer status endpoint responded.' if response.status_code==200 else 'Printer status endpoint rejected the request.',
                                '' if response.status_code==200 else 'Check printer API configuration and saved API key.',http_status=response.status_code))
        except Exception as error: checks.append(failure('http_status',error))
    else: checks.append(check('protocol','unsupported','This protocol has no guided diagnostic yet.','Use the existing printer connection test and report the protocol to support.'))
    return envelope(checks)


class PrinterTest(BaseModel):
    printer_id: int
    transfer: bool=False
    confirm_transfer: bool=False


class ReportRequest(BaseModel):
    problem: str=Field(default='',max_length=2000)
    include_logs: bool=False


@router.post('/system/troubleshooting/server')
def run_server(user=Depends(require_superadmin()),db:Session=Depends(get_db)):
    return server_checks(db)


@router.post('/system/troubleshooting/printer')
def run_printer(request:PrinterTest,user=Depends(require_superadmin()),db:Session=Depends(get_db),_scope=Depends(require_scope('write'))):
    from modules.printers.models import Printer
    printer=db.query(Printer).filter(Printer.id==request.printer_id).first()
    if not printer: raise HTTPException(404,'Printer not found')
    if request.transfer and not request.confirm_transfer: raise HTTPException(400,'Confirm the disposable file transfer test.')
    with printer_lock(printer.id): return printer_checks(printer,request.transfer)


@router.post('/system/troubleshooting/report')
def support_report(request:ReportRequest,user=Depends(require_superadmin()),db:Session=Depends(get_db)):
    report=server_checks(db)
    report['problem']=sanitize_log_line(request.problem)
    report['sharing_notice']='Review before sharing. Free-form text can contain personal information. No email has been sent.'
    report['included']=['server check results','filtered problem description']
    if request.include_logs:
        from modules.system.diagnostics import _read_tail
        from modules.system.sanitize import sanitize_log_lines
        report['recent_logs']={}
        for source in ('backend','mqtt_monitor','moonraker_monitor','prusalink_monitor','elegoo_monitor','supervisord'):
            logs=_read_tail('/data/'+source+'.log',100)
            report['recent_logs'][source]=sanitize_log_lines(logs) if isinstance(logs,list) else []
        report['included'].append('up to 100 filtered lines per source from six server/printer logs, at most 64 KiB per source')
    return report
