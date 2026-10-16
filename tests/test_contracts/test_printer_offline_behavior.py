"""Actual monitor failure paths against owned persistence and controlled transports."""
from types import SimpleNamespace
import pytest


@pytest.fixture(autouse=True)
def _loopback_device_peers(monkeypatch):
    # Brokers/peers in this file run on loopback, which the production device
    # destination policy blocks; exempt exactly 127.0.0.1 and ::1 here (also in
    # spawned workers). test_cert_A_destination_policy covers the policy.
    from tests.support.loopback_devices import allow_loopback_devices
    allow_loopback_devices(monkeypatch)
from sqlalchemy import text
from tests.test_contracts.test_api_token_delegation_behavior import token_client, review_db


@pytest.fixture
def offline_store(review_db):
    review_db.execute(text("INSERT INTO printers(id,name,api_type,org_id,gcode_state,print_stage,last_seen,slot_count,is_active,tags,timelapse_enabled,shared) VALUES(199,'Synthetic monitor','bambu',1,'RUNNING','Printing','2026-10-11 12:00:00',4,1,'[]',0,0)"));review_db.commit()
    review_db.execute(text("INSERT INTO print_jobs(id,printer_id,filename,job_name,started_at,status) VALUES(199,199,'synthetic.3mf','Synthetic active','2026-10-11 11:00:00','running')"));review_db.commit()
    yield review_db
    assert tuple(review_db.execute(text('SELECT status,ended_at FROM print_jobs WHERE id=199')).one())==('running',None)


def state(db):
    return tuple(db.execute(text('SELECT gcode_state,print_stage,last_seen FROM printers WHERE id=199')).one())


def test_prusalink_five_failures_persist_offline_without_advancing_heartbeat(offline_store, monkeypatch):
    from modules.printers.monitors import prusalink_monitor as module
    monkeypatch.setattr(module,'engine',offline_store.get_bind())
    monitor=module.PrusaLinkMonitorThread(199,'Synthetic','127.0.0.1')
    monkeypatch.setattr(monitor,'_discover_and_save_camera',lambda:None)
    observations=[]
    def failure():raise OSError('Synthetic unavailable transport')
    def sleep(_):
        observations.append(state(offline_store)[0])
        if len(observations)==5:monitor._running=False
    monkeypatch.setattr(monitor.client,'get_status',failure)
    monkeypatch.setattr(module,'time',SimpleNamespace(time=lambda:2000,sleep=sleep))
    monitor.run()
    assert observations==['RUNNING']*4+['OFFLINE']
    assert state(offline_store)[2]=='2026-10-11 12:00:00'


def test_prusalink_retries_failed_offline_persistence_on_next_failure(offline_store, monkeypatch):
    from modules.printers.monitors import prusalink_monitor as module
    actual_engine=offline_store.get_bind();attempts=[]
    class FaultEngine:
        def begin(self):
            attempts.append(True)
            if len(attempts)==1:raise RuntimeError('Synthetic DB refusal')
            return actual_engine.begin()
    monkeypatch.setattr(module,'engine',FaultEngine())
    monitor=module.PrusaLinkMonitorThread(199,'Synthetic','127.0.0.1')
    monkeypatch.setattr(monitor,'_discover_and_save_camera',lambda:None)
    polls=[]
    def failure():raise OSError('Synthetic unavailable transport')
    def sleep(_):
        polls.append(True)
        if len(polls)==6:monitor._running=False
    monkeypatch.setattr(monitor.client,'get_status',failure)
    monkeypatch.setattr(module,'time',SimpleNamespace(time=lambda:2000,sleep=sleep))
    monitor.run()
    assert len(attempts)==2
    assert state(offline_store)[0]=='OFFLINE'


def test_moonraker_disconnect_persists_offline_without_advancing_heartbeat(offline_store, monkeypatch):
    from modules.printers.monitors import moonraker_monitor as module
    monkeypatch.setattr(module,'engine',offline_store.get_bind())
    monitor=module.MoonrakerMonitor(199,'Synthetic','127.0.0.1')
    monitor._running=False
    monitor._handle_disconnect()
    assert state(offline_store)[0]=='OFFLINE'
    assert state(offline_store)[2]=='2026-10-11 12:00:00'


