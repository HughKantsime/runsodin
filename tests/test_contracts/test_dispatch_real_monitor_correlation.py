"""Exact dispatch correlation must survive actual monitor creation boundaries."""
from contextlib import contextmanager
from types import SimpleNamespace
import pytest
from sqlalchemy import text
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.dispatch_test_support import configure_dispatch as _configure_dispatch


def configure_dispatch(db,tmp_path,monkeypatch,protocol='moonraker'):
    from core.registry import registry
    from core import event_bus
    from core.event_bus import InMemoryEventBus
    from core.monitor_bootstrap import bootstrap_monitor_authority
    monkeypatch.setattr(registry,'_providers',{})
    bus=InMemoryEventBus()
    monkeypatch.setattr(event_bus,'get_event_bus',lambda:bus)
    result=_configure_dispatch(db,tmp_path,monkeypatch,protocol=protocol)
    bootstrap_monitor_authority()
    return result

@pytest.mark.parametrize('source',['job_event','moonraker','mqtt'])
def test_actual_monitor_correlates_exact_admitted_attempt(review_db,tmp_path,monkeypatch,source):
    from core.db_utils import _ConnectionAdapter
    from modules.printers import dispatch,smart_plug
    from core.event_bus import InMemoryEventBus
    from core import events as ev
    protocol='bambu' if source=='mqtt' else 'moonraker'
    engine,_=configure_dispatch(review_db,tmp_path,monkeypatch,protocol=protocol)
    names=[]
    def adapter(jid,path,name,creds,**options):
        names.append(name)
        return True,'Synthetic accepted'
    monkeypatch.setattr(dispatch,'_dispatch_bambu' if protocol=='bambu' else '_dispatch_moonraker',adapter)
    assert dispatch.dispatch_job(59,53)[0]
    @contextmanager
    def db():
        raw=engine.raw_connection()
        try:yield _ConnectionAdapter(raw,False)
        finally:raw.close()
    bus=InMemoryEventBus()
    bus.subscribe(ev.JOB_STARTED,smart_plug._on_job_started_event)
    from core import event_bus
    events=[]
    bus.subscribe(ev.JOB_STARTED,lambda event:events.append(event))
    monkeypatch.setattr(event_bus,'get_event_bus',lambda:bus)
    if source=='job_event':
        from modules.notifications import job_events
        monkeypatch.setattr(job_events,'get_db',db)
        monkeypatch.setattr(job_events,'get_event_bus',lambda:bus)
        identifier=job_events.job_started(59,names[0])
    elif source=='moonraker':
        from modules.printers.monitors import moonraker_monitor
        monkeypatch.setattr(moonraker_monitor,'engine',engine)
        monitor=moonraker_monitor.MoonrakerMonitor(59,'Synthetic','127.0.0.1')
        monitor._job_started(SimpleNamespace(filename=names[0],total_layers=10,bed_target=60,nozzle_target=200))
        identifier=monitor._current_job_db_id
    else:
        from modules.printers.monitors import mqtt_job_lifecycle
        monkeypatch.setattr(mqtt_job_lifecycle,'get_db',db)
        identifier,_,_=mqtt_job_lifecycle.record_job_started(59,'Synthetic',{'subtask_name':names[0],'gcode_file':'Metadata/plate_1.gcode'},lambda **kwargs:None,lambda:None)
    assert identifier is not None
    with engine.connect() as conn:
        linked=conn.execute(text('SELECT scheduled_job_id FROM print_jobs WHERE id=:id'),{'id':identifier}).scalar_one()
        bound=conn.execute(text('SELECT observed_print_job_id FROM dispatch_attempt_receipts WHERE job_id=53')).scalar_one()
    assert linked==53, 'Exact admitted transport name must link its own job, not a generic candidate'
    assert bound==identifier, 'Actual persisted monitor start must bind the receipt'
    assert len(events)==1 and events[0].data['print_job_id']==identifier
    if source=='mqtt':
        with engine.connect() as conn:
            assert conn.execute(text('SELECT filename FROM print_jobs WHERE id=:id'),{'id':identifier}).scalar_one()=='Metadata/plate_1.gcode'

