"""Queue entry acceptance through actual generic HTTP workflows."""
import json
import pytest
from sqlalchemy import text
from core.base import AlertType
from modules.notifications.models import AlertPreference
from tests.test_contracts.test_job_approval_notification_behavior import approval_client, review_db, signed_license
from tests.test_contracts.test_mqtt_protocol_peer_behavior import mqtt_peer


def configure_creator(review_db, user, enabled=True):
    user.update(id=3, role='operator', username='Synthetic operator')
    for uid in [3,5]:
        review_db.add(AlertPreference(user_id=uid, alert_type=AlertType.QUEUE_ADDED,
            in_app=enabled, email=False, browser_push=False))
    review_db.execute(text("INSERT INTO jobs(id,item_name,status,charged_to_user_id,charged_to_org_id) VALUES(881,'Synthetic repeatable','completed',3,1)"))
    review_db.commit()


def enter(client, path):
    if path == 'bulk':
        response = client.post('/api/v1/jobs/bulk', json=[{'item_name':'Synthetic bulk one'}, {'item_name':'Synthetic bulk held','hold':True}])
        ids = [row['id'] for row in response.json()] if response.status_code == 201 else []
    elif path == 'batch':
        response = client.post('/api/v1/jobs/batch', json={'item_name':'Synthetic batch','printer_ids':[9,10],'queue_only':True})
        ids = [row['id'] for row in response.json()] if response.status_code == 200 else []
    elif path == 'repeat':
        response = client.post('/api/v1/jobs/881/repeat')
        ids = [response.json()['new_job_id']] if response.status_code == 200 else []
    else:
        response = client.post('/api/v1/jobs', json={'item_name':'Synthetic queue wire'})
        ids = [response.json()['id']] if response.status_code == 201 else []
    assert response.status_code in (200,201), response.text
    return ids


@pytest.mark.parametrize('path',['bulk','batch','repeat'])
@pytest.mark.parametrize('enabled',[False,True])
def test_each_generic_queue_entry_honors_creator_preferences(approval_client, review_db, monkeypatch, path, enabled):
    client,user = approval_client
    configure_creator(review_db,user,enabled)
    from core import event_bus
    from core.event_bus import InMemoryEventBus
    events=[];bus=InMemoryEventBus();bus.subscribe("notifications.alert_dispatched",lambda event:events.append(event.data))
    monkeypatch.setattr(event_bus,"_bus",bus)
    ids = enter(client,path)
    assert ids
    actual = set(review_db.execute(text("SELECT user_id,job_id FROM alerts WHERE alert_type='queue_added'")).all())
    assert actual == ({(3,jid) for jid in ids} if enabled else set())
    assert {e["job_id"] for e in events} == (set(ids) if enabled else set())
    assert all(e["_ws_user_ids"]==[3] for e in events)


def test_actual_pending_http_alert_reaches_owned_mqtt_protocol_peer(approval_client, review_db, mqtt_peer, monkeypatch):
    from core import event_bus
    from core.event_bus import InMemoryEventBus
    from modules.notifications import mqtt_republish
    client,user = approval_client
    configure_creator(review_db,user)
    port, packets, disconnected, published, _ = mqtt_peer
    values={'mqtt_republish_enabled':'true','mqtt_republish_host':'127.0.0.1',
        'mqtt_republish_port':str(port),'mqtt_republish_topic_prefix':'synthetic/queue'}
    for key,value in values.items():
        review_db.execute(text('INSERT INTO system_config(key,value) VALUES(:key,:value)'),{'key':key,'value':value})
    review_db.commit()
    monkeypatch.setattr(mqtt_republish,'engine',review_db.get_bind())
    monkeypatch.setattr(mqtt_republish,'_config_cache',None)
    monkeypatch.setattr(mqtt_republish,'_config_ts',0)
    monkeypatch.setattr(mqtt_republish,'_client',None)
    bus = InMemoryEventBus()
    monkeypatch.setattr(event_bus,'_bus',bus)
    mqtt_republish.register_subscribers(bus)
    observed=[]
    bus.subscribe('notifications.alert_dispatched', lambda event: observed.append(event.data))
    try:
        ids = enter(client,'single')
        assert published.wait(2), 'No MQTT publication from actual queue origin'
        frames = [(header,body) for header,body in packets if header>>4 == 3]
        assert len(frames)==1
        header,body = frames[0]
        length=int.from_bytes(body[:2],'big')
        assert body[2:2+length].decode()=='synthetic/queue/alerts'
        payload=json.loads(body[2+length:])
        assert payload['type']=='queue_added'
        assert payload['title']=='Job added to queue: Synthetic queue wire'
        assert observed[0]['job_id']==ids[0]
        assert observed[0]['_ws_user_ids']==[3]
    finally:
        mqtt_republish.disconnect()


