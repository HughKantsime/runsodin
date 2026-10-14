"""Auto-off timer lifecycle; no timer threads, networks or physical commands."""
import pytest
from tests.test_contracts.test_education_review_workflow import review_db

@pytest.fixture
def auto_power(monkeypatch):
    from modules.printers import smart_plug as module
    timers=[];calls=[]
    config={'type':'tasmota','host':'192.0.2.1','entity_id':None,'auth_token':None,'auto_on':False,'auto_off':True,'cooldown_minutes':0,'power_state':True}
    class Timer:
        def __init__(self,delay,callback):self.delay=delay;self.callback=callback;self.cancelled=False;timers.append(self)
        def start(self):pass
        def cancel(self):self.cancelled=True
    monkeypatch.setattr(module,'_cooldown_timers',{})
    monkeypatch.setattr(module.threading,'Timer',Timer)
    monkeypatch.setattr(module,'get_plug_config',lambda pid:dict(config))
    monkeypatch.setattr(module,'_auto_off_safe',lambda pid:True)
    monkeypatch.setattr(module,'power_off',lambda pid,**kwargs:calls.append(('off',pid)))
    monkeypatch.setattr(module,'power_on',lambda pid:calls.append(('on',pid)))
    return module,config,timers,calls


def test_new_print_cancels_pending_off_even_when_auto_on_disabled(auto_power):
    module,config,timers,calls=auto_power
    module.on_print_complete(599)
    module.on_print_start(599)
    assert timers[0].cancelled
    assert 599 not in module._cooldown_timers and calls==[]


def test_cancelled_timer_callback_cannot_power_off_or_remove_new_timer(auto_power):
    module,config,timers,calls=auto_power
    module.on_print_complete(599)
    module.on_print_complete(599)
    assert timers[0].cancelled
    timers[0].callback()
    assert calls==[] and module._cooldown_timers[599] is timers[1]


@pytest.mark.parametrize('change',['disabled','host','token','active'])
def test_timer_refuses_changed_configuration_or_active_print(auto_power,monkeypatch,change):
    module,config,timers,calls=auto_power
    module.on_print_complete(599)
    if change=='disabled':config['auto_off']=False
    elif change=='host':config['host']='192.0.2.2'
    elif change=='token':config['auth_token']='synthetic replacement'
    else:monkeypatch.setattr(module,'_auto_off_safe',lambda pid:False)
    timers[0].callback()
    assert calls==[] and 599 not in module._cooldown_timers


def test_timer_can_fire_immediately_without_leaving_registry_entry(auto_power,monkeypatch):
    module,config,timers,calls=auto_power
    class Immediate:
        def __init__(self,delay,callback):self.callback=callback
        def start(self):self.callback()
        def cancel(self):pass
    monkeypatch.setattr(module.threading,'Timer',Immediate)
    module.on_print_complete(599)
    assert calls==[('off',599)] and 599 not in module._cooldown_timers


@pytest.mark.parametrize('failure',['start','device'])
def test_timer_failure_cleans_up_without_private_exception_logging(auto_power,monkeypatch,caplog,failure):
    module,config,timers,calls=auto_power
    def refuse(*args,**kwargs):raise RuntimeError('synthetic private marker')
    if failure=='start':
        class Failing:
            def __init__(self,delay,callback):pass
            start=refuse
            def cancel(self):pass
        monkeypatch.setattr(module.threading,'Timer',Failing)
    else:monkeypatch.setattr(module,'power_off',refuse)
    module.on_print_complete(599)
    if failure=='device':timers[0].callback()
    assert 599 not in module._cooldown_timers
    assert 'synthetic private marker' not in caplog.text


