"""Actual database configuration and UTC suppression boundaries."""
from datetime import datetime, timezone
import json
import pytest
from sqlalchemy import text
from tests.test_contracts.test_education_review_workflow import review_db

@pytest.fixture
def quiet_config(review_db,monkeypatch):
    from modules.notifications import quiet_hours
    monkeypatch.setattr(quiet_hours,'engine',review_db.get_bind())
    monkeypatch.setattr(quiet_hours,'_config_cache',None)
    monkeypatch.setattr(quiet_hours,'_config_ts',0)
    review_db.execute(text("INSERT INTO system_config(key,value) VALUES('quiet_hours_enabled','true'),('quiet_hours_start','22:00'),('quiet_hours_end','07:00')"));review_db.commit()
    return quiet_hours

@pytest.mark.parametrize('hour,minute,expected',[(21,59,False),(22,0,True),(23,59,True),(0,0,True),(6,59,True),(7,0,False)])
def test_database_system_window_half_open_boundaries(quiet_config,monkeypatch,hour,minute,expected):
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None):return datetime(2026,10,11,hour,minute,tzinfo=timezone.utc)
    monkeypatch.setattr(quiet_config,'datetime',Clock)
    assert quiet_config.should_suppress_notification() is expected

@pytest.mark.parametrize('hour,expected',[(11,False),(12,True),(13,True),(14,False),(23,False)])
def test_org_window_overrides_system_without_affecting_siblings(quiet_config,review_db,monkeypatch,hour,expected):
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None):return datetime(2026,10,11,hour,tzinfo=timezone.utc)
    monkeypatch.setattr(quiet_config,'datetime',Clock)
    settings={'quiet_hours_enabled':True,'quiet_hours_start':'12:00','quiet_hours_end':'14:00'}
    review_db.execute(text('UPDATE groups SET settings_json=:settings WHERE id=1'),{'settings':json.dumps(settings)});review_db.commit()
    assert quiet_config.should_suppress_notification(org_id=1) is expected
    assert quiet_config.should_suppress_notification(org_id=2) is (hour==23)

@pytest.mark.parametrize('path',['orm','daemon'])
@pytest.mark.parametrize('scoped',[False,True])
@pytest.mark.parametrize('hour,suppressed',[(23,True),(8,False)])
def test_real_configuration_controls_all_external_channels_and_retains_alerts(quiet_config,review_db,monkeypatch,path,scoped,hour,suppressed):
    from core.base import AlertType,AlertSeverity
    from core.registry import registry
    from modules.organizations.services import OrgSettingsService
    from modules.notifications import alert_dispatcher,alert_dispatch,channels
    from modules.notifications.models import AlertPreference
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None):return datetime(2026,10,11,hour,tzinfo=timezone.utc)
    monkeypatch.setattr(quiet_config,'datetime',Clock)
    # Bind the production suppression function, not a boolean substitute.
    monkeypatch.setattr(alert_dispatcher,'should_suppress_notification',quiet_config.should_suppress_notification)
    monkeypatch.setattr(alert_dispatch,'engine',review_db.get_bind())
    monkeypatch.setitem(registry._providers,'OrgSettingsProvider',OrgSettingsService())
    settings={'quiet_hours_enabled':True,'quiet_hours_start':'22:00','quiet_hours_end':'07:00','webhook_url':'https://synthetic.invalid/org'}
    review_db.execute(text('UPDATE groups SET settings_json=:settings WHERE id=1'),{'settings':json.dumps(settings)})
    for uid in [1,3,5]:review_db.add(AlertPreference(user_id=uid,alert_type=AlertType.PRINT_COMPLETE,in_app=True,email=True,browser_push=True))
    review_db.commit()
    calls={key:[] for key in ['email','push','global','org']}
    monkeypatch.setattr(channels,'send_email',lambda uid,*a,**k:calls['email'].append(uid))
    monkeypatch.setattr(channels,'send_push_notification',lambda uid,*a,**k:calls['push'].append(uid))
    monkeypatch.setattr(channels,'send_webhook',lambda *a,**k:calls['global'].append(a))
    monkeypatch.setattr(alert_dispatcher,'_deliver_email',lambda db,uid,*a:calls['email'].append(uid))
    monkeypatch.setattr(alert_dispatcher,'_deliver_browser_push',lambda db,uid,*a:calls['push'].append(uid))
    monkeypatch.setattr(alert_dispatcher,'_send_org_webhook',lambda *a:calls['org'].append(a))
    class Bus:
        def publish(self,event):pass
    monkeypatch.setattr(alert_dispatch,'get_event_bus',lambda:Bus())
    reference={'printer_id':9} if scoped else {}
    if path=='orm':alert_dispatcher.dispatch_alert(review_db,AlertType.PRINT_COMPLETE,AlertSeverity.INFO,'Synthetic configured suppression',**reference)
    else:alert_dispatch.dispatch_alert('print_complete','info','Synthetic configured suppression',**reference)
    with review_db.get_bind().connect() as connection:
        recipients=set(connection.execute(text('SELECT user_id FROM alerts')).scalars())
    expected={1,3} if scoped else {1,3,5}
    assert recipients==expected
    if suppressed:assert calls=={key:[] for key in calls}
    else:
        assert set(calls['email'])==expected and set(calls['push'])==expected
        assert len(calls['org'])==int(scoped)
        assert len(calls['global'])==int(not scoped)
