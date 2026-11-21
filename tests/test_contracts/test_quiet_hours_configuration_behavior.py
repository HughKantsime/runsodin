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


@pytest.mark.parametrize("legacy_saved_utc", [False, True])
def test_saved_zone_confirmation_and_org_fallback(quiet_config, review_db, monkeypatch, legacy_saved_utc):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from core.db import get_db
    from core.dependencies import get_current_user
    from core.farm_time import set_farm_timezone
    from modules.system import routes_config
    from modules.organizations import routes

    # Older settings screens saved the displayed school default with unrelated changes.
    if legacy_saved_utc:
        review_db.execute(text("UPDATE groups SET settings_json=:s WHERE id=1"), {'s': json.dumps({'timezone': 'UTC'})})
    set_farm_timezone(review_db, 'America/New_York')
    review_db.commit()
    admin = {'id': 3, 'role': 'admin', 'group_id': None}
    app = FastAPI()
    app.include_router(routes_config.router)
    app.include_router(routes.router)
    app.dependency_overrides[get_db] = lambda: review_db
    app.dependency_overrides[get_current_user] = lambda: admin
    monkeypatch.setattr(routes, 'require_feature', lambda key: None)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 7, 1, 10, tzinfo=timezone.utc)  # 06:00 New York, 12:00 Berlin
    monkeypatch.setattr(quiet_config, 'datetime', Clock)
    with TestClient(app) as client:
        system = '/config/quiet-hours'
        org = '/orgs/1/settings'
        assert client.get(system).json()['local_timezone'] == 'America/New_York'
        assert client.get(system).json()['timezone_confirmed'] is False
        assert not quiet_config.should_suppress_notification()
        assert client.put(system, json={'timezone_confirmed': 'false'}).status_code == 422
        assert client.put(system, json={'timezone_confirmed': True}).is_success
        assert client.get(system).json()['timezone_confirmed'] is True
        assert quiet_config.should_suppress_notification(2)
        assert client.get(org).json()['quiet_hours_local_timezone'] == 'America/New_York'
        assert client.put(org, json={'quiet_hours_enabled': True}).is_success
        assert not quiet_config.should_suppress_notification(1)
        assert client.put(org, json={'quiet_hours_timezone_confirmed': True}).is_success
        assert quiet_config.should_suppress_notification(1)
        from modules.organizations.education_program_policy import options
        assert options(review_db, 1)['timezone'] == 'UTC'
        assert client.get(org).json()['quiet_hours_local_timezone'] == 'America/New_York'
        assert client.put(org, json={'timezone': 'Europe/Berlin'}).is_success
        assert client.get(org).json()['quiet_hours_timezone_confirmed'] is True
        assert not quiet_config.should_suppress_notification(1)
        targets = {row['id']: row['config']['timezone'] for row in quiet_config.iter_orgs_with_digest_enabled()}
        assert targets == {1: 'Europe/Berlin', None: 'America/New_York'}
        assert client.put(org, json={'quiet_hours_enabled': False}).is_success
        assert quiet_config.should_suppress_notification(1)


@pytest.mark.parametrize('utc_start,utc_end', [
    ('2026-03-08T03:00', '2026-03-08T11:00'),
    ('2026-11-01T02:00', '2026-11-01T12:00'),
])
def test_overnight_suppression_and_digest_across_dst(utc_start, utc_end):
    from datetime import timedelta
    from modules.notifications import quiet_hours as qh

    config = {'enabled': True, 'start': '22:00', 'end': '07:00', 'timezone': 'America/New_York'}
    start, end = (datetime.fromisoformat(stamp).replace(tzinfo=timezone.utc) for stamp in (utc_start, utc_end))
    minute = timedelta(minutes=1)
    assert not qh._is_quiet_time_for_config(config, start - minute)
    for instant in (start, start + timedelta(hours=3), end - minute):
        assert qh._is_quiet_time_for_config(config, instant)
        assert qh.compute_last_window_end(config, instant) is None
    assert not qh._is_quiet_time_for_config(config, end)
    assert qh.compute_last_window_end(config, end) == end
    assert qh.compute_window_bounds(config, end) == (start, end)


