"""Actual standard-bus observers are asserted outside caught callback errors."""
import ast
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from tests.test_contracts.test_automatic_power_terminal_delivery import terminal_environment,education_terminal_environment
from tests.test_contracts.test_smart_plug_auto_power_behavior import auto_power
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_education_monitor_policy import monitor_db

ROOT=Path(__file__).resolve().parents[2]


_NEW_IMPORT='from core.committed_terminal_events import publish_committed_completion'
_OLD_IMPORT='from modules.printers.committed_terminal_events import publish_committed_completion'
_BOUNDARY=ROOT/'tests/fixtures/certification/committed-completion-boundary'


def _sha256(data):
    return hashlib.sha256(data if isinstance(data,bytes) else data.encode()).hexdigest()


def test_same_object_and_exact_pre_extraction_body():
    """The extraction moved the publisher into core and changed only job_events' import line.

    Proved against committed fixtures, independent of later job_events changes:
    job_events-before.py is the pre-extraction body (its hash is the original pin)
    and imports the publisher from the old location exactly once; the publisher
    moved to core byte-for-byte (publisher-before.py).
    """
    from core.committed_terminal_events import publish_committed_completion as core
    from modules.printers.committed_terminal_events import publish_committed_completion as old
    assert core is old
    before=json.loads((_BOUNDARY/'source-before.json').read_text())
    publisher=(_BOUNDARY/'publisher-before.py').read_bytes()
    assert _sha256(publisher)==before['publisher_sha256']
    assert (ROOT/'backend/core/committed_terminal_events.py').read_bytes()==publisher
    body=(_BOUNDARY/'job_events-before.py').read_text()
    # Byte hashes are interpreter-independent (ast.dump differs between 3.11 and 3.14).
    assert _sha256(body)==before['job_events_sha256']
    assert body.count(_OLD_IMPORT)==1 and _NEW_IMPORT not in body
    assert set(before)=={'publisher_sha256','job_events_sha256'}


def test_current_job_events_source_is_the_reviewed_revision():
    """Pin of the reviewed job_events.py; a change needs review and an update of source-current.json."""
    current=(ROOT/'backend/modules/notifications/job_events.py').read_text()
    assert current.count(_NEW_IMPORT)==1 and _OLD_IMPORT not in current
    assert _sha256(current)==json.loads((_BOUNDARY/'source-current.json').read_text())['job_events_sha256']


@pytest.mark.parametrize('args,kwargs',[((True,1),{}),((0,1),{}),((1,-1),{}),((1,True),{}),((1,1),{'source_module':'foreign'}),((1,1),{'metadata':{'extra':'value'}}),((1,1),{'metadata':[]})])
def test_invalid_input_refuses_before_bus(args,kwargs,monkeypatch):
    from core import event_bus
    from core.committed_terminal_events import publish_committed_completion
    monkeypatch.setattr(event_bus,'get_event_bus',lambda:pytest.fail('invalid input touched bus'))
    assert publish_committed_completion(*args,**kwargs) is False


def _collect(engine,records,*,fault=None):
    def collect(event):
        record={'event':event,'success':False,'state':None,'error':None,'claim_count':None}
        records.append(record)  # Record before lookup/assertion can be swallowed.
        try:
            if fault=='lookup':raise RuntimeError('owned observer fault')
            with engine.connect() as observer:
                record['state']=observer.execute(text('SELECT status FROM print_jobs WHERE id=:id'),{'id':event.data['print_job_id']}).scalar_one()
                record['claim_count']=observer.execute(text('SELECT count(*) FROM printer_dispatch_claims WHERE printer_id=:id'),{'id':event.data['printer_id']}).scalar_one()
            assert record['state']=='completed'
            record['success']=True
        except Exception as error:
            record['error']=type(error).__name__
            raise
    return collect


def _assert_records(records):
    assert records and all(r['success'] and r['error'] is None and r['state']=='completed' for r in records),'Recorded observer timing failure'


