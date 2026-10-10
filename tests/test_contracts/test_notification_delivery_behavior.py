"""Exercise the production alert dispatcher and persisted recipients, not a recorder."""
import pytest
from sqlalchemy import text
from tests.test_contracts.test_education_review_workflow import review_db

@pytest.fixture
def quiet_dispatch(review_db,monkeypatch):
    from modules.notifications import alert_dispatcher as dispatcher
    # No external messages. Actual quiet-hours branch and in-app delivery remain.
    monkeypatch.setattr(dispatcher,'should_suppress_notification',lambda **_:True)
    dispatcher.test_external_calls=[]
    def forbidden(*args,**kwargs):dispatcher.test_external_calls.append((args,kwargs))
    monkeypatch.setattr(dispatcher,'_deliver_email',forbidden)
    monkeypatch.setattr(dispatcher,'_deliver_browser_push',forbidden)
    return dispatcher


def test_printer_alert_only_reaches_active_authorized_org(quiet_dispatch,review_db):
    from core.base import AlertType,AlertSeverity
    review_db.execute(text('UPDATE users SET is_active=0 WHERE id=4'));review_db.commit()
    quiet_dispatch.dispatch_alert(review_db,AlertType.PRINT_COMPLETE,AlertSeverity.INFO,'Synthetic print',printer_id=9)
    recipients=set(review_db.execute(text('SELECT user_id FROM alerts')).scalars())
    assert recipients=={1,2,3},recipients


def test_explicit_target_does_not_override_resource_org(quiet_dispatch,review_db):
    from core.base import AlertType,AlertSeverity
    quiet_dispatch.dispatch_alert(review_db,AlertType.PRINT_COMPLETE,AlertSeverity.INFO,'Synthetic private print',printer_id=9,target_user_ids=[3,5])
    assert set(review_db.execute(text('SELECT user_id FROM alerts')).scalars())=={3}


def test_preferences_and_quiet_hours_persist_only_selected_in_app(quiet_dispatch,review_db):
    from core.base import AlertType,AlertSeverity
    from modules.notifications.models import AlertPreference
    review_db.add(AlertPreference(user_id=1,alert_type=AlertType.PRINT_COMPLETE,in_app=True,email=True,browser_push=True))
    review_db.add(AlertPreference(user_id=2,alert_type=AlertType.PRINT_COMPLETE,in_app=False,email=True,browser_push=True));review_db.commit()
    assert quiet_dispatch.dispatch_alert(review_db,AlertType.PRINT_COMPLETE,AlertSeverity.INFO,'Synthetic quiet',printer_id=9)==1
    assert list(review_db.execute(text('SELECT user_id FROM alerts')).scalars())==[1]
    assert quiet_dispatch.test_external_calls==[]

@pytest.mark.parametrize('kind',['orm','daemon'])
@pytest.mark.parametrize('reference,expected_org',[({'printer_id':9},1),({'job_id':13},1),({'spool_id':101},1),({},None)])
def test_production_dispatchers_scope_all_channels(review_db,monkeypatch,kind,reference,expected_org):
    from core.base import AlertType,AlertSeverity
    from modules.notifications import alert_dispatcher,alert_dispatch,channels,quiet_hours
    from modules.notifications.models import AlertPreference
    from core.registry import registry
    review_db.execute(text("INSERT INTO filament_library(id,brand,name,material) VALUES(101,'Synthetic','Test','PLA')"))
    review_db.execute(text("INSERT INTO spools(id,filament_id,org_id) VALUES(101,101,1)"))
    review_db.execute(text('UPDATE users SET is_active=0 WHERE id=4'));review_db.commit()
    for user_id in [1,3,4,5]:review_db.add(AlertPreference(user_id=user_id,alert_type=AlertType.PRINT_COMPLETE,in_app=True,email=True,browser_push=True))
    review_db.commit()
    calls={'push':[],'email':[],'global':[],'org':[],'events':[]}
    monkeypatch.setattr(alert_dispatcher,'should_suppress_notification',lambda **_:False)
    monkeypatch.setattr(quiet_hours,'should_suppress_notification',lambda **_:False)
    monkeypatch.setattr(channels,'send_webhook',lambda *a,**k:calls['global'].append(a))
    monkeypatch.setattr(channels,'send_push_notification',lambda uid,*a,**k:calls['push'].append(uid))
    monkeypatch.setattr(channels,'send_email',lambda uid,*a,**k:calls['email'].append(uid))
    monkeypatch.setattr(alert_dispatcher,'_deliver_browser_push',lambda db,uid,*a:calls['push'].append(uid))
    monkeypatch.setattr(alert_dispatcher,'_deliver_email',lambda db,uid,*a:calls['email'].append(uid))
    monkeypatch.setattr(alert_dispatcher,'_send_org_webhook',lambda *a:calls['org'].append(a))
    from modules.organizations.services import OrgSettingsService
    review_db.execute(text("UPDATE groups SET settings_json=:settings WHERE id=1"),
                      {'settings':'{"webhook_url":"https://tenant-1.example.invalid/hook"}'})
    review_db.commit()
    monkeypatch.setattr(registry,'_providers',registry.providers|{'OrgSettingsProvider':OrgSettingsService()})
    class Bus:
        def publish(self,event):calls['events'].append(event.data)
    monkeypatch.setattr(alert_dispatch,'get_event_bus',lambda:Bus())
    monkeypatch.setattr(alert_dispatch,'engine',review_db.get_bind())
    if kind=='orm':alert_dispatcher.dispatch_alert(review_db,AlertType.PRINT_COMPLETE,AlertSeverity.INFO,'Synthetic channel scope',**reference)
    else:alert_dispatch.dispatch_alert('print_complete','info','Synthetic channel scope',**reference)
    expected={1,3} if expected_org else {1,3,5}
    with review_db.get_bind().connect() as conn:assert set(conn.execute(text('SELECT user_id FROM alerts')).scalars())==expected
    assert set(calls['email'])==expected
    assert set(calls['push'])==expected
    assert len(calls['global'])==(0 if expected_org else 1)
    assert len(calls['org'])==(1 if expected_org else 0)
    if expected_org:assert calls['org'][0][0]=='https://tenant-1.example.invalid/hook'
    for event in calls['events']:assert set(event['_ws_user_ids'])==expected