@pytest.mark.parametrize('window,first,last', [
    (('01:30', '02:30'), '2026-03-08T06:30', '2026-03-08T07:00'),
    (('01:15', '01:45'), '2026-11-01T05:15', '2026-11-01T05:45'),
    (('01:15', '01:45'), '2026-11-01T06:15', '2026-11-01T06:45'),
])
def test_digest_handles_missing_and_repeated_wall_clock_endpoints(window, first, last):
    from modules.notifications import quiet_hours as qh
    config = dict(enabled=True, start=window[0], end=window[1], timezone='America/New_York')
    start, end = (datetime.fromisoformat(value).replace(tzinfo=timezone.utc) for value in (first, last))
    assert qh.compute_last_window_end(config, end) == end
    assert qh.compute_window_bounds(config, end) == (start, end)


@pytest.mark.parametrize("stored", ["1", "YES", '"true"'])
def test_daemon_and_get_agree_on_stored_confirmation(quiet_config, review_db, stored):
    import asyncio
    from core.farm_time import set_farm_timezone
    from modules.system.routes_config import get_quiet_hours_config
    set_farm_timezone(review_db, "America/New_York")
    review_db.execute(text("INSERT INTO system_config(key,value) VALUES('quiet_hours_timezone_confirmed', :v)"),
                      {"v": stored})
    review_db.commit()
    shown = asyncio.run(get_quiet_hours_config(db=review_db, current_user={}))
    assert shown["timezone_confirmed"] is True
    assert quiet_config._get_config()["timezone"] == shown["local_timezone"] == "America/New_York"


def test_one_org_zone_failure_does_not_skip_the_next_org(quiet_config, review_db, monkeypatch):
    for org_id, zone in [(1, "broken"), (2, "Europe/Berlin")]:
        review_db.execute(text("UPDATE groups SET settings_json=:s WHERE id=:id"),
                          {"s": json.dumps(dict(quiet_hours_enabled=True, quiet_hours_timezone_confirmed=True,
                                                timezone=zone)), "id": org_id})
    review_db.commit()
    resolve = quiet_config.alert_local_timezone
    def lookup(conn, settings):
        if settings["timezone"] == "broken":
            raise ValueError("unreadable zone")
        return resolve(conn, settings)
    monkeypatch.setattr(quiet_config, "alert_local_timezone", lookup)
    assert [target["id"] for target in quiet_config.iter_orgs_with_digest_enabled()] == [2, None]


def test_digest_readers_include_only_the_half_open_window(quiet_config, review_db, monkeypatch):
    start = datetime(2026, 10, 10, 22, tzinfo=timezone.utc)
    end = datetime(2026, 10, 11, 7, tzinfo=timezone.utc)
    monkeypatch.setattr(quiet_config, "_last_ended_window", lambda *_: (start, end))
    review_db.execute(text("INSERT INTO alerts(user_id, alert_type, severity, title, created_at) "
                           "VALUES(1, 'print_complete', 'info', :title, :created)"), [
        {"title": "start", "created": "2026-10-10 22:00:00"},
        {"title": "inside", "created": "2026-10-11T06:59:59"},
        {"title": "end", "created": "2026-10-11T07:00:00"},
    ])
    review_db.commit()
    for rows in (quiet_config.get_queued_alerts_for_digest(), quiet_config.get_suppressed_alerts_for_window(start, end)):
        assert [row["title"] for row in rows] == ["inside", "start"]


def test_digest_query_logs_the_original_exception(quiet_config, monkeypatch, caplog):
    def fail(*args):
        raise ValueError("unreadable digest bounds")
    monkeypatch.setattr(quiet_config, "_alert_window_filter", fail)
    now = datetime.now(timezone.utc)
    assert quiet_config.get_suppressed_alerts_for_window(now, now) == []
    assert any(record.exc_info and str(record.exc_info[1]) == "unreadable digest bounds"
               for record in caplog.records)
