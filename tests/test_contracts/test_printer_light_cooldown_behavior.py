"""Actual light command HTTP receipts and monitor reconciliation, owned persistence."""
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
import pytest
from sqlalchemy import text
from tests.test_contracts.test_printer_offline_behavior import offline_store, review_db, v2_offline_monitor
from tests.test_contracts.test_api_token_delegation_behavior import token_client


@pytest.fixture
def light_client(token_client,offline_store,monkeypatch):
    from modules.printers import routes_controls
    from modules.printers import monitor_commands
    from modules.printers.telemetry import feature_flag
    from modules.printers.telemetry.bambu import session, broker_policy
    client,issue=token_client;client.app.include_router(routes_controls.router)
    offline_store.execute(text("UPDATE printers SET api_host='127.0.0.1',api_key='synthetic-placeholder',lights_on=0 WHERE id=199"));offline_store.commit()
    calls=[]
    monkeypatch.setattr(routes_controls.crypto,'decrypt',lambda value:'|'.join(['synthetic-serial','synthetic-access']))
    monkeypatch.setattr(feature_flag,'is_v2_enabled',lambda:True)
    monkeypatch.setattr(broker_policy,'resolve_bambu_broker_config',lambda *a,**kw:SimpleNamespace(host='127.0.0.1',port=1883,use_tls=False))
    monkeypatch.setattr(monitor_commands,'request_command',lambda pid,action,**kw:(calls.append((action,(kw['on'],))) or 'accepted'))
    return client,issue,calls


def test_light_http_success_matches_boolean_receipt_and_persists_toggle(light_client,offline_store):
    client,issue,calls=light_client
    response=client.post('/printers/199/lights',headers=issue(['write:printers']))
    assert response.status_code==200
    assert response.json()['lights_on'] is True
    assert calls==[('light',(True,))]
    offline_store.expire_all()
    row=offline_store.execute(text('SELECT lights_on,lights_toggled_at FROM printers WHERE id=199')).one()
    assert row[0]==1 and row[1]


@pytest.mark.parametrize('foreign',[False,True])
def test_light_failures_leave_prior_state_and_timestamp_unchanged(light_client,offline_store,monkeypatch,foreign):
    from modules.printers import monitor_commands
    client,issue,calls=light_client
    if foreign:
        offline_store.execute(text('UPDATE printers SET org_id=2,shared=1 WHERE id=199'));offline_store.commit()
    else:
        monkeypatch.setattr(monitor_commands,'request_command',lambda pid,action,**kw:(calls.append((action,(kw['on'],))) or 'unknown'))
    response=client.post('/printers/199/lights',headers=issue(['write:printers']))
    assert response.status_code==(404 if foreign else 503)
    assert calls==([] if foreign else [('light',(True,))])
    assert tuple(offline_store.execute(text('SELECT lights_on,lights_toggled_at FROM printers WHERE id=199')).one())==(0,None)


@pytest.mark.parametrize('aware',[False,True,'offset'])
@pytest.mark.parametrize('elapsed',[0,21])
def test_recent_light_toggle_ignores_old_telemetry_then_reconciles(v2_offline_monitor,offline_store,aware,elapsed):
    monitor,_,_=v2_offline_monitor
    observed=datetime.now(timezone.utc)-timedelta(seconds=elapsed)
    if not aware:observed=observed.replace(tzinfo=None)
    elif aware=='offset':observed=observed.astimezone(timezone(timedelta(hours=-4)))
    offline_store.execute(text('UPDATE printers SET lights_on=1,lights_toggled_at=:stamp WHERE id=199'),{'stamp':observed.isoformat()});offline_store.commit()
    monitor._on_status(SimpleNamespace(raw_data={'print':{'gcode_state':'IDLE','lights_report':[{'node':'chamber_light','mode':'off'}]}}))
    assert offline_store.execute(text('SELECT lights_on FROM printers WHERE id=199')).scalar_one()==(1 if elapsed==0 else 0)


@pytest.mark.parametrize('timestamp',['synthetic-private-marker',(datetime.now(timezone.utc)+timedelta(days=1)).isoformat()])
def test_invalid_or_future_toggle_timestamp_does_not_hold_old_light_state(v2_offline_monitor,offline_store,timestamp,caplog):
    caplog.set_level('DEBUG',logger='modules.printers.monitors.mqtt_printer')
    monitor,_,_=v2_offline_monitor
    offline_store.execute(text('UPDATE printers SET lights_on=1,lights_toggled_at=:stamp WHERE id=199'),{'stamp':timestamp});offline_store.commit()
    monitor._on_status(SimpleNamespace(raw_data={'print':{'gcode_state':'IDLE','lights_report':[{'node':'chamber_light','mode':'off'}]}}))
    assert offline_store.execute(text('SELECT lights_on FROM printers WHERE id=199')).scalar_one()==0
    assert 'synthetic-private-marker' not in caplog.text


@pytest.mark.parametrize('old_timestamp',[None,'2026-01-01 00:00:00'])
def test_concurrent_toggle_after_cooldown_read_is_not_overwritten(v2_offline_monitor,offline_store,monkeypatch,old_timestamp):
    from contextlib import contextmanager
    from sqlalchemy.orm import Session
    monitor,_,module=v2_offline_monitor
    offline_store.execute(text('UPDATE printers SET lights_on=0,lights_toggled_at=:stamp WHERE id=199'),{'stamp':old_timestamp});offline_store.commit()
    original=module.get_db;commits=[]
    class Cursor:
        def __init__(self,cursor):self.cursor=cursor
        def fetchone(self):
            row=self.cursor.fetchone()
            with Session(offline_store.get_bind()) as writer:
                writer.execute(text('UPDATE printers SET lights_on=1,lights_toggled_at=:stamp WHERE id=199'),{'stamp':datetime.now(timezone.utc).isoformat()})
                writer.commit()
            commits.append(True)
            return row
    class Connection:
        def __init__(self,conn):self.conn=conn
        def __getattr__(self,name):return getattr(self.conn,name)
        def execute(self,statement,*args):
            cursor=self.conn.execute(statement,*args)
            return Cursor(cursor) if statement.startswith('SELECT lights_toggled_at') else cursor
    @contextmanager
    def database():
        with original() as conn:yield Connection(conn)
    monkeypatch.setattr(module,'get_db',database)
    monitor._on_status(SimpleNamespace(raw_data={'print':{'gcode_state':'IDLE','lights_report':[{'node':'chamber_light','mode':'off'}]}}))
    assert commits==[True]
    assert offline_store.execute(text('SELECT lights_on FROM printers WHERE id=199')).scalar_one()==1
    assert offline_store.execute(text('SELECT gcode_state FROM printers WHERE id=199')).scalar_one()=='IDLE'


def test_unchanged_null_timestamp_allows_observed_light_state(v2_offline_monitor,offline_store):
    monitor,_,_=v2_offline_monitor
    offline_store.execute(text('UPDATE printers SET lights_on=1,lights_toggled_at=NULL WHERE id=199'));offline_store.commit()
    monitor._on_status(SimpleNamespace(raw_data={'print':{'gcode_state':'IDLE','lights_report':[{'node':'chamber_light','mode':'off'}]}}))
    assert offline_store.execute(text('SELECT lights_on FROM printers WHERE id=199')).scalar_one()==0