@pytest.mark.parametrize('fail_first_commit',[False,True])
def test_elegoo_stale_status_persists_offline_and_retries_db_failure(offline_store, monkeypatch, fail_first_commit):
    from modules.printers.monitors import elegoo_monitor as module
    actual_engine=offline_store.get_bind();attempts=[]
    class FaultEngine:
        def begin(self):
            attempts.append(True)
            if fail_first_commit and len(attempts)==1:raise RuntimeError('Synthetic DB refusal')
            return actual_engine.begin()
    monkeypatch.setattr(module,'engine',FaultEngine())
    monitor=module.ElegooMonitorThread(199,'Synthetic','127.0.0.1')
    monitor._last_heartbeat=1900
    monkeypatch.setattr(monitor.client,'connect',lambda:True)
    monitor.client._connected=True
    monkeypatch.setattr(monitor.client,'disconnect',lambda:None)
    calls=[]
    def sleep(seconds):
        if seconds==2:
            calls.append(True)
            if len(calls)==(2 if fail_first_commit else 1):monitor._running=False
    monkeypatch.setattr(module,'time',SimpleNamespace(time=lambda:2000,sleep=sleep))
    monitor.run()
    assert len(attempts)==(2 if fail_first_commit else 1)
    assert state(offline_store)[0]=='OFFLINE'
    assert state(offline_store)[2]=='2026-10-11 12:00:00'


def test_bambu_daemon_stale_reconnect_marks_offline_before_failed_replacement(offline_store, monkeypatch):
    from modules.printers.monitors import mqtt_monitor as module
    monkeypatch.setattr(module,'engine',offline_store.get_bind())
    old=SimpleNamespace(name='Synthetic',_last_heartbeat=1800,_bambu=None,disconnect=lambda:None)
    daemon=module.MQTTMonitorDaemon();daemon.monitors={199:old}
    daemon._all_printers=[{'id':199,'name':'Synthetic','ip':'127.0.0.1','serial':'synthetic','access_code':''}];daemon._all_moonraker=[]
    observed=[]
    class Replacement:
        def __init__(self,**kwargs):pass
        def connect(self):observed.append(state(offline_store)[0]);return False
    monkeypatch.setattr(module,'PrinterMonitor',Replacement)
    monkeypatch.setattr(module,'time',SimpleNamespace(time=lambda:2000,sleep=lambda _:None))
    daemon._check_reconnect()
    assert observed==['OFFLINE']
    assert state(offline_store)[2]=='2026-10-11 12:00:00'
    assert 199 not in daemon.monitors


@pytest.fixture
def v2_offline_monitor(offline_store, monkeypatch):
    from contextlib import contextmanager
    from core.db_utils import _ConnectionAdapter
    from modules.printers.monitors import mqtt_printer as module
    from modules.printers.telemetry.bambu import adapter, broker_policy
    emitters=[]
    class Adapter:
        def __init__(self,config,emitter):emitters.append(emitter)
        def start(self):pass
        def stop(self):pass
    @contextmanager
    def database():
        raw=offline_store.get_bind().raw_connection()
        try:yield _ConnectionAdapter(raw,False)
        finally:raw.close()
    monkeypatch.setattr(module,'get_db',database)
    monkeypatch.setattr(module,'time',SimpleNamespace(time=lambda:2000))
    monkeypatch.setattr(module,'ws_push',lambda *a,**kw:None)
    monkeypatch.setattr(module,'mqtt_republish',None)
    monkeypatch.setattr(adapter,'BambuTelemetryAdapter',Adapter)
    monkeypatch.setattr(broker_policy,'resolve_bambu_broker_config',lambda *a,**kw:SimpleNamespace(host='127.0.0.1',port=1883,use_tls=False))
    monitor=module.PrinterMonitor(199,'Synthetic','127.0.0.1','synthetic','')
    monitor._camera_discovered=True
    monkeypatch.setattr(monitor,'_recover_orphaned_jobs',lambda:None)
    monkeypatch.setattr(monitor,'_on_state_change',lambda *a:None)
    monkeypatch.setattr(monitor,'_check_spool_levels',lambda:None)
    assert monitor._connect_v2()
    return monitor,emitters,module


