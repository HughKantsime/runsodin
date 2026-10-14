"""Actual monitor completion and monitor bootstrap, with inert owned timers."""
from contextlib import contextmanager
from types import SimpleNamespace
import pytest
from sqlalchemy import text
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_smart_plug_auto_power_behavior import auto_power
from tests.test_contracts.test_education_monitor_policy import monitor_db


@pytest.fixture
def terminal_environment(review_db, auto_power, monkeypatch):
    from core import db as core_db, event_bus
    from core.db_utils import _ConnectionAdapter
    from core.event_bus import InMemoryEventBus
    from core.monitor_bootstrap import bootstrap_monitor_authority
    from core.registry import registry
    monkeypatch.setattr(registry, "_providers", dict(registry._providers))
    from modules.notifications import event_dispatcher, alert_dispatch
    from modules.archives import archive
    from modules.printers.monitors import mqtt_job_lifecycle, moonraker_monitor
    engine = review_db.get_bind()
    monkeypatch.setattr(core_db, 'engine', engine)
    monkeypatch.setattr(moonraker_monitor, 'engine', engine)
    @contextmanager
    def database():
        raw = engine.raw_connection()
        try:
            yield _ConnectionAdapter(raw, False)
        finally:
            raw.close()
    monkeypatch.setattr(mqtt_job_lifecycle, 'get_db', database)
    for name in ('dispatch_alert', 'record_error', 'increment_care_counters'):
        monkeypatch.setattr(event_dispatcher, name, lambda *args, **kwargs:None)
    monkeypatch.setattr(alert_dispatch, 'dispatch_alert', lambda *args, **kwargs:None)
    monkeypatch.setattr(alert_dispatch, '_start_bed_cooled_monitor', lambda *args, **kwargs:None)
    monkeypatch.setattr(archive, 'create_print_archive', lambda *args, **kwargs:None)
    bus = InMemoryEventBus()
    monkeypatch.setattr(event_bus, '_bus', bus)
    bootstrap_monitor_authority()
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO printers(id,name,is_active,api_type,api_host,org_id,gcode_state,last_seen) VALUES(551,'Owned terminal',TRUE,'moonraker','127.0.0.1',1,'FINISH',CURRENT_TIMESTAMP)"))
        conn.execute(text("INSERT INTO jobs(id,item_name,status,printer_id,charged_to_org_id,quantity) VALUES(552,'Owned terminal','printing',551,1,1)"))
        conn.execute(text("INSERT INTO print_jobs(id,printer_id,scheduled_job_id,filename,job_name,status,started_at) VALUES(553,551,552,'owned.gcode','Owned terminal','running',CURRENT_TIMESTAMP)"))
    module,config,timers,calls = auto_power
    return engine, bus, timers, calls, mqtt_job_lifecycle, moonraker_monitor


def test_real_monitor_bootstrap_installs_power_subscribers_once(terminal_environment):
    from core import events
    from core.monitor_bootstrap import bootstrap_monitor_authority
    _,bus,_,_,_,_ = terminal_environment
    bootstrap_monitor_authority()
    assert len(bus._handlers[events.JOB_STARTED]) == 1
    assert len(bus._handlers[events.JOB_COMPLETED]) == 1


@pytest.mark.parametrize('source', ['mqtt', 'moonraker'])
@pytest.mark.parametrize('status', ['completed', 'failed', 'cancelled'])
def test_actual_committed_monitor_terminal_reaches_cooldown_once(terminal_environment, source, status):
    engine,bus,timers,calls,mqtt,moon = terminal_environment
    if source == 'mqtt':
        mqtt.record_job_ended(551,'Owned terminal',553,552,status,{'subtask_name':'Owned terminal'},{},lambda **kwargs:None)
        mqtt.record_job_ended(551,'Owned terminal',553,552,status,{'subtask_name':'Owned terminal'},{},lambda **kwargs:None)
    else:
        monitor = moon.MoonrakerMonitor(551,'Owned terminal','127.0.0.1')
        monitor._current_job_db_id = 553
        monitor._last_filename = 'owned.gcode'
        state = SimpleNamespace(filename='owned.gcode',filament_used_mm=0,error_message=None)
        monitor._job_ended(status,state)
        monitor._current_job_db_id = 553
        monitor._job_ended(status,state)
    with engine.connect() as conn:
        assert conn.execute(text('SELECT status FROM print_jobs WHERE id=553')).scalar_one() == status
    assert len(timers) == (1 if status == 'completed' else 0)
    assert calls == []