@pytest.mark.parametrize('state,age,expected',[('IDLE',0,True),('FINISH',0,True),('RUNNING',0,False),('PAUSE',0,False),('OFFLINE',0,False),(None,0,False),('IDLE',120,False),('IDLE',-120,False)])
def test_actual_database_auto_off_gate_requires_recent_idle_state(review_db,monkeypatch,state,age,expected):
    from datetime import datetime,timezone,timedelta
    from sqlalchemy import text
    from modules.printers import smart_plug as module
    review_db.execute(text("INSERT INTO printers(id,name,gcode_state,last_seen) VALUES(599,'Synthetic gate',:state,:stamp)"),{'state':state,'stamp':datetime.now(timezone.utc)-timedelta(seconds=age)});review_db.commit()
    monkeypatch.setattr(module,'engine',review_db.get_bind())
    assert module._auto_off_safe(599) is expected


def test_actual_active_job_refuses_auto_off_even_when_printer_looks_idle(review_db,monkeypatch):
    from datetime import datetime,timezone
    from sqlalchemy import text
    from modules.printers import smart_plug as module
    review_db.execute(text("INSERT INTO printers(id,name,gcode_state,last_seen) VALUES(599,'Synthetic active gate','IDLE',:stamp)"),{'stamp':datetime.now(timezone.utc)})
    review_db.execute(text("INSERT INTO jobs(id,item_name,printer_id,status) VALUES(599,'Synthetic paused',599,'paused')"));review_db.commit()
    monkeypatch.setattr(module,'engine',review_db.get_bind())
    assert module._auto_off_safe(599) is False


def test_real_event_bus_completion_then_start_cancels_pending_off(auto_power):
    from core.event_bus import InMemoryEventBus
    from core.interfaces.event_bus import Event
    from core import events
    module,config,timers,calls=auto_power
    bus=InMemoryEventBus();module.register_subscribers(bus)
    bus.publish(Event(events.JOB_COMPLETED,'synthetic',{'printer_id':599,'success':True}))
    assert len(timers)==1
    bus.publish(Event(events.JOB_STARTED,'synthetic',{'printer_id':599}))
    assert timers[0].cancelled and calls==[]
    timers[0].callback()
    assert calls==[]


def test_blocked_off_for_one_printer_does_not_block_another_printer_start(auto_power,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    module,config,timers,calls=auto_power;entered=Event();release=Event()
    def blocked(pid,**kwargs):
        entered.set()
        assert release.wait(5)
        calls.append(('off',pid))
    monkeypatch.setattr(module,'power_off',blocked)
    module.on_print_complete(599)
    module.on_print_complete(600)
    with ThreadPoolExecutor(max_workers=2) as pool:
        off=pool.submit(timers[0].callback)
        try:
            assert entered.wait(5)
            start=pool.submit(module.on_print_start,600)
            start.result(timeout=2)
            assert timers[1].cancelled and 600 not in module._cooldown_timers
        finally:release.set()
        off.result(timeout=5)
    assert calls==[('off',599)]


def test_auto_off_disabled_at_final_config_fetch_sends_no_device_command(monkeypatch):
    from types import SimpleNamespace
    from modules.printers import smart_plug as module
    callbacks=[];calls=[];fetches=[]
    config={'type':'tasmota','host':'192.0.2.1','entity_id':None,'auth_token':None,'auto_on':False,'auto_off':True,'cooldown_minutes':0}
    def current(pid):
        fetches.append(pid)
        return {**config,'auto_off':len(fetches)<3}
    def timer(delay,callback):
        callbacks.append(callback)
        return SimpleNamespace(start=lambda:None,cancel=lambda:None)
    monkeypatch.setattr(module,'_cooldown_timers',{})
    monkeypatch.setattr(module.threading,'Timer',timer)
    monkeypatch.setattr(module,'get_plug_config',current)
    monkeypatch.setattr(module,'_auto_off_safe',lambda pid:True)
    monkeypatch.setattr(module,'tasmota_power',lambda *args:(calls.append(args) or False))
    monkeypatch.setattr(module,'_update_power_state',lambda *args:None)
    module.on_print_complete(599)
    callbacks[0]()
    assert len(fetches)==3 and calls==[]
    # The setting controls automation, not an explicit manual command.
    assert module.power_off(599) is False
    assert calls==[('192.0.2.1','OFF')]