@pytest.mark.parametrize('reported_name',['Owned dispatch test','odin-dispatch-ffffffffffffffffffffffffffffffff.3mf'])
def test_retained_claim_quarantines_mqtt_generic_matching_and_bumps(review_db,tmp_path,monkeypatch,reported_name):
    from core.db_utils import _ConnectionAdapter
    from modules.printers import dispatch
    from modules.printers.monitors import mqtt_job_lifecycle
    engine,_=configure_dispatch(review_db,tmp_path,monkeypatch,protocol='bambu')
    monkeypatch.setattr(dispatch,'_dispatch_bambu',lambda *args,**kwargs:(True,'Synthetic accepted'))
    assert dispatch.dispatch_job(59,53)[0]
    with engine.connect() as conn:
        before=conn.execute(text('SELECT id,status,printer_id FROM jobs WHERE id IN (53,54) ORDER BY id')).all()
    @contextmanager
    def db():
        raw=engine.raw_connection()
        try:yield _ConnectionAdapter(raw,False)
        finally:raw.close()
    monkeypatch.setattr(mqtt_job_lifecycle,'get_db',db)
    alerts=[];reschedules=[]
    identifier,linked,_=mqtt_job_lifecycle.record_job_started(59,'Synthetic',{'subtask_name':reported_name,'gcode_file':'Metadata/plate_1.gcode'},lambda **kwargs:alerts.append(kwargs),lambda:reschedules.append(True))
    assert identifier is not None
    with engine.connect() as conn:
        jobs=conn.execute(text('SELECT id,status,printer_id FROM jobs WHERE id IN (53,54) ORDER BY id')).all()
        receipt=conn.execute(text('SELECT observed_print_job_id FROM dispatch_attempt_receipts WHERE job_id=53')).scalar_one()
    assert linked is None,'Retained attempt must prevent fuzzy attribution of an unrelated start'
    assert jobs==before,'Uncorrelated start must not sweep or bump retained work'
    assert receipt is None
    assert not alerts and not reschedules


@pytest.mark.parametrize('source',['job_event','moonraker','mqtt'])
@pytest.mark.parametrize('fault',['capture','publication','repeat','terminal'])
def test_monitor_atomicity_and_resume_boundaries(review_db,tmp_path,monkeypatch,source,fault):
    from core.db_utils import _ConnectionAdapter
    from core import event_bus
    from modules.printers import dispatch
    protocol='bambu' if source=='mqtt' else 'moonraker'
    engine,_=configure_dispatch(review_db,tmp_path,monkeypatch,protocol=protocol)
    names=[]
    def adapter(*args,**kwargs):names.append(args[2]);return True,'Synthetic accepted'
    monkeypatch.setattr(dispatch,'_dispatch_bambu' if protocol=='bambu' else '_dispatch_moonraker',adapter)
    assert dispatch.dispatch_job(59,53)[0]
    with engine.connect() as conn:before=conn.execute(text('SELECT status FROM jobs WHERE id=53')).scalar_one()
    @contextmanager
    def db():
        raw=engine.raw_connection()
        try:yield _ConnectionAdapter(raw,False)
        finally:raw.close()
    def fail(*args,**kwargs):raise RuntimeError('Synthetic fault')
    if source=='job_event':
        from modules.notifications import job_events as module
        monkeypatch.setattr(module,'get_db',db)
        def start():return module.job_started(59,names[0])
    elif source=='mqtt':
        from modules.printers.monitors import mqtt_job_lifecycle as module
        monkeypatch.setattr(module,'get_db',db)
        def start():return module.record_job_started(59,'Synthetic',{'subtask_name':names[0],'gcode_file':'Metadata/plate_1.gcode'},lambda **kw:None,lambda:None)[0]
    else:
        from modules.printers.monitors import moonraker_monitor as module
        monkeypatch.setattr(module,'engine',engine)
        def start():
            monitor=module.MoonrakerMonitor(59,'Synthetic','127.0.0.1')
            monitor._job_started(SimpleNamespace(filename=names[0],total_layers=10,bed_target=60,nozzle_target=200))
            return monitor._current_job_db_id
    if fault=='capture':
        from modules.archives import services
        monkeypatch.setattr(services,'capture_attempt_spools',fail)
    if fault=='publication':
        broken=SimpleNamespace(publish=fail)
        monkeypatch.setattr(event_bus,'get_event_bus',lambda:broken)
        if source=='job_event':monkeypatch.setattr(module,'get_event_bus',lambda:broken)
    if fault=='terminal':
        with engine.begin() as conn:conn.execute(text("UPDATE jobs SET status='completed' WHERE id=53"))
    identifier=start()
    if fault=='repeat':
        with engine.begin() as conn:conn.execute(text("UPDATE print_jobs SET observed_received_at='2001-01-01 00:00:00' WHERE id=:id"),{'id':identifier})
        assert start()==identifier
    with engine.connect() as conn:
        rows=conn.execute(text('SELECT id,scheduled_job_id,observed_received_at FROM print_jobs')).all()
        bound=conn.execute(text('SELECT observed_print_job_id FROM dispatch_attempt_receipts')).scalar_one()
        status=conn.execute(text('SELECT status FROM jobs WHERE id=53')).scalar_one()
    if fault=='capture':
        assert identifier is None and rows==[] and bound is None and status==before
    elif fault=='terminal':
        assert identifier is not None and len(rows)==1 and rows[0][1] is None and bound is None and status=='completed'
    else:
        assert identifier is not None and len(rows)==1 and bound==identifier and rows[0][1]==53
        if fault=='repeat':assert str(rows[0][2]).startswith('2001-01-01')