def test_monitor_bootstrap_failure_does_not_register_power(monkeypatch):
    from core import event_bus
    from core.event_bus import InMemoryEventBus
    from core.monitor_bootstrap import bootstrap_monitor_authority
    from modules import organizations
    bus = InMemoryEventBus()
    monkeypatch.setattr(event_bus, '_bus', bus)
    def fail(_):
        raise RuntimeError('owned initialization failure')
    monkeypatch.setattr(organizations, 'register_providers', fail)
    with pytest.raises(RuntimeError, match='Education authority initialization failed'):
        bootstrap_monitor_authority()
    assert not any(bus._handlers.values())


@pytest.mark.parametrize('source', ['mqtt', 'moonraker'])
def test_completion_subscriber_observes_committed_state_despite_optional_failure(terminal_environment, monkeypatch, source):
    from core import events
    from modules.notifications import event_dispatcher, alert_dispatch
    engine,bus,timers,_,mqtt,moon = terminal_environment
    observed = []
    def observe(event):
        with engine.connect() as conn:
            observed.append((event.data['print_job_id'], conn.execute(text('SELECT status FROM print_jobs WHERE id=553')).scalar_one()))
    bus.subscribe(events.JOB_COMPLETED, observe)
    def fail(*args, **kwargs):
        raise RuntimeError('owned optional failure')
    if source == 'mqtt':
        monkeypatch.setattr(alert_dispatch, 'dispatch_alert', fail)
        mqtt.record_job_ended(551,'Owned terminal',553,552,'completed',{'subtask_name':'Owned terminal'},{},lambda **kwargs:None)
    else:
        monkeypatch.setattr(event_dispatcher, 'dispatch_alert', fail)
        monitor = moon.MoonrakerMonitor(551,'Owned terminal','127.0.0.1')
        monitor._current_job_db_id = 553
        monitor._last_filename = 'owned.gcode'
        monitor._job_ended('completed',SimpleNamespace(filename='owned.gcode',filament_used_mm=0,error_message=None))
    assert observed == [(553,'completed')]
    assert len(timers) == 1


@pytest.mark.parametrize('source', ['mqtt', 'moonraker'])
def test_commit_failure_does_not_schedule_completion(terminal_environment, monkeypatch, source):
    from sqlalchemy import event
    from core.db_utils import _ConnectionAdapter
    engine,_,timers,_,mqtt,moon = terminal_environment
    if source == 'mqtt':
        class FailingCommit(_ConnectionAdapter):
            def commit(self):
                raise RuntimeError('owned commit failure')
        @contextmanager
        def database():
            raw = engine.raw_connection()
            try:
                yield FailingCommit(raw, False)
            finally:
                raw.close()
        monkeypatch.setattr(mqtt, 'get_db', database)
        mqtt.record_job_ended(551,'Owned terminal',553,552,'completed',{'subtask_name':'Owned terminal'},{},lambda **kwargs:None)
    else:
        def fail_commit(conn):
            state = conn.execute(text('SELECT status FROM print_jobs WHERE id=553')).scalar_one()
            if state == 'completed':
                raise RuntimeError('owned commit failure')
        event.listen(engine, 'commit', fail_commit)
        try:
            monitor = moon.MoonrakerMonitor(551,'Owned terminal','127.0.0.1')
            monitor._current_job_db_id = 553
            monitor._last_filename = 'owned.gcode'
            monitor._job_ended('completed',SimpleNamespace(filename='owned.gcode',filament_used_mm=0,error_message=None))
        finally:
            event.remove(engine, 'commit', fail_commit)
    with engine.connect() as conn:
        assert conn.execute(text('SELECT status FROM print_jobs WHERE id=553')).scalar_one() == 'running'
    assert timers == []