def test_actual_v2_disconnect_event_persists_offline(v2_offline_monitor,offline_store):
    from modules.printers.telemetry.events import ConnectionEvent
    monitor,emitters,_=v2_offline_monitor
    emitters[0](ConnectionEvent('monitor-199',2000,'disconnected'))
    assert state(offline_store)[0]=='OFFLINE'
    assert state(offline_store)[2]=='2026-10-11 12:00:00'


def test_actual_v2_retired_disconnect_cannot_overwrite_new_valid_report(v2_offline_monitor,offline_store):
    from modules.printers.telemetry.events import ConnectionEvent,BambuReportEvent
    from modules.printers.telemetry.bambu.raw import BambuPrintSection
    monitor,emitters,_=v2_offline_monitor
    monitor.disconnect();assert monitor._connect_v2()
    emitters[1](BambuReportEvent('monitor-199',2000,BambuPrintSection.model_validate({'gcode_state':'IDLE'})))
    assert state(offline_store)[0]=='IDLE'
    emitters[0](ConnectionEvent('monitor-199',2001,'disconnected'))
    assert state(offline_store)[0]=='IDLE'


@pytest.mark.parametrize('protocol',['prusalink','elegoo','moonraker'])
@pytest.mark.parametrize('refuse_commit',[False,True])
def test_actual_valid_status_restores_offline_before_normal_heartbeat_throttle(offline_store, monkeypatch, token_client, caplog, protocol, refuse_commit):
    from modules.printers import routes_crud
    if protocol=='prusalink':
        from modules.printers.monitors import prusalink_monitor as module
        from modules.printers.adapters.prusalink import PrusaLinkStatus,PrusaLinkState
        monitor=module.PrusaLinkMonitorThread(199,'Synthetic','127.0.0.1')
        status=PrusaLinkStatus(state=PrusaLinkState.IDLE,internal_state='IDLE')
        monitor._last_state='IDLE'
    elif protocol=='elegoo':
        from modules.printers.monitors import elegoo_monitor as module
        from modules.printers.adapters.elegoo import ElegooStatus
        monitor=module.ElegooMonitorThread(199,'Synthetic','127.0.0.1')
        status=ElegooStatus(internal_state='IDLE')
        monitor._last_state='IDLE'
    else:
        from modules.printers.monitors import moonraker_monitor as module
        from modules.printers.adapters.moonraker import MoonrakerStatus,MoonrakerState
        monitor=module.MoonrakerMonitor(199,'Synthetic','127.0.0.1')
        status=MoonrakerStatus(state=MoonrakerState.READY,internal_state='IDLE',raw_data={'print_stats':{'state':'standby'}})
        monitor._prev_state='IDLE'
    from contextlib import contextmanager
    actual_engine=offline_store.get_bind();commits=[];reported=[]
    class FaultEngine:
        @contextmanager
        def begin(self):
            with actual_engine.connect() as conn:
                tx=conn.begin()
                try:
                    yield conn
                    commits.append(True)
                    if refuse_commit and len(commits)==1:
                        raise RuntimeError('synthetic-private-recovery-commit-detail')
                    tx.commit()
                except Exception:
                    tx.rollback();raise
    monkeypatch.setattr(module,'engine',FaultEngine())
    monkeypatch.setattr(module,'time',SimpleNamespace(time=lambda:2001))
    def delivered(channel):
        reported.append((channel,state(offline_store)[0]))
    monkeypatch.setattr(module,'ws_push',lambda *a,**kw:delivered('ws'))
    publisher=SimpleNamespace(republish_telemetry=lambda *a,**kw:delivered('mqtt'))
    if hasattr(module,'_mqtt_republish'):monkeypatch.setattr(module,'_mqtt_republish',publisher)
    if hasattr(module,'mqtt_republish'):monkeypatch.setattr(module,'mqtt_republish',publisher)
    offline_store.execute(text("UPDATE printers SET gcode_state='OFFLINE',print_stage='Offline' WHERE id=199"));offline_store.commit()
    monitor._marked_offline=True;monitor._last_heartbeat=2000
    client,issue=token_client;client.app.include_router(routes_crud.router)
    headers=issue(['read:printers'])
    assert client.get('/printers/199',headers=headers).json()['gcode_state']=='OFFLINE'
    if refuse_commit:
        monitor._process_status(status)
        assert state(offline_store)[0]=='OFFLINE'
        assert monitor._marked_offline is True and monitor._last_heartbeat==2000
        assert reported==[]
        assert 'synthetic-private-recovery-commit-detail' not in caplog.text
    monitor._process_status(status)
    assert state(offline_store)[0]!='OFFLINE'
    assert monitor._marked_offline is False and monitor._last_heartbeat==2001
    response=client.get('/printers/199',headers=headers)
    assert response.status_code==200 and response.json()['gcode_state']!='OFFLINE'
    assert response.json()['last_seen']!='2026-10-11T12:00:00'
    assert len(reported)==2 and all(value!='OFFLINE' for _,value in reported)