@pytest.mark.parametrize('path',['bulk','batch','repeat'])
@pytest.mark.parametrize('failure',['stage','commit','uncertain_commit'])
def test_other_queue_entries_rollback_atomically_without_event_replay(approval_client, review_db, monkeypatch, path, failure):
    from sqlalchemy.orm import Session
    from modules.notifications import alert_dispatcher
    from core import event_bus
    from core.event_bus import InMemoryEventBus
    client,user=approval_client
    configure_creator(review_db,user)
    before=review_db.execute(text('SELECT COUNT(*) FROM jobs')).scalar_one()
    events=[];bus=InMemoryEventBus();bus.subscribe('notifications.alert_dispatched',lambda event:events.append(event.data))
    monkeypatch.setattr(event_bus,'_bus',bus)
    if failure=='stage':
        original=alert_dispatcher.stage_alert
        calls=[]
        def stage(*args,**kwargs):
            plan=original(*args,**kwargs);calls.append(True)
            if len(calls)==(1 if path=='repeat' else 2):
                raise RuntimeError('Synthetic staging failure')
            return plan
        monkeypatch.setattr(alert_dispatcher,'stage_alert',stage)
    else:
        original=Session.commit
        def commit(db):
            if failure=='uncertain_commit':original(db)
            raise RuntimeError('Synthetic commit failure')
        monkeypatch.setattr(Session,'commit',commit)
    with pytest.raises(RuntimeError,match='Synthetic'):
        enter(client,path)
    assert events==[]
    review_db.rollback()
    count=1 if path=='repeat' else 2
    if failure=='uncertain_commit':
        assert review_db.execute(text('SELECT COUNT(*) FROM jobs')).scalar_one()==before+count
        assert review_db.execute(text("SELECT COUNT(*) FROM alerts WHERE alert_type='queue_added'")).scalar_one()==count
    else:
        assert review_db.execute(text('SELECT COUNT(*) FROM jobs')).scalar_one()==before
        assert review_db.execute(text('SELECT COUNT(*) FROM alerts')).scalar_one()==0


@pytest.mark.parametrize('quiet',[False,True])
def test_configured_quiet_hours_keep_http_in_app_event_but_suppress_mqtt(approval_client,review_db,monkeypatch,quiet):
    from datetime import datetime,timezone
    from types import SimpleNamespace
    from core import event_bus
    from core.event_bus import InMemoryEventBus
    from modules.notifications import mqtt_republish,quiet_hours
    client,user=approval_client;configure_creator(review_db,user)
    for key,value in {'quiet_hours_enabled':'true','quiet_hours_start':'22:00','quiet_hours_end':'07:00',
            'mqtt_republish_enabled':'true','mqtt_republish_host':'synthetic.invalid','mqtt_republish_topic_prefix':'synthetic'}.items():
        review_db.execute(text('INSERT INTO system_config(key,value) VALUES(:key,:value)'),{'key':key,'value':value})
    review_db.commit()
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None):return datetime(2026,10,11,23 if quiet else 12,0,tzinfo=timezone.utc)
    monkeypatch.setattr(quiet_hours,'datetime',Clock)
    monkeypatch.setattr(quiet_hours,'engine',review_db.get_bind())
    monkeypatch.setattr(quiet_hours,'_config_cache',None)
    monkeypatch.setattr(mqtt_republish,'engine',review_db.get_bind())
    monkeypatch.setattr(mqtt_republish,'_config_cache',None)
    calls=[]
    monkeypatch.setattr(mqtt_republish,'_get_client',lambda:SimpleNamespace(publish=lambda *args,**kwargs:calls.append((args,kwargs))))
    bus=InMemoryEventBus();monkeypatch.setattr(event_bus,'_bus',bus)
    mqtt_republish.register_subscribers(bus)
    events=[];bus.subscribe('notifications.alert_dispatched',lambda event:events.append(event.data))
    ids=enter(client,'single')
    assert len(events)==1 and events[0]['_ws_user_ids']==[3]
    assert events[0]['job_id']==ids[0]
    assert review_db.execute(text('SELECT COUNT(*) FROM alerts WHERE job_id=:id'),{'id':ids[0]}).scalar_one()==1
    assert len(calls)==(0 if quiet else 1)
    if calls:
        payload=json.loads(calls[0][0][1])
        assert '_ws_user_ids' not in payload
        assert 'count' not in payload
        assert 'job_id' not in payload