@pytest.mark.parametrize('kind',['orm','daemon'])
@pytest.mark.parametrize('reference',[{'printer_id':99999},{'job_id':99999},{'spool_id':99999},{'printer_id':9,'spool_id':102}])
def test_invalid_or_conflicting_scope_has_no_side_effects(review_db,monkeypatch,kind,reference):
    from core.base import AlertType,AlertSeverity
    from modules.notifications import alert_dispatcher,alert_dispatch,channels
    review_db.execute(text("INSERT INTO filament_library(id,brand,name,material) VALUES(101,'Synthetic','Test','PLA')"))
    review_db.execute(text("INSERT INTO spools(id,filament_id,org_id) VALUES(102,101,2)"));review_db.commit()
    unexpected=[]
    def forbidden(*a,**k):unexpected.append((a,k))
    monkeypatch.setattr(alert_dispatcher,'seed_alert_preferences',forbidden)
    monkeypatch.setattr(channels,'send_webhook',forbidden)
    monkeypatch.setattr(channels,'send_email',forbidden)
    monkeypatch.setattr(channels,'send_push_notification',forbidden)
    monkeypatch.setattr(alert_dispatcher,'_send_org_webhook',forbidden)
    monkeypatch.setattr(alert_dispatcher,'_deliver_email',forbidden)
    monkeypatch.setattr(alert_dispatcher,'_deliver_browser_push',forbidden)
    monkeypatch.setattr(alert_dispatch,'get_event_bus',forbidden)
    monkeypatch.setattr(alert_dispatch,'engine',review_db.get_bind())
    if kind=='orm':assert alert_dispatcher.dispatch_alert(review_db,AlertType.PRINT_COMPLETE,AlertSeverity.INFO,'Invalid scope',**reference)==0
    else:alert_dispatch.dispatch_alert('print_complete','info','Invalid scope',**reference)
    assert review_db.execute(text('SELECT count(*) FROM alerts')).scalar_one()==0
    assert review_db.execute(text('SELECT count(*) FROM alert_preferences')).scalar_one()==0

    assert unexpected==[]


def test_daemon_same_title_different_tenants_not_deduplicated(review_db,monkeypatch):
    from modules.notifications import alert_dispatch,quiet_hours
    from modules.notifications.models import AlertPreference
    from core.base import AlertType
    review_db.execute(text("INSERT INTO filament_library(id,brand,name,material) VALUES(101,'Synthetic','Test','PLA')"))
    review_db.execute(text('INSERT INTO spools(id,filament_id,org_id) VALUES(101,101,1),(102,101,2)'))
    for uid in [1,3,5]:review_db.add(AlertPreference(user_id=uid,alert_type=AlertType.SPOOL_LOW,in_app=True,email=False,browser_push=False))
    review_db.commit()
    monkeypatch.setattr(alert_dispatch,'engine',review_db.get_bind())
    monkeypatch.setattr(quiet_hours,'should_suppress_notification',lambda **_:True)
    events=[]
    class Bus:
        def publish(self,event):events.append(event.data)
    monkeypatch.setattr(alert_dispatch,'get_event_bus',lambda:Bus())
    for spool_id in [101,102,101]:alert_dispatch.dispatch_alert('spool_low','warning','Same stock warning',spool_id=spool_id)
    with review_db.get_bind().connect() as conn:
        assert set(conn.execute(text('SELECT user_id,spool_id FROM alerts')).all())=={(1,101),(3,101),(5,102)}
        assert conn.execute(text('SELECT count(*) FROM alerts')).scalar_one()==3
    assert len(events)==2
    assert [set(e['_ws_user_ids']) for e in events]==[{1,3},{5}]