@pytest.mark.parametrize('protocol',['prusalink','elegoo','moonraker'])
def test_retired_monitor_ignores_late_status_before_any_mutation(offline_store, monkeypatch, protocol):
    if protocol=='prusalink':
        from modules.printers.monitors import prusalink_monitor as module
        monitor=module.PrusaLinkMonitorThread(199,'Synthetic','127.0.0.1')
    elif protocol=='elegoo':
        from modules.printers.monitors import elegoo_monitor as module
        monitor=module.ElegooMonitorThread(199,'Synthetic','127.0.0.1')
        monkeypatch.setattr(monitor.client,'disconnect',lambda:None)
    else:
        from modules.printers.monitors import moonraker_monitor as module
        monitor=module.MoonrakerMonitor(199,'Synthetic','127.0.0.1')
        monkeypatch.setattr(monitor.printer,'disconnect',lambda:None)
    monkeypatch.setattr(module,'engine',offline_store.get_bind())
    before=state(offline_store)
    if protocol=='moonraker':monitor.disconnect()
    else:monitor.stop()
    # Deliberately no status fields: retirement must be checked before processing.
    monitor._process_status(object())
    assert state(offline_store)==before


def test_v2_disconnected_then_valid_status_restores_within_heartbeat_throttle(v2_offline_monitor,offline_store):
    from modules.printers.telemetry.events import ConnectionEvent,BambuReportEvent
    from modules.printers.telemetry.bambu.raw import BambuPrintSection
    monitor,emitters,_=v2_offline_monitor
    monitor._last_heartbeat=1999
    emitters[0](ConnectionEvent('monitor-199',2000,'disconnected'))
    assert state(offline_store)[0]=='OFFLINE'
    emitters[0](BambuReportEvent('monitor-199',2000,BambuPrintSection.model_validate({'gcode_state':'IDLE'})))
    assert state(offline_store)[0]=='IDLE'
    assert monitor._transport_offline is False


@pytest.mark.parametrize('fail_offline_commit',[False,True])
def test_v2_offline_and_valid_report_commits_are_serialized(v2_offline_monitor,offline_store,monkeypatch,caplog,fail_offline_commit):
    from contextlib import contextmanager
    from threading import Event
    from concurrent.futures import ThreadPoolExecutor
    from modules.printers.telemetry.events import ConnectionEvent,BambuReportEvent
    from modules.printers.telemetry.bambu.raw import BambuPrintSection
    monitor,emitters,module=v2_offline_monitor
    original=module.get_db;entered=Event();release=Event();report_started=Event()
    class PausedConnection:
        def __init__(self,conn):self.conn=conn;self.offline=False
        def __getattr__(self,name):return getattr(self.conn,name)
        def execute(self,statement,parameters=()):
            if "gcode_state='OFFLINE'" in statement:self.offline=True
            return self.conn.execute(statement,parameters)
        def commit(self):
            if self.offline:
                entered.set()
                if not release.wait(5):raise TimeoutError('Synthetic release timeout')
                if fail_offline_commit:raise RuntimeError('synthetic-private-offline-commit-detail')
            self.conn.commit()
    @contextmanager
    def database():
        with original() as conn:yield PausedConnection(conn)
    monkeypatch.setattr(module,'get_db',database)
    def report():
        report_started.set()
        emitters[0](BambuReportEvent('monitor-199',2001,BambuPrintSection.model_validate({'gcode_state':'IDLE'})))
    with ThreadPoolExecutor(max_workers=2) as executor:
        offline=executor.submit(emitters[0],ConnectionEvent('monitor-199',2000,'disconnected'))
        try:
            assert entered.wait(3)
            healthy=executor.submit(report)
            assert report_started.wait(3)
            assert not healthy.done()
        finally:release.set()
        offline.result(timeout=5);healthy.result(timeout=5)
    assert state(offline_store)[0]=='IDLE'
    assert monitor._transport_offline is False
    assert 'synthetic-private-offline-commit-detail' not in caplog.text