@pytest.fixture
def education_terminal_environment(monitor_db, auto_power, monkeypatch):
    from core import event_bus
    from core.db_utils import _ConnectionAdapter
    from core.monitor_bootstrap import bootstrap_monitor_authority
    from core.registry import registry
    from modules.printers.monitors import mqtt_job_lifecycle, moonraker_monitor
    from modules.notifications import job_events
    from tests.test_contracts.test_education_monitor_policy import _reserve, _observation
    from modules.organizations.education_policy import confirm_dispatch_started, claim_monitor_observation
    engine = monitor_db.get_bind()
    monkeypatch.setattr(registry, '_providers', dict(registry._providers))
    bootstrap_monitor_authority()
    reservation = _reserve(monitor_db)
    confirmed = confirm_dispatch_started(monitor_db,claim_id=reservation['claim_id'],submission_id=14,job_id=13,printer_id=9,expected_revision=3)
    assert confirmed['transitioned'] is True
    identifier = _observation(monitor_db,reservation['remote_filename'])
    claimed = claim_monitor_observation(monitor_db,print_job_id=identifier,printer_id=9,observed_filename=reservation['remote_filename'])
    assert claimed['authorized'] is True
    monitor_db.commit()
    @contextmanager
    def database():
        raw = engine.raw_connection()
        try:
            yield _ConnectionAdapter(raw,False)
        finally:
            raw.close()
    monkeypatch.setattr(mqtt_job_lifecycle, 'get_db', database)
    monkeypatch.setattr(job_events, 'get_db', database)
    monkeypatch.setattr(moonraker_monitor, 'engine', engine)
    _,_,timers,calls = auto_power
    return engine,event_bus.get_event_bus(),timers,calls,identifier,mqtt_job_lifecycle,moonraker_monitor,job_events


@pytest.mark.parametrize('source', ['mqtt','moonraker','job_event'])
@pytest.mark.parametrize('status', ['completed','failed'])
def test_actual_education_terminal_reaches_registered_cooldown_once(education_terminal_environment,source,status):
    engine,bus,timers,calls,identifier,mqtt,moon,job_events = education_terminal_environment
    for _ in range(2):
        if source == 'mqtt':
            mqtt.record_job_ended(9,'Owned education',identifier,13,status,{}, {},lambda **kwargs:pytest.fail('Education must not send generic alerts'))
        elif source == 'moonraker':
            monitor = moon.MoonrakerMonitor(9,'Owned education','127.0.0.1')
            monitor._current_job_db_id = identifier
            monitor._last_filename = 'owned.gcode'
            monitor._job_ended(status,SimpleNamespace(filename='owned.gcode',filament_used_mm=0,error_message=None),education_terminal_status=status)
        else:
            job_events.job_completed(9,identifier,success=status=='completed')
    with engine.connect() as conn:
        assert conn.execute(text('SELECT status FROM print_jobs WHERE id=:id'),{'id':identifier}).scalar_one() == status
        assert conn.execute(text('SELECT status FROM education_submissions WHERE id=14')).scalar_one() == status
    assert len(timers) == (1 if status == 'completed' else 0)
    assert calls == []


def test_generic_job_event_duplicate_does_not_reset_cooldown(terminal_environment,monkeypatch):
    from modules.notifications import job_events
    _,_,timers,_,mqtt,_ = terminal_environment
    monkeypatch.setattr(job_events,'get_db',mqtt.get_db)
    for name in ('dispatch_alert','record_error','increment_care_counters','_start_bed_cooled_monitor'):
        monkeypatch.setattr(job_events,name,lambda *args, **kwargs:None)
    job_events.job_completed(551,553,success=True,duration_seconds=120)
    job_events.job_completed(551,553,success=True,duration_seconds=120)
    assert len(timers) == 1


def test_concurrent_generic_completion_has_one_event_and_preserves_payload(terminal_environment,monkeypatch):
    import threading
    from core import events
    from modules.notifications import job_events
    engine,bus,timers,_,mqtt,_ = terminal_environment
    monkeypatch.setattr(job_events,'get_db',mqtt.get_db)
    for name in ('dispatch_alert','record_error','increment_care_counters','_start_bed_cooled_monitor'):
        monkeypatch.setattr(job_events,name,lambda *args, **kwargs:None)
    received = []
    bus.subscribe(events.JOB_COMPLETED,received.append)
    barrier = threading.Barrier(2)
    original = job_events.prepare_print_job_write
    def prepare(*args,**kwargs):
        barrier.wait(timeout=5)
        return original(*args,**kwargs)
    monkeypatch.setattr(job_events,'prepare_print_job_write',prepare)
    threads = [threading.Thread(target=job_events.job_completed,args=(551,553),kwargs={'success':True,'duration_seconds':120}) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert all(not thread.is_alive() for thread in threads)
    assert len(timers) == len(received) == 1
    assert received[0].source_module == 'notifications'
    assert received[0].data == {'printer_id':551,'print_job_id':553,'scheduled_job_id':552,'status':'completed','success':True,'job_name':'Owned terminal','duration_seconds':120}
    with engine.connect() as conn:
        assert conn.execute(text('SELECT status FROM print_jobs WHERE id=553')).scalar_one() == 'completed'
