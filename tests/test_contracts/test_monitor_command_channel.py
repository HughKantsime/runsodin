"""Real local IPC and source-bound resume admission; no hardware traffic."""
import asyncio
import json
import threading
import time
import tempfile
from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from sqlalchemy import text
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_vision_training_acceptance import seed


@pytest.fixture
def socket_dir(monkeypatch):
    with tempfile.TemporaryDirectory(prefix='odin-cmd-', dir='/tmp') as path:
        monkeypatch.setenv('ODIN_MONITOR_COMMAND_DIR',path)
        yield path


def monitor():
    calls=[]
    transport=SimpleNamespace(_connected=True, _client=SimpleNamespace(
        publish=lambda *a,**kw: calls.append((a,kw)) or SimpleNamespace(rc=0)))
    return SimpleNamespace(_lock=threading.Lock(), _bambu=transport, serial='synthetic',
        _current_job_id=991, _state={'gcode_state':'PAUSE'}),calls


def test_real_owned_channel_uses_existing_client_only(socket_dir,monkeypatch):
    from modules.printers import monitor_commands as channel
    mon,calls=monitor()
    try:
        server=channel.CommandServer({9:mon})
    except PermissionError as error:
        if error.errno != 1:
            raise
        pytest.skip(f"Host sandbox blocks AF_UNIX bind: {error}")
    try:
        assert channel.request_command(9,'light',on=True)=='accepted'
        assert len(calls)==1
        assert json.loads(calls[0][0][1])['system']['led_mode']=='on'
        mon._bambu._connected=False
        assert channel.request_command(9,'light',on=False)=='unavailable'
        assert len(calls)==1
    finally:server.close()


def test_timeout_is_unknown_no_retry(socket_dir,monkeypatch):
    from modules.printers import monitor_commands as channel
    monkeypatch.setattr(channel,'TIMEOUT',0.1)
    calls=[]
    def slow(*args):
        calls.append(1);time.sleep(.2);return 'accepted'
    monkeypatch.setattr(channel,'execute',slow)
    try:
        server=channel.CommandServer({})
    except PermissionError as error:
        if error.errno != 1:
            raise
        pytest.skip(f"Host sandbox blocks AF_UNIX bind: {error}")
    try:
        assert channel.request_command(9,'light',on=True)=='unknown'
        assert calls==[1]
    finally:server.close()


@pytest.mark.parametrize('job,state',[(992,'PAUSE'),(991,'IDLE'),(991,'RUNNING')])
def test_monitor_refuses_drift(job,state):
    from modules.printers import monitor_commands as channel
    mon,calls=monitor();mon._current_job_id=job;mon._state['gcode_state']=state
    assert channel.execute({9:mon},dict(printer_id=9,action='resume',print_job_id=991,
        deadline=time.monotonic()+3))=='refused'
    assert calls==[]


def setup_resume(db,state):
    seed(db)
    db.execute(text("UPDATE printers SET api_type='bambu',is_active=1,gcode_state=:state WHERE id=9"),{'state':state})
    db.execute(text("INSERT INTO print_jobs(id,printer_id,filename,started_at,status) VALUES(991,9,'test.3mf','2026-10-23','running')"))
    db.execute(text('UPDATE vision_detections SET print_job_id=991 WHERE id=701'))
    db.commit()


@pytest.mark.parametrize('state',['PAUSE','PAUSED','RUNNING'])
def test_exact_resume_or_running_dismiss(review_db,monkeypatch,state):
    from modules.vision.routes.detections import dismiss_and_resume
    from modules.printers import monitor_commands
    setup_resume(review_db,state);calls=[]
    monkeypatch.setattr(monitor_commands,'request_command',lambda *a,**kw:calls.append((a,kw)) or 'accepted')
    result=dismiss_and_resume(701,current_user={'id':3,'role':'admin','group_id':None},db=review_db)
    assert result['status']=='dismissed'
    assert len(calls)==(0 if state=='RUNNING' else 1)
    assert review_db.execute(text('SELECT gcode_state FROM printers WHERE id=9')).scalar_one()==state


@pytest.mark.parametrize('fault',['stale','multiple','ended','wrong','inactive','idle','unknown'])
def test_refusal_or_unknown_preserves_detection(review_db,monkeypatch,fault):
    from modules.vision.routes.detections import dismiss_and_resume
    from modules.printers import monitor_commands
    setup_resume(review_db,'PAUSE');calls=[]
    changes={
        'stale':'UPDATE vision_detections SET print_job_id=992 WHERE id=701',
        'multiple':"INSERT INTO print_jobs(id,printer_id,filename,started_at,status) VALUES(992,9,'other','2026-10-23','running')",
        'ended':"UPDATE print_jobs SET ended_at='2026-10-23' WHERE id=991",
        'wrong':'UPDATE print_jobs SET printer_id=10 WHERE id=991',
        'inactive':'UPDATE printers SET is_active=0 WHERE id=9',
        'idle':"UPDATE printers SET gcode_state='IDLE' WHERE id=9"}
    if fault in changes:review_db.execute(text(changes[fault]));review_db.commit()
    monkeypatch.setattr(monitor_commands,'request_command',lambda *a,**kw:calls.append(1) or 'unknown')
    with pytest.raises(HTTPException) as error:
        dismiss_and_resume(701,current_user={'id':3,'role':'admin','group_id':None},db=review_db)
    assert error.value.status_code==(503 if fault=='unknown' else 409)
    assert len(calls)==(1 if fault=='unknown' else 0)
    assert review_db.execute(text('SELECT status FROM vision_detections WHERE id=701')).scalar_one()=='confirmed'


