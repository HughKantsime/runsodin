"""Parent configuration guards and actual atomic cache writes; no dispatch."""
import time
from types import SimpleNamespace
import pytest
from sqlalchemy import text
from tests.test_contracts.test_education_review_workflow import review_db


@pytest.fixture
def parent(review_db,monkeypatch):
    from modules.printers import smart_plug as module
    from modules.printers import plug_power_acquisition as acquisition
    review_db.execute(text("INSERT INTO printers(id,name,plug_type,plug_host,plug_auto_on,plug_power_state) VALUES(599,'Synthetic bounded','tasmota','127.0.0.1:1',1,0)"));review_db.commit()
    monkeypatch.setattr(module,'engine',review_db.get_bind())
    calls=[]
    monkeypatch.setattr(module,'_cooldown_timers',{599:SimpleNamespace(cancel=lambda:calls.append('cancel'))})
    def worker(config,deadline,connection):
        calls.append('worker')
        return acquisition.PowerAttempt('accepted',True)
    monkeypatch.setattr(acquisition,'bounded_power_on',worker)
    return module,acquisition,review_db,calls


def test_actual_observation_persisted_and_pending_off_cancelled(parent):
    module,acquisition,db,calls=parent
    result=module.prepare_bounded_dispatch_power(599,time.monotonic()+3)
    assert result.accepted and result.required
    assert calls==['cancel','worker'] and 599 not in module._cooldown_timers
    assert db.execute(text('SELECT plug_power_state FROM printers WHERE id=599')).scalar_one()==1


@pytest.mark.parametrize('column,value',[('plug_host','changed.invalid'),('plug_auto_on',0),('plug_auth_token','synthetic-token')])
def test_committed_configuration_change_during_worker_refuses_without_cache_write(parent,monkeypatch,column,value):
    module,acquisition,db,calls=parent
    def worker(*args):
        with db.get_bind().begin() as conn:conn.execute(text(f'UPDATE printers SET {column}=:value WHERE id=599'),{'value':value})
        return acquisition.PowerAttempt('accepted',True)
    monkeypatch.setattr(acquisition,'bounded_power_on',worker)
    assert not module.prepare_bounded_dispatch_power(599,time.monotonic()+3).accepted
    assert db.execute(text('SELECT plug_power_state FROM printers WHERE id=599')).scalar_one()==0


@pytest.mark.parametrize('column,value',[('plug_host','changed.invalid'),('plug_auto_on',0),('plug_auth_token','synthetic-token')])
def test_conditional_sql_refuses_change_after_last_read(parent,column,value):
    module,acquisition,db,calls=parent
    config=module.get_plug_config(599,include_storage_identity=True)
    with db.get_bind().begin() as conn:conn.execute(text(f'UPDATE printers SET {column}=:value WHERE id=599'),{'value':value})
    assert module._conditional_power_observation(599,True,config) is False
    assert db.execute(text('SELECT plug_power_state FROM printers WHERE id=599')).scalar_one()==0


@pytest.mark.parametrize('status,state',[('uncertain',None),('refused',False),('accepted',None),('accepted',1)])
def test_bad_or_unknown_http_receipt_never_updates_cache(parent,monkeypatch,status,state):
    module,acquisition,db,calls=parent
    monkeypatch.setattr(acquisition,'bounded_power_on',lambda *args:acquisition.PowerAttempt(status,state))
    assert not module.prepare_bounded_dispatch_power(599,time.monotonic()+3).accepted
    assert db.execute(text('SELECT plug_power_state FROM printers WHERE id=599')).scalar_one()==0


@pytest.mark.parametrize('changed',[False,True])
def test_mqtt_snapshot_rechecked_without_fabricating_power(parent,monkeypatch,changed):
    from core.mqtt_connection_snapshot import MqttConnectionSnapshot
    from modules.notifications import mqtt_republish
    module,acquisition,db,calls=parent
    db.execute(text("UPDATE printers SET plug_type='mqtt',plug_entity_id='synthetic/command' WHERE id=599"));db.commit()
    first=MqttConnectionSnapshot('localhost',1883,'synthetic','synthetic',False)
    second=MqttConnectionSnapshot('localhost',8883,'synthetic','synthetic',True)
    snapshots=iter([first,second if changed else first])
    monkeypatch.setattr(mqtt_republish,'get_power_connection_snapshot',lambda:next(snapshots))
    monkeypatch.setattr(acquisition,'bounded_power_on',lambda *args:acquisition.PowerAttempt('accepted',None))
    result=module.prepare_bounded_dispatch_power(599,time.monotonic()+3)
    assert result.accepted is (not changed)
    assert db.execute(text('SELECT plug_power_state FROM printers WHERE id=599')).scalar_one()==0
    assert 'synthetic' not in repr(result)


def test_disabled_plug_cancels_off_without_worker(parent,monkeypatch):
    module,acquisition,db,calls=parent
    db.execute(text('UPDATE printers SET plug_auto_on=0 WHERE id=599'));db.commit()
    monkeypatch.setattr(acquisition,'bounded_power_on',lambda *args:pytest.fail('Disabled must not send'))
    result=module.prepare_bounded_dispatch_power(599,time.monotonic()+3)
    assert result.accepted and not result.required and calls==['cancel']


def test_pending_off_lock_held_through_worker(parent,monkeypatch):
    import threading
    module,acquisition,db,calls=parent
    outcomes=[]
    def worker(*args):
        def competing():
            lock=module._printer_power_lock(599)
            obtained=lock.acquire(timeout=.1);outcomes.append(obtained)
            if obtained:lock.release()
        thread=threading.Thread(target=competing);thread.start();thread.join(timeout=1)
        assert not thread.is_alive()
        return acquisition.PowerAttempt('accepted',True)
    monkeypatch.setattr(acquisition,'bounded_power_on',worker)
    assert module.prepare_bounded_dispatch_power(599,time.monotonic()+3).accepted
    assert outcomes==[False]