def test_bambu_wrapper_cannot_resume_bound_attempt_from_unrelated_task_name(review_db,tmp_path,monkeypatch):
    from core.db_utils import _ConnectionAdapter
    from modules.printers import dispatch
    from modules.printers.monitors import mqtt_job_lifecycle,mqtt_printer
    engine,_=configure_dispatch(review_db,tmp_path,monkeypatch,protocol='bambu')
    names=[]
    def adapter(*args,**kwargs):names.append(args[2]);return True,'Synthetic accepted'
    monkeypatch.setattr(dispatch,'_dispatch_bambu',adapter)
    assert dispatch.dispatch_job(59,53)[0]
    @contextmanager
    def db():
        raw=engine.raw_connection()
        try:yield _ConnectionAdapter(raw,False)
        finally:raw.close()
    monkeypatch.setattr(mqtt_job_lifecycle,'get_db',db)
    monkeypatch.setattr(mqtt_printer,'get_db',db)
    identifier,_,_=mqtt_job_lifecycle.record_job_started(59,'Synthetic',{'subtask_name':names[0],'gcode_file':'Metadata/plate_1.gcode'},lambda **kw:None,lambda:None)
    assert identifier is not None
    monitor=mqtt_printer.PrinterMonitor(59,'Synthetic','127.0.0.1','SYNTHETICPRINTER','synthetic')
    monitor._state={'subtask_name':'Unrelated task','gcode_file':'Metadata/plate_1.gcode'}
    assert monitor._job_started()
    assert monitor._current_job_id!=identifier and monitor._linked_job_id is None
    with engine.connect() as conn:
        assert conn.execute(text('SELECT observed_print_job_id FROM dispatch_attempt_receipts')).scalar_one()==identifier

@pytest.mark.parametrize('state',['RUNNING','IDLE','MATCHED'])
def test_bambu_orphan_recovery_preserves_unresolved_claim_without_exact_current_identity(review_db,tmp_path,monkeypatch,state):
    from core.db_utils import _ConnectionAdapter
    from modules.printers import dispatch
    from modules.printers.monitors import mqtt_job_lifecycle,mqtt_printer
    engine,_=configure_dispatch(review_db,tmp_path,monkeypatch,protocol='bambu')
    names=[]
    def adapter(*args,**kwargs):names.append(args[2]);return True,'Synthetic accepted'
    monkeypatch.setattr(dispatch,'_dispatch_bambu',adapter)
    assert dispatch.dispatch_job(59,53)[0]
    @contextmanager
    def db():
        raw=engine.raw_connection()
        try:yield _ConnectionAdapter(raw,False)
        finally:raw.close()
    monkeypatch.setattr(mqtt_job_lifecycle,'get_db',db);monkeypatch.setattr(mqtt_printer,'get_db',db)
    identifier,_,_=mqtt_job_lifecycle.record_job_started(59,'Synthetic',{'subtask_name':names[0],'gcode_file':'Metadata/plate_1.gcode'},lambda **kw:None,lambda:None)
    monitor=mqtt_printer.PrinterMonitor(59,'Synthetic','127.0.0.1','SYNTHETICPRINTER','synthetic')
    monitor._status_received.set()
    monitor._state={'gcode_state':'RUNNING' if state=='MATCHED' else state,'subtask_name':names[0] if state=='MATCHED' else 'Unrelated task','gcode_file':'Metadata/plate_1.gcode'}
    monitor._recover_orphaned_jobs()
    if state=='MATCHED':assert monitor._current_job_id==identifier and monitor._linked_job_id==53
    else:assert monitor._current_job_id is None and monitor._linked_job_id is None
    with engine.connect() as conn:
        assert conn.execute(text('SELECT status FROM print_jobs WHERE id=:id'),{'id':identifier}).scalar_one()=='running'
        assert conn.execute(text('SELECT observed_print_job_id FROM dispatch_attempt_receipts')).scalar_one()==identifier


