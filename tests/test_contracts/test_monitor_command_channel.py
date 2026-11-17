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


@pytest.mark.parametrize('ids', [[], [0], [16777216], [True], ['15'], [15,15], list(range(1,66)), None])
def test_skip_ids_are_bounded_distinct_integers(ids):
    from modules.printers import monitor_commands as channel
    mon,calls=monitor()
    assert channel.execute({9:mon},dict(printer_id=9,action='skip_objects',print_job_id=991,
        object_ids=ids,deadline=time.monotonic()+3))=='refused'
    assert calls==[]


@pytest.mark.parametrize('state', ['RUNNING','PAUSE','PAUSED'])
@pytest.mark.parametrize('v2', [False,True])
def test_skip_uses_existing_transport_integer_wire(state,v2):
    from modules.printers import monitor_commands as channel
    mon,calls=monitor();mon._state['gcode_state']=state
    if v2:mon._v2_adapter=mon._bambu;mon._bambu=None
    assert channel.execute({9:mon},dict(printer_id=9,action='skip_objects',print_job_id=991,
        object_ids=[15,300],deadline=time.monotonic()+3))=='accepted'
    assert len(calls)==1 and calls[0][1]==dict(qos=0)
    assert json.loads(calls[0][0][1])=={'print':{'sequence_id':'0','command':'skip_objects','obj_list':[15,300]}}


@pytest.mark.parametrize('changes', [dict(print_job_id=992), dict(print_job_id=True),dict(print_job_id=None),dict(deadline=0),dict(state='IDLE')])
def test_skip_job_state_and_deadline_fences(changes):
    from modules.printers import monitor_commands as channel
    mon,calls=monitor()
    message=dict(printer_id=9,action='skip_objects',print_job_id=991,object_ids=[15],deadline=time.monotonic()+3)
    if 'state' in changes:mon._state['gcode_state']=changes.pop('state')
    message.update(changes)
    assert channel.execute({9:mon},message)=='refused' and calls==[]


def test_skip_state_binds_only_reports_of_current_print():
    from modules.printers import monitor_commands as channel
    mon,calls=monitor()
    mon._state.update(s_obj=[15],fun=format(1<<49,'x'),plate_idx=1)
    assert channel.skip_state(mon)['skipped'] is None
    channel.observe_skip_state(mon, {'s_obj':[15]})
    result=channel.execute({9:mon},dict(printer_id=9,action='skip_state',deadline=time.monotonic()+3))
    assert result==dict(print_job_id=991,gcode_state='PAUSE',skipped=[15],fun=format(1<<49,'x'),plate_idx=1)
    assert len(json.dumps(result))<channel.MAX_FRAME and calls==[]
    mon._current_job_id=992
    channel.observe_skip_state(mon,{})
    assert channel.skip_state(mon)['skipped'] is None and 's_obj' not in mon._state
    mon._current_job_id=993
    channel.observe_skip_state(mon,{'s_obj':[300]})
    assert channel.skip_state(mon)['skipped']==[300]
    channel.observe_skip_state(mon,{'s_obj':['300']})
    assert channel.skip_state(mon)['skipped'] is None


@pytest.mark.parametrize('answer', [None, [], {}, {'print_job_id':True}, 'accepted', 'unknown'])
def test_bad_skip_state_reply_is_unavailable(monkeypatch,answer):
    from modules.printers import monitor_commands as channel
    class Connection:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def settimeout(self,*args):pass
        def connect(self,*args):pass
        def sendall(self,*args):pass
        def recv(self,*args):return json.dumps(answer).encode()+b'\n'
    monkeypatch.setattr(channel.socket,'socket',lambda *args:Connection())
    assert channel.request_skip_state(9) is None


def test_v2_preserves_skip_fields_in_legacy_bridge():
    from modules.printers.telemetry.bambu.raw import BambuPrintSection
    report = BambuPrintSection.model_validate(dict(gcode_state='RUNNING',s_obj=[15,300],fun=format(1<<49,'x'),plate_idx=2))
    raw = report.model_dump(exclude_unset=True)
    assert raw['s_obj']==[15,300] and raw['fun']==format(1<<49,'x') and raw['plate_idx']==2


def test_skip_request_is_sent_once_and_reply_is_validated(monkeypatch):
    from modules.printers import monitor_commands as channel
    sent=[]
    class Connection:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def settimeout(self,*args):pass
        def connect(self,*args):pass
        def sendall(self,packet):sent.append(json.loads(packet))
        def recv(self,*args):return b'"accepted"\n'
    monkeypatch.setattr(channel.socket,'socket',lambda *args:Connection())
    assert channel.request_command(9,'skip_objects',print_job_id=991,object_ids=[15,79])=='accepted'
    assert len(sent)==1 and sent[0]['object_ids']==[15,79] and sent[0]['print_job_id']==991


def test_real_status_path_discards_old_skip_list_and_keeps_opening_report(wire_monitor):
    from modules.printers import monitor_commands as channel
    mon,sock,clock,events=wire_monitor
    mon._state['s_obj']=[79]
    mon._skip_report_job_id=111
    mon._restart_check_needed=False
    mon._recovery_pending=False
    sock.deliver({'print':dict(gcode_state='RUNNING',subtask_name='synthetic-first',s_obj=[15],fun=format(1<<49,'x'),plate_idx=1)})
    assert mon._current_job_id is not None
    assert channel.skip_state(mon)['skipped']==[15]
    sock.deliver({'print':dict(gcode_state='FINISH')})
    assert mon._current_job_id is None
    clock[0]+=20
    sock.deliver({'print':dict(gcode_state='RUNNING',subtask_name='synthetic-second')})
    assert mon._current_job_id is not None
    assert channel.skip_state(mon)['skipped'] is None


from tests.test_contracts.test_cert_A_harness import wire_monitor, broker_socket, fernet_env  # noqa: F401,E402


def test_skip_deadline_expires_while_waiting_for_owner_lock(monkeypatch):
    from modules.printers import monitor_commands as channel
    mon,calls=monitor()
    now=[100.0]
    monkeypatch.setattr(channel,'time',SimpleNamespace(monotonic=lambda:now[0]))
    class Lock:
        def acquire(self,timeout):now[0]=104.0;return True
        def release(self):pass
    mon._lock=Lock()
    assert channel.execute({9:mon},dict(printer_id=9,action='skip_objects',print_job_id=991,
        object_ids=[15],deadline=103.0))=='unknown'
    assert calls==[]