@pytest.mark.parametrize('fault',[None,'precommit','lookup'])
def test_real_standard_bus_swallowed_failure_still_red_outside(terminal_environment,fault):
    from core import events
    from core.committed_terminal_events import publish_committed_completion
    engine,bus,_,_,_,_=terminal_environment
    if fault is None:
        with engine.begin() as db:db.execute(text("UPDATE print_jobs SET status='completed' WHERE id=553"))
    records=[];bus.subscribe(events.JOB_COMPLETED,_collect(engine,records,fault=fault))
    assert publish_committed_completion(551,553,552,metadata={'job_name':'Owned terminal','duration_seconds':1.25},source_module='notifications') is True
    assert len(records)==1
    event=records[0]['event']
    assert event.source_module=='notifications'
    assert event.data=={'printer_id':551,'print_job_id':553,'scheduled_job_id':552,'status':'completed','success':True,'job_name':'Owned terminal','duration_seconds':1.25}
    if fault is None:_assert_records(records)
    else:
        assert not records[0]['success'] and records[0]['error'] is not None
        with pytest.raises(AssertionError,match='Recorded observer timing failure'):_assert_records(records)


@pytest.mark.parametrize('source',['mqtt','moonraker','notifications'])
@pytest.mark.parametrize('status',['completed','failed'])
def test_actual_three_producers_committed_and_duplicate_events(terminal_environment,monkeypatch,source,status):
    from core import events
    from core.db_utils import _ConnectionAdapter
    from modules.notifications import job_events
    engine,bus,_,_,mqtt,moon=terminal_environment
    @contextmanager
    def raw_db():
        raw=engine.raw_connection()
        try:yield _ConnectionAdapter(raw,False)
        finally:raw.close()
    monkeypatch.setattr(job_events,'get_db',raw_db)
    monkeypatch.setattr(job_events,'get_event_bus',lambda:bus)
    monkeypatch.setattr(job_events,'_start_bed_cooled_monitor',lambda *a:None)
    records=[];bus.subscribe(events.JOB_COMPLETED,_collect(engine,records))
    for _ in range(2):
        if source=='mqtt':mqtt.record_job_ended(551,'Owned terminal',553,552,status,{'subtask_name':'Owned terminal'},{},lambda **kw:None)
        elif source=='moonraker':
            monitor=moon.MoonrakerMonitor(551,'Owned terminal','127.0.0.1');monitor._current_job_db_id=553;monitor._last_filename='owned.gcode'
            monitor._job_ended(status,SimpleNamespace(filename='owned.gcode',filament_used_mm=0,error_message=None))
        else:job_events.job_completed(551,553,success=status=='completed')
    if status=='completed':
        assert len(records)==1;_assert_records(records)
        event=records[0]['event'];assert event.source_module==('notifications' if source=='notifications' else 'printers')
        assert event.data['printer_id']==551 and event.data['print_job_id']==553 and event.data['scheduled_job_id']==552
        if source=='notifications':assert event.data['job_name']=='Owned terminal' and 'duration_seconds' in event.data
        assert records[0]['claim_count']==0
    else:assert records==[]


@pytest.mark.parametrize('source',['mqtt','moonraker','notifications'])
def test_actual_education_producers_deliver_committed_without_generic_fanout(education_terminal_environment,monkeypatch,source):
    from core import events
    engine,bus,_,_,identifier,mqtt,moon,job_events=education_terminal_environment
    records=[];bus.subscribe(events.JOB_COMPLETED,_collect(engine,records))
    for _ in range(2):
        if source=='mqtt':mqtt.record_job_ended(9,'Owned education',identifier,13,'completed',{}, {},lambda **kw:pytest.fail('generic fanout'))
        elif source=='moonraker':
            monitor=moon.MoonrakerMonitor(9,'Owned education','127.0.0.1');monitor._current_job_db_id=identifier;monitor._last_filename='owned.gcode'
            monitor._job_ended('completed',SimpleNamespace(filename='owned.gcode',filament_used_mm=0,error_message=None),education_terminal_status='completed')
        else:job_events.job_completed(9,identifier,success=True)
    assert len(records)==1;_assert_records(records)
    assert records[0]['event'].data['scheduled_job_id']==13