def test_elegoo_retired_socket_status_cannot_overwrite_new_connection(offline_store,monkeypatch):
    import json
    from modules.printers.monitors import elegoo_monitor as module
    monkeypatch.setattr(module,'engine',offline_store.get_bind())
    clock=[2000]
    monkeypatch.setattr(module,'time',SimpleNamespace(time=lambda:clock[0]))
    monkeypatch.setattr(module,'ws_push',lambda *a,**kw:None)
    monkeypatch.setattr(module,'_mqtt_republish',None)
    monitor=module.ElegooMonitorThread(199,'Synthetic','127.0.0.1');monitor._last_state='IDLE'
    import modules.notifications.event_dispatcher as printer_events
    starts=[]
    monkeypatch.setattr(printer_events,'on_print_start',lambda *a,**kw:starts.append(True))
    old_socket=object();new_socket=object();monitor.client._ws=new_socket
    monitor.client._on_message(new_socket,json.dumps({'Topic':'sdcp/status/synthetic','Status':{'CurrentStatus':[0]}}))
    assert state(offline_store)[0]=='IDLE'
    clock[0]=2011
    monitor.client._on_message(old_socket,json.dumps({'Topic':'sdcp/status/synthetic','Status':{'CurrentStatus':[8]}}))
    assert state(offline_store)[0]=='IDLE'
    assert monitor.client.get_status().internal_state=='IDLE'
    assert starts==[]


@pytest.mark.parametrize('callback',['open','close'])
def test_elegoo_retired_socket_lifecycle_does_not_change_current_connection(callback):
    from modules.printers.adapters.elegoo import ElegooPrinter
    client=ElegooPrinter('127.0.0.1');client._ws=object();old_socket=object()
    client._connected=callback=='close'
    if callback=='open':client._on_open(old_socket)
    else:client._on_close(old_socket,None,None)
    assert client._connected is (callback=='close')


@pytest.mark.parametrize('outcome',['open','error','close'])
def test_elegoo_connect_current_socket_readiness_and_retirement(monkeypatch,outcome):
    import sys
    from modules.printers.adapters.elegoo import ElegooPrinter
    sockets=[]
    class Socket:
        def __init__(self,url,**callbacks):
            self.callbacks=callbacks;self.closed=False;sockets.append(self)
        def run_forever(self,**kwargs):
            if outcome=='open':self.callbacks['on_open'](self)
            elif outcome=='error':self.callbacks['on_error'](self,RuntimeError('synthetic-private-transport'))
            else:self.callbacks['on_close'](self,None,None)
        def close(self):self.closed=True
    monkeypatch.setitem(sys.modules,'websocket',SimpleNamespace(WebSocketApp=Socket))
    client=ElegooPrinter('127.0.0.1')
    assert client.connect() is (outcome=='open')
    if outcome=='open':
        assert client._ws is sockets[0] and client._connected
        client._on_close(sockets[0],None,None)
        assert not client._connected
        client.disconnect()
    assert client._ws is None and sockets[0].closed


def test_elegoo_disconnect_wakes_pending_connect_without_late_open(monkeypatch):
    import sys
    from threading import Event
    from concurrent.futures import ThreadPoolExecutor
    from modules.printers.adapters.elegoo import ElegooPrinter
    entered=Event();release=Event();sockets=[]
    class Socket:
        def __init__(self,url,**callbacks):self.callbacks=callbacks;self.closed=False;sockets.append(self)
        def run_forever(self,**kwargs):
            entered.set()
            if release.wait(3):self.callbacks['on_open'](self)
        def close(self):self.closed=True;release.set()
    monkeypatch.setitem(sys.modules,'websocket',SimpleNamespace(WebSocketApp=Socket))
    client=ElegooPrinter('127.0.0.1')
    with ThreadPoolExecutor(max_workers=1) as executor:
        connecting=executor.submit(client.connect)
        try:
            assert entered.wait(3)
            client.disconnect()
            assert connecting.result(timeout=3) is False
        finally:release.set()
    assert client._ws is None and not client._connected and sockets[0].closed


