"""Phone delivery contracts using disposable databases and mocked transports."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import text

from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401


@pytest.fixture
def alert_db(review_db, monkeypatch):
    from core.economic_source_creation import _create_spool
    from modules.notifications import alert_dispatch, alert_dispatcher, channels, quiet_hours
    from modules.push import fanout
    db = review_db
    db.execute(text("INSERT INTO filament_library(id,brand,name,material) VALUES(32,'Test','PLA','PLA')"))
    spool = _create_spool(db, {'filament_id': 32, 'org_id': 1, 'remaining_weight_g': 60},
                          owner_org_id=1)
    db.info['alert_spool_id'] = spool.id
    db.execute(text("UPDATE filament_slots SET assigned_spool_id=:spool WHERE printer_id=9"),
               {'spool': spool.id})
    for kind in ('spool_low', 'printer_error', 'print_complete'):
        db.execute(text("INSERT INTO alert_preferences(user_id,alert_type,in_app,browser_push,email,threshold_value) "
                        "VALUES(3,:kind,1,1,1,100)"), {'kind': kind})
    db.commit()
    monkeypatch.setattr(alert_dispatch, 'engine', db.get_bind())
    monkeypatch.setattr(quiet_hours, 'should_suppress_notification', lambda **_: False)
    monkeypatch.setattr(alert_dispatcher, 'should_suppress_notification', lambda **_: False)
    for module, names in ((channels, ('send_webhook', 'send_email', 'send_push_notification')),
                          (alert_dispatcher, ('_deliver_email', '_deliver_browser_push', '_send_org_webhook'))):
        for name in names:
            monkeypatch.setattr(module, name, Mock())
    monkeypatch.setattr(fanout, 'get_provider', Mock(return_value=Mock()))
    return db


def mqtt_check(db, monkeypatch):
    from modules.printers.monitors import mqtt_printer

    @contextmanager
    def connection():
        conn = db.get_bind().raw_connection()
        try:
            yield conn
        finally:
            conn.close()

    monkeypatch.setattr(mqtt_printer, 'get_db', connection)
    monitor = object.__new__(mqtt_printer.PrinterMonitor)
    monitor.printer_id = 9
    monitor.name = 'Test printer'
    monitor._state = {'ams': {'present': True}}
    monitor._check_spool_levels()


def test_mqtt_unread_spool_dedupes_beyond_five_minute_window(alert_db, monkeypatch):
    """MQTT used a separate five-minute title check, bypassing unread-spool dedupe."""
    mqtt_check(alert_db, monkeypatch)
    assert alert_db.execute(text('SELECT count(*) FROM alerts')).scalar_one() == 1
    alert_db.execute(text('UPDATE alerts SET created_at=:old'),
                     {'old': datetime.now(timezone.utc) - timedelta(minutes=6)})
    alert_db.commit()
    mqtt_check(alert_db, monkeypatch)
    assert alert_db.execute(text('SELECT count(*) FROM alerts')).scalar_one() == 1


def send(db, path, kind='printer_error', message='[bambu_hms:123] original', **kwargs):
    from core.base import AlertType, AlertSeverity
    from modules.notifications import alert_dispatch, alert_dispatcher
    args = dict(alert_type=AlertType(kind), severity=AlertSeverity.WARNING,
                title='Error on Test printer', message=message, printer_id=9, **kwargs)
    if path == 'orm':
        return alert_dispatcher.dispatch_alert(db, **args)
    return alert_dispatch.dispatch_alert(**args)


@pytest.mark.parametrize('path', ['orm', 'daemon'])
@pytest.mark.parametrize('case,expected', [
    ('same', 1), ('different', 2), ('read', 2), ('dismissed', 2), ('expired', 2),
    ('legacy', 1), ('full_message_same', 1), ('full_message_different', 2),
])
def test_error_repeat_policy(alert_db, path, case, expected):
    first = 'plain error' if case.startswith('full_message') else '[bambu_hms:123] original'
    send(alert_db, path, message=first)
    if case in {'read', 'dismissed'}:
        column = 'is_read' if case == 'read' else 'is_dismissed'
        alert_db.execute(text(f'UPDATE alerts SET {column}=TRUE'))
    elif case == 'expired':
        alert_db.execute(text('UPDATE alerts SET created_at=:old'),
                         {'old': datetime.now(timezone.utc) - timedelta(hours=6, seconds=1)})
    elif case == 'legacy':
        alert_db.execute(text("UPDATE alerts SET alert_type='PRINTER_ERROR',severity='WARNING'"))
    alert_db.commit()
    second = ('plain error' if case == 'full_message_same' else 'another plain error'
              if case == 'full_message_different' else '[bambu_hms:456] new'
              if case == 'different' else '[bambu_hms:123] changed description')
    send(alert_db, path, message=second)
    assert alert_db.execute(text('SELECT count(*) FROM alerts')).scalar_one() == expected


@pytest.mark.parametrize('path', ['orm', 'daemon', 'mqtt'])
def test_legacy_spool_and_duplicate_preferences(alert_db, monkeypatch, path):
    alert_db.execute(text("UPDATE alert_preferences SET alert_type='SPOOL_LOW' WHERE alert_type='spool_low'"))
    alert_db.execute(text("INSERT INTO alert_preferences(user_id,alert_type,in_app,browser_push,email) "
                          "VALUES(3,'spool_low',1,1,1)"))
    alert_db.commit()
    def dispatch():
        if path == 'mqtt':
            mqtt_check(alert_db, monkeypatch)
        else:
            send(alert_db, path, 'spool_low', spool_id=alert_db.info['alert_spool_id'])
    dispatch()
    assert alert_db.execute(text('SELECT count(*) FROM alerts')).scalar_one() == 1
    alert_db.execute(text("UPDATE alerts SET alert_type='SPOOL_LOW',severity='WARNING',created_at='2020-01-01'"))
    alert_db.commit()
    dispatch()
    assert alert_db.execute(text('SELECT count(*) FROM alerts')).scalar_one() == 1


@pytest.fixture
def devices(alert_db, monkeypatch):
    from modules.push import fanout
    from modules.push.models import PushDevice
    for uid in (3, 5):
        alert_db.add(PushDevice(user_id=uid, device_id=f'device-{uid}',
                               platform='apns', token=f'synthetic-device-token-{uid}'))
    alert_db.commit()
    provider = Mock()
    monkeypatch.setattr(fanout, 'get_provider', Mock(return_value=provider))
    return provider


@pytest.mark.parametrize('path', ['orm', 'daemon', 'mqtt'])
@pytest.mark.parametrize('kind,category', [('printer_error', 'HMS_ERROR'), ('spool_low', 'SPOOL_LOW')])
def test_operational_push_and_fully_deduped_external_silence(alert_db, monkeypatch, devices, path, kind, category):
    from modules.notifications import alert_dispatcher, channels
    if path == 'mqtt' and kind == 'printer_error':
        # Exercise the MQTT forwarding method for HMS as well as the spool checker.
        from modules.printers.monitors.mqtt_printer import PrinterMonitor
        from core.base import AlertType, AlertSeverity
        monitor = object.__new__(PrinterMonitor)
        monitor.printer_id = 9
        dispatch = lambda: monitor._dispatch_alert(AlertType.PRINTER_ERROR, AlertSeverity.WARNING,
                                                   'Error on Test printer', '[hms:123] detail')
    elif path == 'mqtt':
        dispatch = lambda: mqtt_check(alert_db, monkeypatch)
    else:
        dispatch = lambda: send(alert_db, path, kind,
                                spool_id=alert_db.info['alert_spool_id'] if kind == 'spool_low' else None)
    dispatch()
    assert devices.send_push_result.call_count == 1
    assert devices.send_push_result.call_args.kwargs['category'] == category
    assert devices.send_push_result.call_args.kwargs['device_token'] == 'synthetic-device-token-3'
    sinks = [getattr(module, name) for module, names in (
        (alert_dispatcher, ('_deliver_email', '_deliver_browser_push', '_send_org_webhook')),
        (channels, ('send_webhook', 'send_email', 'send_push_notification')))
        for name in names]
    for sink in sinks:
        sink.reset_mock()
    dispatch()
    assert devices.send_push_result.call_count == 1
    for sink in sinks:
        sink.assert_not_called()


def test_push_copy_truncation_and_allowlisted_payload(alert_db, devices):
    message = 'detail ' * 50
    send(alert_db, 'orm', message=message)
    payload = devices.send_push_result.call_args.kwargs
    assert payload['title'].endswith(': print needs attention')
    assert payload['body'] == message[:180]
    assert set(payload['data']) == {'odin_event', 'alert_type', 'severity', 'printer_id'}


@pytest.mark.parametrize('prefs', [
    {'categories': {'HMS_ERROR': False}},
    {'quiet_hours_enabled': True, 'quiet_hours_start': '00:00', 'quiet_hours_end': '23:59'},
])
def test_device_preferences(alert_db, devices, prefs):
    import json
    alert_db.execute(text('UPDATE push_devices SET preferences_json=:prefs'), {'prefs': json.dumps(prefs)})
    alert_db.commit()
    send(alert_db, 'orm')
    devices.send_push_result.assert_not_called()


def test_canonical_preference_opt_out_wins_over_legacy(alert_db, devices):
    alert_db.execute(text("INSERT INTO alert_preferences(user_id,alert_type,in_app) VALUES(3,'PRINTER_ERROR',1)"))
    alert_db.execute(text("UPDATE alert_preferences SET in_app=FALSE WHERE alert_type='printer_error'"))
    alert_db.commit()
    send(alert_db, 'orm')
    devices.send_push_result.assert_not_called()


@pytest.mark.parametrize('path', ['orm', 'daemon'])
def test_education_suppressed(alert_db, devices, path):
    send(alert_db, path, job_id=13)
    devices.send_push_result.assert_not_called()


def test_education_mode_suppresses_resource_only_alert(alert_db, devices):
    alert_db.execute(text("INSERT INTO system_config(key,value) VALUES('education_mode','true')"))
    alert_db.commit()
    send(alert_db, 'orm')
    devices.send_push_result.assert_not_called()


def test_no_push_before_commit_or_after_rollback(alert_db, devices):
    from core.base import AlertType, AlertSeverity
    from modules.notifications.alert_dispatcher import stage_alert
    stage_alert(alert_db, AlertType.PRINTER_ERROR, AlertSeverity.WARNING, 'Staged', printer_id=9)
    devices.send_push_result.assert_not_called()
    alert_db.rollback()
    assert alert_db.execute(text('SELECT count(*) FROM alerts')).scalar_one() == 0


def test_terminal_outbox_delivers_once(alert_db, devices, monkeypatch):
    from modules.notifications import alert_outbox, alert_dispatcher
    from core.base import AlertType, AlertSeverity
    # Use the real outbox claim/commit/delivery/replay; terminal payload construction
    # is already covered by the foundation contracts.
    alert_db.execute(text("INSERT INTO jobs(id,item_name,status,submitted_by,charged_to_org_id) "
                          "VALUES(99,'Synthetic print','completed',3,1)"))
    identifier = alert_outbox.enqueue(alert_db, 99, 'queue_added', 3)
    alert_db.execute(text("UPDATE alert_outbox SET alert_kind='terminal:99' WHERE id=:id"), {'id': identifier})
    alert_db.commit()
    monkeypatch.setattr(alert_outbox, '_stage_terminal', lambda db, *a, **k:
        alert_dispatcher.stage_alert(db, AlertType.PRINT_COMPLETE, AlertSeverity.INFO,
                                     'Print Complete', 'Finished', printer_id=9, job_id=99))
    assert alert_outbox.dispatch(alert_db.get_bind(), identifier)
    assert devices.send_push_result.call_count == 1
    assert not alert_outbox.dispatch(alert_db.get_bind(), identifier)
    assert devices.send_push_result.call_count == 1


def test_pending_spool_alert_dedupes_without_autoflush(alert_db):
    from core.base import AlertType, AlertSeverity
    from modules.notifications.alert_dispatcher import stage_alert
    alert_db.autoflush = False
    first = stage_alert(alert_db, AlertType.SPOOL_LOW, AlertSeverity.WARNING, 'Low',
                        spool_id=alert_db.info['alert_spool_id'])
    second = stage_alert(alert_db, AlertType.SPOOL_LOW, AlertSeverity.WARNING, 'Low again',
                         spool_id=alert_db.info['alert_spool_id'])
    assert first.alerts_created == 1
    assert second.alerts_created == 0 and second.suppress_external


def test_unknown_category_allowed_by_default(alert_db, devices):
    alert_db.execute(text("INSERT INTO alert_preferences(user_id,alert_type,in_app) VALUES(3,'bed_cooled',1)"))
    alert_db.commit()
    send(alert_db, 'orm', kind='bed_cooled')
    assert devices.send_push_result.call_count == 1
    assert devices.send_push_result.call_args.kwargs['category'] == ''


@pytest.mark.parametrize('path', ['orm', 'daemon'])
def test_uppercase_only_preference_delivers(alert_db, devices, path):
    alert_db.execute(text("UPDATE alert_preferences SET alert_type='PRINTER_ERROR' WHERE alert_type='printer_error'"))
    alert_db.commit()
    send(alert_db, path)
    assert devices.send_push_result.call_count == 1
    assert devices.send_push_result.call_args.kwargs['category'] == 'HMS_ERROR'


@pytest.mark.parametrize('path', ['orm', 'daemon'])
def test_dedupe_is_per_user_and_printer(alert_db, devices, path):
    send(alert_db, path)
    alert_db.execute(text("INSERT INTO alert_preferences(user_id,alert_type,in_app) VALUES(1,'printer_error',1)"))
    alert_db.commit()
    send(alert_db, path)
    assert set(alert_db.execute(text('SELECT user_id FROM alerts')).scalars()) == {1, 3}
    assert alert_db.execute(text('SELECT count(*) FROM alerts')).scalar_one() == 2
    assert devices.send_push_result.call_count == 1  # user 3 was deduped; user 1 has no device
    from modules.notifications.alert_policy import active_resource_duplicate
    assert not active_resource_duplicate(alert_db, 3, 'printer_error', 10, None, '[bambu_hms:123] original')


@pytest.mark.parametrize('change', ['inactive', 'org', 'preference'])
def test_push_rechecks_eligibility_after_commit(alert_db, devices, change):
    from core.base import AlertType, AlertSeverity
    from modules.notifications.alert_dispatcher import stage_alert, deliver_staged_alert
    plan = stage_alert(alert_db, AlertType.PRINTER_ERROR, AlertSeverity.WARNING, 'Error', printer_id=9)
    alert_db.commit()
    if change == 'inactive':
        alert_db.execute(text('UPDATE users SET is_active=FALSE WHERE id=3'))
    elif change == 'org':
        alert_db.execute(text('DELETE FROM education_cost_center_printers WHERE printer_id=9'))
        alert_db.execute(text('UPDATE printers SET org_id=2 WHERE id=9'))
    else:
        alert_db.execute(text("UPDATE alert_preferences SET in_app=FALSE WHERE alert_type='printer_error'"))
    alert_db.commit()
    deliver_staged_alert(alert_db, plan)
    devices.send_push_result.assert_not_called()