@pytest.mark.parametrize('reference',[{'job_id':99999},{'printer_id':99999},{'job_id':881,'printer_id':99999}])
def test_mqtt_alert_handler_refuses_missing_resources(approval_client,review_db,monkeypatch,reference):
    from core.interfaces.event_bus import Event
    from modules.notifications import mqtt_republish
    client,user=approval_client;configure_creator(review_db,user)
    monkeypatch.setattr(mqtt_republish,'engine',review_db.get_bind())
    calls=[];monkeypatch.setattr(mqtt_republish,'republish_alert',lambda **kwargs:calls.append(kwargs))
    mqtt_republish._on_alert_dispatched(Event('notifications.alert_dispatched','notifications',reference|{'alert_type':'queue_added','_ws_user_ids':[3]}))
    assert calls==[]



def test_mqtt_alert_handler_refuses_conflicting_resource_tenants(approval_client,review_db,monkeypatch):
    from core.interfaces.event_bus import Event
    from modules.notifications import mqtt_republish
    client,user=approval_client;configure_creator(review_db,user)
    review_db.execute(text("INSERT INTO jobs(id,item_name,status,charged_to_org_id,charged_to_user_id) VALUES(882,'Synthetic foreign job','pending',2,5)"));review_db.commit()
    monkeypatch.setattr(mqtt_republish,'engine',review_db.get_bind())
    calls=[];monkeypatch.setattr(mqtt_republish,'republish_alert',lambda **kwargs:calls.append(kwargs))
    mqtt_republish._on_alert_dispatched(Event('notifications.alert_dispatched','notifications',{'printer_id':9,'job_id':882,'alert_type':'queue_added','_ws_user_ids':[3]}))
    assert calls==[]


@pytest.mark.parametrize('mutation',['inactive','foreign','preference_disabled_after_staging'])
def test_committed_event_uses_created_in_app_recipients_and_current_access(review_db,monkeypatch,mutation):
    from core import event_bus
    from core.event_bus import InMemoryEventBus
    from core.base import AlertSeverity
    from modules.notifications import alert_dispatcher
    review_db.execute(text("INSERT INTO jobs(id,item_name,status,charged_to_org_id,charged_to_user_id) VALUES(883,'Synthetic staged queue','pending',1,4)"))
    review_db.add(AlertPreference(user_id=4,alert_type=AlertType.QUEUE_ADDED,in_app=True,email=False,browser_push=False));review_db.commit()
    monkeypatch.setattr(alert_dispatcher,'should_suppress_notification',lambda **_:True)
    plan=alert_dispatcher.stage_alert(review_db,AlertType.QUEUE_ADDED,AlertSeverity.INFO,'Synthetic frozen audience',job_id=883,target_user_ids=[4]);review_db.commit()
    if mutation=='inactive':review_db.execute(text('UPDATE users SET is_active=0 WHERE id=4'))
    elif mutation=='foreign':review_db.execute(text('UPDATE users SET group_id=2 WHERE id=4'))
    else:review_db.execute(text("UPDATE alert_preferences SET in_app=0 WHERE user_id=4 AND alert_type='queue_added'"))
    review_db.commit()
    events=[];bus=InMemoryEventBus();bus.subscribe('notifications.alert_dispatched',lambda event:events.append(event.data));monkeypatch.setattr(event_bus,'_bus',bus)
    alert_dispatcher.deliver_staged_alert(review_db,plan)
    if mutation=='preference_disabled_after_staging':
        assert len(events)==1 and events[0]['_ws_user_ids']==[4]
    else:assert events==[]


def test_actual_http_event_persists_websocket_recipient_audience(approval_client,review_db,monkeypatch):
    from core import event_bus,ws_hub
    from core.event_bus import InMemoryEventBus
    client,user=approval_client;configure_creator(review_db,user)
    bus=InMemoryEventBus();monkeypatch.setattr(event_bus,'_bus',bus)
    monkeypatch.setattr(ws_hub,'engine',review_db.get_bind())
    bus.subscribe('notifications.alert_dispatched',ws_hub._handle_alert_dispatched)
    ids=enter(client,'single')
    rows=review_db.execute(text("SELECT data FROM ws_events WHERE event_type='alert_new'")).scalars().all()
    assert len(rows)==1
    payload=json.loads(rows[0])
    assert payload['_audience']=={'user_ids':[3]}
    assert payload['data']['job_id']==ids[0]
    assert payload['data']['count']==1
    assert '_ws_user_ids' not in payload['data']


