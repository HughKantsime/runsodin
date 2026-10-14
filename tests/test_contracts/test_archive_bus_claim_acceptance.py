"""Actual in-process subscription delivery; archive persistence tested separately."""
import pytest
from core.event_bus import InMemoryEventBus
from core.interfaces.event_bus import Event
from core import events
from modules.archives import archive

@pytest.mark.parametrize('kind,success,result',[(events.JOB_COMPLETED,True,None),(events.JOB_FAILED,False,None),(events.JOB_CANCELLED,False,'cancelled')])
def test_archive_subscriber_receives_terminal_event_once(monkeypatch,kind,success,result):
    monkeypatch.setattr(archive,'recover_missing_archives',lambda:{'attempted':0,'pending_count':0})
    calls=[]
    monkeypatch.setattr(archive,'create_print_archive',lambda *args,**kwargs:calls.append((args,kwargs)))
    bus=InMemoryEventBus();archive.register_subscribers(bus);archive.register_subscribers(bus)
    bus.publish(Event(kind,'notifications',{'printer_id':9,'print_job_id':701,'success':success}))
    assert len(calls)==1
    args,kwargs=calls[0]
    if result: assert args==(701,9) and kwargs=={'success':False,'result_status':'cancelled'}
    else: assert args==(701,9,success) and kwargs=={}

@pytest.mark.parametrize('data',[{}, {'printer_id':9}, {'print_job_id':701}])
def test_incomplete_terminal_identity_never_archives(monkeypatch,data):
    monkeypatch.setattr(archive,'recover_missing_archives',lambda:{'attempted':0,'pending_count':0})
    calls=[]
    monkeypatch.setattr(archive,'create_print_archive',lambda *a,**k:calls.append((a,k)))
    bus=InMemoryEventBus();archive.register_subscribers(bus)
    bus.publish(Event(events.JOB_COMPLETED,'notifications',data))
    assert calls==[]