def test_exact_mqtt_start_preserves_existing_ams_remaining_snapshot(review_db,tmp_path,monkeypatch):
    from core.db_utils import _ConnectionAdapter
    from modules.printers import dispatch
    from modules.printers.monitors import mqtt_job_lifecycle
    engine,_=configure_dispatch(review_db,tmp_path,monkeypatch,protocol='bambu')
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO filament_library(id,brand,name) VALUES(700,'Synthetic','PLA')"))
        conn.execute(text('INSERT INTO spools(id,filament_id,initial_weight_g,remaining_weight_g,org_id) VALUES(700,700,1000,800,1)'))
        conn.execute(text('UPDATE filament_slots SET assigned_spool_id=700 WHERE printer_id=59'))
    names=[]
    def adapter(*args,**kwargs):names.append(args[2]);return True,'Synthetic accepted'
    monkeypatch.setattr(dispatch,'_dispatch_bambu',adapter)
    assert dispatch.dispatch_job(59,53)[0]
    @contextmanager
    def db():
        raw=engine.raw_connection()
        try:yield _ConnectionAdapter(raw,False)
        finally:raw.close()
    monkeypatch.setattr(mqtt_job_lifecycle,'get_db',db)
    identifier,_,weights=mqtt_job_lifecycle.record_job_started(59,'Synthetic',{'subtask_name':names[0],'gcode_file':'Metadata/plate_1.gcode','_ams_remain':{1:80}},lambda **kw:None,lambda:None)
    assert weights=={700:{'remain_pct':80,'initial_weight_g':1000,'slot':1}}

    with engine.begin() as conn:conn.execute(text('UPDATE spools SET initial_weight_g=900,remaining_weight_g=200 WHERE id=700'))
    def no_recapture(*args,**kwargs):raise AssertionError('Resume must not recapture current spool levels')
    monkeypatch.setattr(mqtt_job_lifecycle,'capture_mqtt_start_weights',no_recapture)
    resumed,_,saved=mqtt_job_lifecycle.record_job_started(59,'Synthetic',{'subtask_name':names[0],'gcode_file':'Metadata/plate_1.gcode','_ams_remain':{1:20}},lambda **kw:None,lambda:None)
    assert resumed==identifier and saved==weights


@pytest.mark.parametrize('mismatch',['reassigned','foreign'])
def test_first_start_weights_remain_unknown_when_captured_identity_or_owner_disagrees(review_db,tmp_path,monkeypatch,mismatch):
    import json
    from core.db_utils import _ConnectionAdapter
    from modules.printers import dispatch
    from modules.printers.monitors import mqtt_job_lifecycle
    engine,_=configure_dispatch(review_db,tmp_path,monkeypatch,protocol='bambu')
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO filament_library(id,brand,name) VALUES(700,'Synthetic','PLA')"))
        conn.execute(text('INSERT INTO spools(id,filament_id,initial_weight_g,remaining_weight_g,org_id) VALUES(700,700,1000,800,:owner),(701,700,900,600,1)'),{'owner':2 if mismatch=='foreign' else 1})
        conn.execute(text('UPDATE filament_slots SET assigned_spool_id=700 WHERE printer_id=59'))
    names=[]
    def adapter(*args,**kwargs):names.append(args[2]);return True,'Synthetic accepted'
    monkeypatch.setattr(dispatch,'_dispatch_bambu',adapter)
    assert dispatch.dispatch_job(59,53)[0]
    @contextmanager
    def db():
        raw=engine.raw_connection()
        try:yield _ConnectionAdapter(raw,False)
        finally:raw.close()
    monkeypatch.setattr(mqtt_job_lifecycle,'get_db',db)
    if mismatch=='reassigned':
        from modules.archives import services
        original=services.capture_attempt_spools
        def changed(conn,*args,**kwargs):
            result=original(conn,*args,**kwargs)
            conn.execute('UPDATE filament_slots SET assigned_spool_id=701 WHERE printer_id=59')
            return result
        monkeypatch.setattr(services,'capture_attempt_spools',changed)
    identifier,_,weights=mqtt_job_lifecycle.record_job_started(59,'Synthetic',{'subtask_name':names[0],'gcode_file':'Metadata/plate_1.gcode','_ams_remain':{1:80}},lambda **kw:None,lambda:None)
    assert identifier is not None and weights=={}
    with engine.connect() as conn:
        snapshot=json.loads(conn.execute(text('SELECT spool_snapshot FROM print_jobs WHERE id=:id'),{'id':identifier}).scalar_one())
        if mismatch=='reassigned':
            assert conn.execute(text('SELECT assigned_spool_id FROM filament_slots WHERE printer_id=59')).scalar_one()==701
    assert snapshot['slots'][0]['spool_id']==700 and snapshot['mqtt_start_weights']=={}
