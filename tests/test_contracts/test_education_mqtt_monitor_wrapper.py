"""Actual monitor start/terminal wrappers; no transport or printer motion."""
from contextlib import contextmanager
import pytest
from sqlalchemy import text
from core.db_utils import _ConnectionAdapter
from tests.test_contracts.test_education_dispatch_policy import actual_education_dispatch
from tests.test_contracts.test_education_review_workflow import review_db


@pytest.mark.parametrize('earlier_orphan', [False, True])
@pytest.mark.parametrize('new_state,status', [('IDLE', 'cancelled'), ('FINISH', 'completed'), ('FAILED', 'failed')])
def test_monitor_wrapper_links_and_ends_exact_education_attempt(actual_education_dispatch, monkeypatch, new_state, status, earlier_orphan):
    from modules.printers import dispatch
    from modules.printers.monitors import mqtt_printer, mqtt_job_lifecycle
    monitor_db, uploaded = actual_education_dispatch
    names=[]
    def accepted_sender(job_id,path,name,creds,**options):
        names.append(name)
        return True, 'Owned physical sender stub'
    monkeypatch.setattr(dispatch, '_dispatch_bambu', accepted_sender)
    assert dispatch.dispatch_job(9, uploaded['job_id'])[0]
    assert len(names)==1
    monitor_db.rollback()
    assert monitor_db.execute(text('SELECT observed_print_job_id FROM dispatch_attempt_receipts WHERE job_id=:id'), {'id':uploaded['job_id']}).scalar_one() is None
    monitor_db.rollback()
    if earlier_orphan:
        monitor_db.execute(text("INSERT INTO print_jobs(id,printer_id,job_name,filename,status,started_at) VALUES(90,9,'Unrelated earlier print','earlier.3mf','running',CURRENT_TIMESTAMP)"))
        monitor_db.commit()
    engine = monitor_db.get_bind()
    @contextmanager
    def owned_db():
        raw = engine.raw_connection()
        try: yield _ConnectionAdapter(raw, False)
        finally: raw.close()
    monkeypatch.setattr(mqtt_printer, 'get_db', owned_db)
    monkeypatch.setattr(mqtt_job_lifecycle, 'get_db', owned_db)
    monitor = mqtt_printer.PrinterMonitor(9, 'Owned A1', '192.0.2.1', 'SYNTHETIC', '')
    monitor._state = {'subtask_name': names[0].removesuffix('.3mf'),
                      'gcode_file': 'Metadata/plate_1.gcode', 'job_id': 'owned-attempt',
                      'gcode_state': 'RUNNING', 'total_layer_num': 10}
    monitor._dispatch_alert = lambda **kwargs: pytest.fail('Education wrapper must not fan out generic alerts')
    monitor._trigger_reschedule = lambda: pytest.fail('Education wrapper must not run generic scheduling')
    monitor._on_state_change('IDLE', 'RUNNING')
    assert monitor._current_job_id is not None and monitor._linked_job_id == uploaded['job_id']
    print_job = monitor._current_job_id
    with engine.connect() as observer:
        assert observer.execute(text('SELECT observed_print_job_id FROM dispatch_attempt_receipts WHERE job_id=:id'), {'id':uploaded['job_id']}).scalar_one()==print_job
        birth=observer.execute(text('SELECT economic_birth_nonce,economic_uid,spool_snapshot FROM print_jobs WHERE id=:id'), {'id':print_job}).one()
        assert birth[0] and birth[1] is None and birth[2]
    assert print_job != 90
    monitor._state['gcode_state'] = new_state
    monitor._on_state_change('RUNNING', new_state)
    assert monitor._current_job_id is None
    # A duplicate terminal callback has no authority to change the saved result.
    monitor._on_state_change('RUNNING', new_state)
    monitor_db.expire_all()
    assert tuple(monitor_db.execute(text('SELECT status,lifecycle_revision FROM education_submissions WHERE id=:id'), {'id':uploaded['id']}).one()) == (status, 5)
    assert monitor_db.execute(text('SELECT status FROM jobs WHERE id=:id'), {'id':uploaded['job_id']}).scalar_one() == status
    if earlier_orphan:
        assert tuple(monitor_db.execute(text('SELECT status,scheduled_job_id FROM print_jobs WHERE id=90')).one()) == ('running', None)
    assert monitor_db.execute(text('SELECT status FROM print_jobs WHERE id=:id'), {'id': print_job}).scalar_one() == status



from tests.test_contracts.test_printer_offline_behavior import offline_store, v2_offline_monitor
from types import SimpleNamespace


_MISSING_DIAMETER=object()


@pytest.mark.parametrize('diameter',[_MISSING_DIAMETER,None,True,False,'invalid',float('nan'),float('inf'),-0.4,0,10**1000],ids=['missing','null','true','false','invalid','nan','infinity','negative','zero','overflow'])
def test_invalid_partial_nozzle_diameter_preserves_configuration(v2_offline_monitor,offline_store,monkeypatch,diameter):
    monitor,_,module=v2_offline_monitor
    offline_store.execute(text('UPDATE printers SET nozzle_diameter=.4 WHERE id=199'));offline_store.commit()
    original=module.get_db;writes=[]
    class ObservedConnection:
        def __init__(self,connection):self.connection=connection
        def __getattr__(self,name):return getattr(self.connection,name)
        def execute(self,statement,*args,**kwargs):
            if str(statement).lstrip().upper().startswith('UPDATE') and 'nozzle_diameter' in str(statement):writes.append(statement)
            return self.connection.execute(statement,*args,**kwargs)
    @contextmanager
    def database():
        with original() as connection:yield ObservedConnection(connection)
    monkeypatch.setattr(module,'get_db',database)
    packet={'gcode_state':'IDLE'}
    if diameter is not _MISSING_DIAMETER:packet['nozzle_diameter']=diameter
    monitor._on_status(SimpleNamespace(raw_data={'print':packet}))
    assert offline_store.execute(text('SELECT nozzle_diameter FROM printers WHERE id=199')).scalar_one()==.4
    assert writes==[], 'Invalid observation must not issue a nozzle configuration UPDATE'


def test_present_valid_nozzle_and_later_missing_packet_do_not_replay_cached_value(v2_offline_monitor,offline_store,monkeypatch):
    monitor,_,module=v2_offline_monitor
    offline_store.execute(text('UPDATE printers SET nozzle_diameter=.4 WHERE id=199'));offline_store.commit()
    monitor._on_status(SimpleNamespace(raw_data={'print':{'gcode_state':'IDLE','nozzle_diameter':'0.6'}}))
    assert offline_store.execute(text('SELECT nozzle_diameter FROM printers WHERE id=199')).scalar_one()==.6
    offline_store.execute(text('UPDATE printers SET nozzle_diameter=.8 WHERE id=199'));offline_store.commit()
    monkeypatch.setattr(module,'time',SimpleNamespace(time=lambda:2011))
    monitor._on_status(SimpleNamespace(raw_data={'print':{'gcode_state':'IDLE'}}))
    assert offline_store.execute(text('SELECT nozzle_diameter FROM printers WHERE id=199')).scalar_one()==.8
    monkeypatch.setattr(module,'time',SimpleNamespace(time=lambda:2022))
    monitor._on_status(SimpleNamespace(raw_data={'print':{'gcode_state':'IDLE','nozzle_diameter':.4}}))
    assert offline_store.execute(text('SELECT nozzle_diameter FROM printers WHERE id=199')).scalar_one()==.4