def test_elegoo_replacement_cancels_old_wait_without_closing_new_socket(monkeypatch):
    import sys
    from threading import Event
    from concurrent.futures import ThreadPoolExecutor
    from modules.printers.adapters.elegoo import ElegooPrinter
    entered=Event();release=Event();new_closed=Event();sockets=[]
    class Socket:
        def __init__(self,url,**callbacks):self.callbacks=callbacks;self.closed=False;sockets.append(self)
        def run_forever(self,**kwargs):
            if self is sockets[0]:
                entered.set()
                if release.wait(3):self.callbacks['on_open'](self)
            else:
                self.callbacks['on_open'](self)
                new_closed.wait(3)
        def close(self):
            self.closed=True
            if self is sockets[0]:release.set()
            else:new_closed.set()
    monkeypatch.setitem(sys.modules,'websocket',SimpleNamespace(WebSocketApp=Socket))
    client=ElegooPrinter('127.0.0.1')
    with ThreadPoolExecutor(max_workers=1) as executor:
        old=executor.submit(client.connect)
        try:
            assert entered.wait(3)
            assert client.connect() is True
            assert old.result(timeout=3) is False
            assert client._ws is sockets[1] and client._connected
            assert sockets[0].closed and not sockets[1].closed
        finally:release.set();client.disconnect()


def test_elegoo_connection_timeout_retires_socket(monkeypatch):
    import sys
    from modules.printers.adapters.elegoo import ElegooPrinter
    sockets=[]
    class Socket:
        def __init__(self,url,**callbacks):self.closed=False;sockets.append(self)
        def run_forever(self,**kwargs):pass
        def close(self):self.closed=True
    monkeypatch.setitem(sys.modules,'websocket',SimpleNamespace(WebSocketApp=Socket))
    client=ElegooPrinter('127.0.0.1')
    assert client.connect() is False
    assert client._ws is None and not client._connected and sockets[0].closed


def test_elegoo_queued_retired_runner_never_enters_transport():
    from modules.printers.adapters.elegoo import ElegooPrinter
    calls=[]
    socket=SimpleNamespace(run_forever=lambda **kw:calls.append('run'),close=lambda:calls.append('close'))
    client=ElegooPrinter('127.0.0.1')
    client._ws=object()
    client._run_socket(socket)
    assert calls==['close']


def test_elegoo_retirement_during_initialization_closes_late_open():
    from modules.printers.adapters.elegoo import ElegooPrinter
    client=ElegooPrinter('127.0.0.1');calls=[]
    def run(**kwargs):
        client.disconnect()
        calls.append('initialize-after-cancel')
        client._on_open(socket)
        assert not client._connected and client._ws is None
    socket=SimpleNamespace(run_forever=run,close=lambda:calls.append('close'))
    client._ws=socket;client._run_socket(socket)
    assert calls==['close','initialize-after-cancel','close','close']


def test_elegoo_stop_during_inflight_status_has_no_lock_inversion(offline_store,monkeypatch):
    import json
    from threading import Event
    from concurrent.futures import ThreadPoolExecutor
    from modules.printers.monitors import elegoo_monitor as module
    monkeypatch.setattr(module,'engine',offline_store.get_bind())
    monitor=module.ElegooMonitorThread(199,'Synthetic','127.0.0.1')
    entered=Event();release=Event();retired=Event();closed=[]
    socket=SimpleNamespace(close=lambda:closed.append(True));monitor.client._ws=socket
    original=monitor._process_status
    def callback(status):
        entered.set()
        assert release.wait(3)
        original(status)
    monitor.client.on_status(callback)
    disconnect=monitor.client.disconnect
    def observe_disconnect():
        retired.set()
        disconnect()
    monkeypatch.setattr(monitor.client,'disconnect',observe_disconnect)
    before=state(offline_store)
    with ThreadPoolExecutor(max_workers=2) as executor:
        message=executor.submit(monitor.client._on_message,socket,json.dumps({'Topic':'sdcp/status/synthetic','Status':{'CurrentStatus':[0]}}))
        try:
            assert entered.wait(3)
            stop=executor.submit(monitor.stop)
            assert retired.wait(3)
            assert monitor._retired
        finally:release.set()
        message.result(timeout=3);stop.result(timeout=3)
    assert state(offline_store)==before
    assert closed and monitor.client._ws is None