def test_existing_transport_publishes_resume_and_light_without_connection_creation():
    from modules.printers import monitor_commands as channel
    mon,calls=monitor()
    for action in ('resume','light'):
        assert channel.execute({9:mon},dict(printer_id=9,action=action,on=True,
            print_job_id=991,deadline=time.monotonic()+3))=='accepted'
    assert len(calls)==2


def test_client_timeout_reports_unknown_without_resubmission(monkeypatch):
    from modules.printers import monitor_commands as channel
    calls=[]
    class Connection:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def settimeout(self,*args):pass
        def connect(self,*args):calls.append('connect')
        def sendall(self,*args):calls.append('send')
        def recv(self,*args):raise TimeoutError()
    monkeypatch.setattr(channel.socket,'socket',lambda *args:Connection())
    assert channel.request_command(9,'light',on=True)=='unknown'
    assert calls==['connect','send']


def test_sqlite_fence_blocks_concurrent_association_write_through_transport(review_db,monkeypatch):
    from sqlalchemy.orm import Session
    from sqlalchemy.exc import OperationalError
    from modules.vision.routes.detections import dismiss_and_resume
    from modules.printers import monitor_commands
    setup_resume(review_db,'PAUSE');attempts=[]
    def command(*args,**kwargs):
        with Session(review_db.get_bind()) as other:
            other.execute(text('PRAGMA busy_timeout=0'))
            with pytest.raises(OperationalError):
                other.execute(text('UPDATE vision_detections SET print_job_id=992 WHERE id=701'))
            other.rollback()
        attempts.append(1)
        return 'accepted'
    monkeypatch.setattr(monitor_commands,'request_command',command)
    result=dismiss_and_resume(701,current_user={'id':3,'role':'admin','group_id':None},db=review_db)
    assert result['resume_requested'] and attempts==[1]


def test_exact_frame_cas_refuses_interleaved_source_change(review_db,monkeypatch):
    from modules.vision.routes.detections import dismiss_and_resume
    from modules.printers import monitor_commands
    setup_resume(review_db,'PAUSE');calls=[]
    original=review_db.execute
    def execute(statement,*args,**kwargs):
        if str(statement).startswith('UPDATE vision_detections SET status=') and 'reviewed_by=' in str(statement):
            original(text("UPDATE vision_detections SET frame_path='changed.jpg' WHERE id=701"))
        return original(statement,*args,**kwargs)
    monkeypatch.setattr(review_db,'execute',execute)
    monkeypatch.setattr(monitor_commands,'request_command',lambda *a,**kw:calls.append(1) or 'accepted')
    with pytest.raises(HTTPException) as error:
        dismiss_and_resume(701,current_user={'id':3,'role':'admin','group_id':None},db=review_db)
    assert error.value.status_code==409 and calls==[]
    assert original(text('SELECT status FROM vision_detections WHERE id=701')).scalar_one()=='confirmed'


@pytest.mark.parametrize('ack_lost',[False,True])
def test_resume_commit_uncertainty_never_retries_or_fabricates_state(review_db,monkeypatch,ack_lost):
    from sqlalchemy.orm import Session
    from modules.vision.routes.detections import dismiss_and_resume
    from modules.printers import monitor_commands
    setup_resume(review_db,'PAUSE');calls=[]
    monkeypatch.setattr(monitor_commands,'request_command',lambda *a,**kw:calls.append(1) or 'accepted')
    class FaultSession(Session):
        def commit(self):
            if ack_lost:super().commit()
            raise RuntimeError('Synthetic lost commit')
    with FaultSession(review_db.get_bind()) as db:
        with pytest.raises(HTTPException) as error:
            dismiss_and_resume(701,current_user={'id':3,'role':'admin','group_id':None},db=db)
    assert error.value.status_code==503 and error.value.detail['outcome']=='unknown'
    assert calls==[1]
    assert review_db.execute(text('SELECT gcode_state FROM printers WHERE id=9')).scalar_one()=='PAUSE'
    assert review_db.execute(text('SELECT status FROM vision_detections WHERE id=701')).scalar_one()==('dismissed' if ack_lost else 'confirmed')


@pytest.mark.parametrize('v2',[False,True])
def test_disconnected_monitor_and_expired_queue_never_publish(v2):
    from modules.printers import monitor_commands as channel
    mon,calls=monitor()
    transport=mon._bambu
    if v2:mon._v2_adapter=transport;mon._bambu=None
    message=dict(printer_id=9,action='light',on=True,deadline=time.monotonic()+3)
    transport._connected=False
    assert channel.execute({9:mon},message)=='unavailable'
    assert channel.execute({},message)=='unavailable'
    transport._connected=True
    message['deadline']=time.monotonic()-1
    assert channel.execute({9:mon},message)=='refused'
    assert calls==[]
    message['deadline']=time.monotonic()+3
    assert channel.execute({9:mon},message)=='accepted'
    assert len(calls)==1


@pytest.mark.parametrize('family',[None,'unknown','anycubic_lan','Moonraker'])
def test_unknown_family_never_falls_back_to_a_new_bambu_connection(monkeypatch,family):
    from modules.printers.services import request_observed_task_resume
    from modules.printers import route_utils
    calls=[]
    monkeypatch.setattr(route_utils,'_send_printer_command',lambda *args:calls.append(1))
    assert request_observed_task_resume(SimpleNamespace(api_type=family),991)=='refused'
    assert calls==[]
