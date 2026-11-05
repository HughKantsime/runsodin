"""Real monitor and organization writes must retain atomic identity history."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from core.recipe_configuration import unchanged_since
from tests.test_contracts.test_printer_offline_behavior import offline_store, review_db, v2_offline_monitor


def audits(db, pid=199):
    return db.execute(text("SELECT action,details FROM audit_logs WHERE entity_type='printer' AND entity_id=:pid ORDER BY id"), {'pid':pid}).all()


def reject_audit(db):
    db.execute(text("""CREATE TRIGGER reject_identity_audit BEFORE INSERT ON audit_logs
        WHEN NEW.entity_type='printer' BEGIN SELECT RAISE(ABORT,'synthetic audit failure'); END"""))
    db.commit()


@pytest.mark.parametrize('field,old,value,packet', [
    ('nozzle_diameter',.4,.6,{'nozzle_diameter':'0.6'}),
    ('model',None,'X1C',{'printer_type':'X1C'}),
    ('model','Unknown','X1C',{'printer_type':'X1C'}),
    ('machine_type','P1S','X1C',{'printer_type':'X1C'}),
])
def test_mqtt_identity_change_is_audited_once(v2_offline_monitor,offline_store,field,old,value,packet):
    monitor,_,_=v2_offline_monitor
    offline_store.execute(text(f'UPDATE printers SET {field}=:old WHERE id=199'), {'old':old})
    offline_store.commit()
    for _ in range(2):
        monitor._last_heartbeat=0
        monitor._on_status(SimpleNamespace(raw_data={'print':{'gcode_state':'IDLE',**packet}}))
    entries=[(action,json.loads(details)) for action,details in audits(offline_store)]
    matching=[(action,d) for action,d in entries if d['field']==field]
    assert matching==[('printer.observed_config_changed',{'field':field,'old':old,'new':value})]
    assert offline_store.execute(text(f'SELECT {field} FROM printers WHERE id=199')).scalar_one()==value
    assert not unchanged_since(offline_store,199,'2020-01-01')


def test_mqtt_keeps_user_model_and_does_not_audit_unchanged_values(v2_offline_monitor,offline_store):
    monitor,_,_=v2_offline_monitor
    from modules.printers.printer_models import bambu_model_name
    offline_store.execute(text("UPDATE printers SET model='Custom',machine_type=:model,nozzle_diameter=.4 WHERE id=199"), {'model':bambu_model_name('X1C')})
    offline_store.commit()
    monitor._on_status(SimpleNamespace(raw_data={'print':{'gcode_state':'IDLE','printer_type':'X1C','nozzle_diameter':'0.4'}}))
    assert audits(offline_store)==[]
    assert offline_store.execute(text('SELECT model FROM printers WHERE id=199')).scalar_one()=='Custom'


@pytest.mark.parametrize('packet,field', [({'nozzle_diameter':.6},'nozzle_diameter'),
    ({'printer_type':'X1C'},'model'), ({'printer_type':'X1C'},'machine_type')])
def test_mqtt_audit_failure_cannot_commit_identity(v2_offline_monitor,offline_store,packet,field):
    monitor,_,_=v2_offline_monitor
    before=offline_store.execute(text(f'SELECT {field} FROM printers WHERE id=199')).scalar_one()
    reject_audit(offline_store)
    monitor._on_status(SimpleNamespace(raw_data={'print':{'gcode_state':'IDLE',**packet}}))
    assert offline_store.execute(text(f'SELECT {field} FROM printers WHERE id=199')).scalar_one()==before
    assert audits(offline_store)==[]


@pytest.mark.parametrize('intervening', [
    {}, *({'nozzle_diameter': value} for value in
          (None, True, False, 'invalid', '', [], {}, float('nan'),
           float('inf'), float('-inf'), 'NaN', 'inf', 0, -0.6)),
])
def test_mqtt_pending_diameter_survives_partial_and_invalid_packets(
        v2_offline_monitor, offline_store, monkeypatch, intervening):
    monitor, _, module = v2_offline_monitor
    offline_store.execute(text('UPDATE printers SET nozzle_diameter=.4 WHERE id=199'))
    offline_store.commit()
    clock = [2000]
    monkeypatch.setattr(module, 'time', SimpleNamespace(time=lambda: clock[0]))
    monitor._last_heartbeat = clock[0]

    def packet(values):
        monitor._on_status(SimpleNamespace(raw_data={'print': {'gcode_state': 'IDLE', **values}}))

    # Both values arrive inside the heartbeat window; the latest valid one wins.
    packet({'nozzle_diameter': .8})
    packet({'nozzle_diameter': '0.6'})
    packet({})
    packet(intervening)
    assert offline_store.execute(text('SELECT nozzle_diameter FROM printers WHERE id=199')).scalar_one() == .4
    assert audits(offline_store) == []

    clock[0] += 10
    packet({})
    assert offline_store.execute(text('SELECT nozzle_diameter FROM printers WHERE id=199')).scalar_one() == .6
    expected = [('printer.observed_config_changed', {'field': 'nozzle_diameter', 'old': .4, 'new': .6})]
    assert [(action, json.loads(details)) for action, details in audits(offline_store)] == expected
    clock[0] += 10
    packet({})
    assert [(action, json.loads(details)) for action, details in audits(offline_store)] == expected

    # A committed observation must not be replayed over a later edit.
    offline_store.execute(text('UPDATE printers SET nozzle_diameter=.8 WHERE id=199'))
    offline_store.commit()
    clock[0] += 10
    packet({})
    assert offline_store.execute(text('SELECT nozzle_diameter FROM printers WHERE id=199')).scalar_one() == .8
    assert [(action, json.loads(details)) for action, details in audits(offline_store)] == expected


@pytest.mark.parametrize('failure', ['audit', 'commit'])
def test_mqtt_pending_diameter_retries_failed_write(
        v2_offline_monitor, offline_store, monkeypatch, failure):
    from contextlib import contextmanager
    from core import monitor_observations

    monitor, _, module = v2_offline_monitor
    offline_store.execute(text('UPDATE printers SET nozzle_diameter=.4 WHERE id=199'))
    offline_store.commit()
    snapshot = text('SELECT nozzle_diameter,last_seen,gcode_state,installed_nozzles FROM printers WHERE id=199')
    before = tuple(offline_store.execute(snapshot).one())
    clock = [2000]
    monkeypatch.setattr(module, 'time', SimpleNamespace(time=lambda: clock[0]))
    monkeypatch.setattr(monitor_observations, 'time', SimpleNamespace(time=lambda: clock[0]))
    monitor._last_heartbeat = clock[0]
    monitor._on_status(SimpleNamespace(raw_data={'print': {'gcode_state': 'IDLE', 'nozzle_diameter': .6}}))
    database = module.get_db
    commit_attempts = []

    class FailedCommit:
        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def commit(self):
            commit_attempts.append(True)
            raise RuntimeError('synthetic commit failure')

    @contextmanager
    def failing_database():
        with database() as connection:
            yield FailedCommit(connection)

    if failure == 'audit':
        reject_audit(offline_store)
    else:
        monkeypatch.setattr(module, 'get_db', failing_database)
    clock[0] += 10
    monitor._on_status(SimpleNamespace(raw_data={'print': {'gcode_state': 'IDLE'}}))
    assert tuple(offline_store.execute(snapshot).one()) == before
    assert audits(offline_store) == []
    assert monitor._last_heartbeat == 2000
    if failure == 'audit':
        offline_store.execute(text('DROP TRIGGER reject_identity_audit'))
        offline_store.commit()
    else:
        # The unchanged live-state evidence is inside its 15-second interval;
        # only the identity/heartbeat transaction attempts a commit at +10s.
        assert commit_attempts == [True]
        monkeypatch.setattr(module, 'get_db', database)

    for _ in range(2):
        clock[0] += 10
        monitor._on_status(SimpleNamespace(raw_data={'print': {'gcode_state': 'IDLE'}}))
    assert offline_store.execute(text('SELECT nozzle_diameter FROM printers WHERE id=199')).scalar_one() == .6
    assert [(action, json.loads(details)) for action, details in audits(offline_store)] == [
        ('printer.observed_config_changed', {'field': 'nozzle_diameter', 'old': .4, 'new': .6})]


@pytest.mark.parametrize('fail',[False,True])
def test_anycubic_reconnect_model_change_is_atomic_and_not_repeated(offline_store,monkeypatch,fail):
    from modules.printers.monitors import anycubic_lan_monitor as module
    monkeypatch.setattr(module,'engine',offline_store.get_bind())
    offline_store.execute(text("UPDATE printers SET model='Old model' WHERE id=199"));offline_store.commit()
    if fail:reject_audit(offline_store)
    for _ in range(2):
        monitor=module.AnycubicLANMonitorThread(199,'Synthetic','192.0.2.1')
        monitor.client=SimpleNamespace(_connected=False,model_id='20025',connect=lambda:True,
            poll=lambda:True,disconnect=lambda:None,last_info=10**12)
        monkeypatch.setattr(monitor,'_discover_and_save_camera',lambda:None)
        monkeypatch.setattr(monitor._wake,'wait',lambda _:setattr(monitor,'_running',False))
        monitor.run()
    assert offline_store.execute(text('SELECT model FROM printers WHERE id=199')).scalar_one()==('Old model' if fail else 'Kobra S1')
    rows=audits(offline_store)
    assert len(rows)==(0 if fail else 1)
    if not fail:
        assert json.loads(rows[0][1])=={'field':'model','old':'Old model','new':'Kobra S1'}
        assert not unchanged_since(offline_store,199,'2020-01-01')


@pytest.mark.parametrize('fail',[False,True])
def test_org_assignment_audits_only_change_in_same_transaction(offline_store,fail):
    from modules.organizations.routes import assign_printer_to_org, AssignPrinterRequest
    if fail:reject_audit(offline_store)
    async def assign():
        return await assign_printer_to_org(2,AssignPrinterRequest(printer_id=199),
            current_user={'role':'admin','group_id':None},db=offline_store)
    if fail:
        with pytest.raises(Exception,match='synthetic audit failure'):asyncio.run(assign())
        offline_store.rollback()
    else:
        asyncio.run(assign());asyncio.run(assign())
    assert offline_store.execute(text('SELECT org_id FROM printers WHERE id=199')).scalar_one()==(1 if fail else 2)
    rows=audits(offline_store)
    assert len(rows)==(0 if fail else 1)
    if not fail:
        assert rows[0][0]=='printer.org_assigned'
        assert json.loads(rows[0][1])=={'field':'org_id','old':1,'new':2}
        assert not unchanged_since(offline_store,199,'2020-01-01')


@pytest.mark.parametrize('dbapi',[False,True])
def test_observation_remains_in_caller_transaction(offline_store,dbapi):
    from core.db_utils import _ConnectionAdapter
    from core.printer_identity_audit import observe_printer_identity
    engine=offline_store.get_bind()
    offline_store.execute(text('UPDATE printers SET nozzle_diameter=.4 WHERE id=199'));offline_store.commit()
    if dbapi:
        raw=engine.raw_connection()
        connection=_ConnectionAdapter(raw,False)
    else:
        connection=engine.connect()
    try:
        assert observe_printer_identity(connection,199,'nozzle_diameter',.6)
        connection.rollback()
    finally:
        if dbapi:raw.close()
        else:connection.close()
    assert offline_store.execute(text('SELECT nozzle_diameter FROM printers WHERE id=199')).scalar_one()==.4
    assert audits(offline_store)==[]