def test_event_publish_failure_preserves_committed_http_job_and_alert(approval_client,review_db,monkeypatch,caplog):
    from core import event_bus
    client,user=approval_client;configure_creator(review_db,user)
    class BrokenBus:
        def publish(self,event):raise RuntimeError('Synthetic private bus exception marker')
    monkeypatch.setattr(event_bus,'_bus',BrokenBus())
    ids=enter(client,'single')
    assert review_db.execute(text('SELECT COUNT(*) FROM jobs WHERE id=:id'),{'id':ids[0]}).scalar_one()==1
    assert review_db.execute(text('SELECT COUNT(*) FROM alerts WHERE job_id=:id'),{'id':ids[0]}).scalar_one()==1
    assert 'Synthetic private bus exception marker' not in caplog.text


@pytest.mark.parametrize('path',['orm','daemon'])
@pytest.mark.parametrize('org_quiet',[False,True])
def test_spool_origin_mqtt_uses_org_quiet_policy_not_opposing_global(review_db,monkeypatch,path,org_quiet):
    from datetime import datetime,timezone
    from types import SimpleNamespace
    from core import event_bus
    from core.event_bus import InMemoryEventBus
    from core.base import AlertSeverity
    from modules.notifications import alert_dispatcher,alert_dispatch,quiet_hours,mqtt_republish
    review_db.execute(text("INSERT INTO filament_library(id,brand,name,material) VALUES(101,'Synthetic','Test','PLA')"))
    review_db.execute(text('INSERT INTO spools(id,filament_id,org_id) VALUES(101,101,1)'))
    review_db.add(AlertPreference(user_id=3,alert_type=AlertType.SPOOL_LOW,in_app=True,email=False,browser_push=False))
    global_window=('22:00','07:00') if org_quiet else ('11:00','13:00')
    org_window=('11:00','13:00') if org_quiet else ('22:00','07:00')
    settings={'quiet_hours_enabled':True,'quiet_hours_start':org_window[0],'quiet_hours_end':org_window[1]}
    review_db.execute(text('UPDATE groups SET settings_json=:settings WHERE id=1'),{'settings':json.dumps(settings)})
    for key,value in {'quiet_hours_enabled':'true','quiet_hours_start':global_window[0],'quiet_hours_end':global_window[1],
            'mqtt_republish_enabled':'true','mqtt_republish_host':'synthetic.invalid','mqtt_republish_topic_prefix':'synthetic/spool'}.items():
        review_db.execute(text('INSERT INTO system_config(key,value) VALUES(:key,:value)'),{'key':key,'value':value})
    review_db.commit()
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None):return datetime(2026,10,11,12,0,tzinfo=timezone.utc)
    monkeypatch.setattr(quiet_hours,'datetime',Clock)
    monkeypatch.setattr(quiet_hours,'engine',review_db.get_bind());monkeypatch.setattr(quiet_hours,'_config_cache',None)
    monkeypatch.setattr(alert_dispatcher,'should_suppress_notification',quiet_hours.should_suppress_notification)
    monkeypatch.setattr(alert_dispatch,'engine',review_db.get_bind())
    monkeypatch.setattr(mqtt_republish,'engine',review_db.get_bind());monkeypatch.setattr(mqtt_republish,'_config_cache',None)
    calls=[];monkeypatch.setattr(mqtt_republish,'_get_client',lambda:SimpleNamespace(publish=lambda *args,**kwargs:calls.append(args)))
    bus=InMemoryEventBus();monkeypatch.setattr(event_bus,'_bus',bus);mqtt_republish.register_subscribers(bus)
    events=[];bus.subscribe('notifications.alert_dispatched',lambda event:events.append(event.data))
    if path=='orm':alert_dispatcher.dispatch_alert(review_db,AlertType.SPOOL_LOW,AlertSeverity.INFO,'Synthetic spool event',spool_id=101,target_user_ids=[3])
    else:alert_dispatch.dispatch_alert('spool_low','info','Synthetic spool event',spool_id=101)
    assert len(events)==1
    assert len(calls)==(0 if org_quiet else 1)
    if calls:
        payload=json.loads(calls[0][1]);assert 'spool_id' not in payload and '_ws_user_ids' not in payload
