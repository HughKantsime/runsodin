"""Pause reminders against a real disposable database; external delivery is stubbed."""
import json
from datetime import timedelta

import pytest
from sqlalchemy import text
from modules.notifications import job_faults, pause_reminders, alert_dispatcher
from modules.notifications.pause_reminder_settings import read_settings, store_settings, read_json
from tests.test_contracts.test_temperature_alerts import farm, execute, alerts, BASE  # noqa: F401


@pytest.fixture
def paused(farm):
    execute(farm, "UPDATE alert_preferences SET alert_type='print_paused'")
    execute(farm, "INSERT INTO printers(id,name,api_type,is_active,gcode_state,last_seen) "
                  "VALUES(1,'Printer One','moonraker',1,'PAUSE',:at)", at=BASE)
    execute(farm, "INSERT INTO print_jobs(id,printer_id,job_name,filename,status,started_at) "
                  "VALUES(1,1,'Gear','gear.gcode','running',:at)", at=BASE - timedelta(minutes=5))
    from core.physical_print import pause_identity
    attempt = pause_identity(execute(farm, 'SELECT economic_uid FROM print_jobs WHERE id=1').scalar(), 1)
    farm.item = dict(printer=1, job=None, attempt=attempt, kind='paused', occurred=BASE, context={}, code=None)
    with farm.factory() as db:
        job_faults.stage(db, farm.item)
        db.commit()
    execute(farm, 'DELETE FROM alerts')
    def run(hours, fresh=True):
        now = BASE + timedelta(hours=hours)
        if fresh:
            execute(farm, 'UPDATE printers SET last_seen=:at', at=now)
        return pause_reminders.run_pass(farm.factory, now=now)
    farm.run = run
    return farm


def configure(farm, **changes):
    with farm.factory() as db:
        store_settings(db, {**read_settings(db), **changes})
        db.commit()


def transition(farm, kind, hours, **changes):
    with farm.factory() as db:
        job_faults.stage(db, {**farm.item, 'kind': kind, 'occurred': BASE + timedelta(hours=hours), **changes})
        db.commit()


def test_first_repeat_restart_and_new_episode(paused):
    assert paused.run(1.99) == []
    assert paused.run(2) == [1]
    assert paused.run(2) == []  # no worker memory: the committed marker prevents repeats
    assert paused.run(13.99) == []
    assert paused.run(14) == [1]
    assert [a['metadata']['reminder'] for a in alerts(paused) if a['user_id'] == 1] == [1, 2]
    transition(paused, 'resumed', 15)
    transition(paused, 'paused', 16)
    assert paused.run(17.99) == []
    assert paused.run(18) == [1]
    assert alerts(paused)[-1]['metadata']['reminder'] == 1


@pytest.mark.parametrize('condition', ['resumed', 'completed', 'cancelled', 'failed', 'offline', 'not_paused', 'ended', 'off', 'no_since', 'education', 'reserved'])
def test_does_not_remind_when_ineligible(paused, condition):
    if condition in ('resumed', 'completed', 'cancelled', 'failed'):
        transition(paused, condition, 1)
    elif condition == 'not_paused':
        execute(paused, "UPDATE printers SET gcode_state='RUNNING'")
    elif condition == 'ended':
        execute(paused, "UPDATE print_jobs SET status='completed'")
    elif condition == 'off':
        configure(paused, enabled=False)
    elif condition == 'no_since':
        execute(paused, "UPDATE system_config SET value=:value WHERE key='pause_episode:1'", value=json.dumps({'paused': True, 'at': BASE.isoformat()}))
    elif condition == 'education':
        execute(paused, "INSERT INTO system_config(key,value) VALUES('education_mode','true')")
    elif condition == 'reserved':
        execute(paused, "UPDATE print_jobs SET filename='odin-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.gcode'")
    execute(paused, 'DELETE FROM alerts')
    assert paused.run(3, fresh=condition != 'offline') == []
    assert alerts(paused) == []


def test_once_only_and_custom_threshold(paused):
    configure(paused, first_hours=0.25, repeat_hours=0)
    assert paused.run(0.24) == []
    assert paused.run(0.25) == [1]
    assert paused.run(100) == []


def test_pause_since_survives_restart_and_identity_becoming_known(paused):
    transition(paused, 'paused', 1, attempt=None)
    with paused.factory() as db:
        episode = read_json(db, 'pause_episode:1')
    assert episode['since'] == BASE.isoformat()
    assert episode['at'] == (BASE + timedelta(hours=1)).isoformat()
    assert paused.run(2) == [1]


def test_legacy_episode_adopts_its_saved_time(paused):
    execute(paused, "UPDATE system_config SET value=:v WHERE key='pause_episode:1'",
            v=json.dumps({'paused': True, 'at': BASE.isoformat(), 'attempt': paused.item['attempt']}))
    transition(paused, 'paused', 1)
    assert paused.run(2) == [1]


@pytest.mark.parametrize('cause,expected', [
    ('filament', 'Waiting for filament. Load filament at the printer, then resume.'),
    ('spaghetti', 'Paused for a possible failed print. Check the plate, then resume or stop.'),
    ('error', 'Door open. Check the printer, then resume or stop.'),
    ('planned', 'Planned pause. Do what the pause is for, then resume.'),
    (None, 'Check the printer, then resume or stop.'),
])
def test_each_cause_and_day_copy(paused, cause, expected):
    with paused.factory() as db:
        title, message = pause_reminders.describe(db, dict(printer=1, printer_name='Printer One', job=None,
            job_name='Gear', filename='gear.gcode', cause=cause, reason='Door open', episode={'context': {'pause_reason': 'Door open'}}), 27)
        assert title == 'Printer One: Gear still paused after 1 day 3 hours'
        assert message == expected


def test_humanize_preserves_reminder_title_and_preferences(paused):
    execute(paused, 'UPDATE alert_preferences SET in_app=0 WHERE user_id=2')
    assert paused.run(2) == [1]
    rows = alerts(paused)
    assert len(rows) == 1 and rows[0]['user_id'] == 1
    assert rows[0]['title'] == 'Printer One: Gear still paused after 2 hours'
    assert rows[0]['metadata'] == {'kind': 'pause_reminder', 'cause': 'planned', 'hours': 2,
                                   'since': BASE.isoformat(), 'reminder': 1}
    assert rows[0]['severity'].lower() == 'critical'


def test_quiet_hours_keep_inbox_and_defer_channels(paused, monkeypatch):
    monkeypatch.setattr(alert_dispatcher, 'should_suppress_notification', lambda **_: True)
    monkeypatch.setattr('modules.notifications.deferred_deliveries.webhook_destination_exists', lambda *_: True)
    execute(paused, 'UPDATE alert_preferences SET browser_push=1,email=1')
    execute(paused, 'UPDATE alert_preferences SET in_app=0 WHERE user_id=2')
    assert paused.run(2) == [1]
    assert [row['user_id'] for row in alerts(paused)] == [1]
    assert paused.delivered[0].suppress_external is True
    rows = execute(paused, 'SELECT user_id,channel,state FROM quiet_hours_deferred_deliveries').all()
    assert set(rows) == {(2, 'email', 'pending'), (2, 'push', 'pending'), (None, 'webhook', 'pending')}


def test_failed_staging_rolls_back_and_retries(paused, monkeypatch, caplog):
    original = alert_dispatcher.stage_alert
    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError('synthetic failure')
    monkeypatch.setattr(alert_dispatcher, 'stage_alert', fail)
    assert paused.run(2) == []
    assert alerts(paused) == []
    with paused.factory() as db:
        assert read_json(db, 'pause_reminder:1') == {}
    assert 'will retry' in caplog.text
    monkeypatch.setattr(alert_dispatcher, 'stage_alert', original)
    assert paused.run(2) == [1]


def test_runout_matches_print_and_pause_and_clears_on_resume(paused):
    transition(paused, 'resumed', 1)
    transition(paused, 'runout', 1.1)
    transition(paused, 'resumed', 1.2)  # repeated RUNNING telemetry must not discard runout
    transition(paused, 'paused', 1.3)
    assert paused.run(3.3) == [1]
    assert alerts(paused)[-1]['metadata']['cause'] == 'filament'
    transition(paused, 'resumed', 4)
    transition(paused, 'paused', 5)
    assert paused.run(7) == [1]
    assert alerts(paused)[-1]['metadata']['cause'] == 'planned'


def test_runout_from_another_print_or_after_pause_is_not_filament(paused):
    transition(paused, 'runout', 1, attempt='other')
    transition(paused, 'paused', 2)
    assert paused.run(4) == [1]
    assert alerts(paused)[-1]['metadata']['cause'] == 'planned'
    transition(paused, 'runout', 5)
    assert paused.run(16) == [1]
    assert alerts(paused)[-1]['metadata']['cause'] == 'planned'


def test_spaghetti_wins_over_runout(paused):
    transition(paused, 'resumed', 1)
    transition(paused, 'runout', 1.1)
    transition(paused, 'paused', 1.2)
    execute(paused, "UPDATE printers SET print_stage='Paused: hms=0C000300_00030008'")
    assert paused.run(3.2) == [1]
    assert alerts(paused)[-1]['metadata']['cause'] == 'spaghetti'


@pytest.mark.parametrize('source', ['active', 'history', 'episode'])
def test_paused_print_reminder_keeps_information_level_detection(paused, source):
    from modules.printers.hms_codes import parse_hms_errors
    entry, = parse_hms_errors([{'attr': 0x0C000300, 'code': 0x30008}])
    assert entry['severity'] == 'info'
    if source == 'episode':
        transition(paused, 'resumed', 0)
        transition(paused, 'paused', 0, code=entry['code'])
    execute(paused, "UPDATE printers SET api_type='bambu', hms_errors=:entries, last_error_code=:code, "
                    "last_error_at=:at", entries=json.dumps([entry] if source == 'active' else []),
            code=entry['code'] if source == 'history' else None, at=BASE)
    assert paused.run(2) == [1]
    assert alerts(paused)[-1]['metadata']['cause'] == 'spaghetti'


def test_mqtt_records_information_level_pause_cause(monkeypatch):
    from unittest.mock import Mock
    from core import job_timeline
    from modules.printers.monitors.mqtt_printer import PrinterMonitor
    from modules.printers.monitors import pause_cause
    monitor = object.__new__(PrinterMonitor)
    monitor.printer_id, monitor.name, monitor._current_job_id = 1, 'Printer', 1
    monitor._state = {'gcode_state': 'PAUSE', 'print_error': 0, 'hms': [{'attr': 0x0C000300, 'code': 0x30008}]}
    record = Mock()
    monkeypatch.setattr(job_timeline, 'for_printer', record)
    monitor._on_state_change('RUNNING', 'PAUSE')
    assert record.call_args.args == (1, 'paused')
    assert record.call_args.kwargs['code'] == '0C000300_00030008'
    assert record.call_args.kwargs['context']['hms_codes'] == ['0C000300_00030008']
    pause_cause.observe(monitor, monitor._state)
    stored = pause_cause.stage(monitor, '')
    assert stored == 'Paused: hms=0C000300_00030008'
    assert pause_cause.display_stage(stored) != 'Paused: Cause unavailable'


@pytest.mark.parametrize('brand', ['bambu', 'moonraker'])
def test_monitor_runout_reaches_filament_classifier(paused, monkeypatch, brand):
    import time
    from unittest.mock import Mock
    from core import job_timeline
    from modules.notifications import event_dispatcher
    transition(paused, 'resumed', 0)
    observed = []
    def record(pid, kind, **kwargs):
        observed.append(kind)
        transition(paused, kind, 0.1, context=kwargs.get('context', {}), code=kwargs.get('code'))
    monkeypatch.setattr(job_timeline, 'for_printer', record)
    monkeypatch.setattr(job_timeline, 'close_pause_episode', lambda pid: transition(paused, 'resumed', 0.1))
    monkeypatch.setattr(event_dispatcher, 'on_print_start', Mock())
    if brand == 'bambu':
        from modules.printers.monitors.mqtt_printer import PrinterMonitor
        monitor = object.__new__(PrinterMonitor)
        monitor.printer_id, monitor.name, monitor._current_job_id = 1, 'Printer One', 1
        monitor._state = {'stg_cur': 6, 'layer_num': 4, 'gcode_file': 'gear.gcode'}
        monitor._on_state_change('RUNNING', 'PAUSE')
    else:
        from modules.printers.monitors.moonraker_monitor import MoonrakerMonitor
        from modules.printers.adapters.moonraker import MoonrakerStatus
        monitor = MoonrakerMonitor(1, 'Printer One', '127.0.0.1')
        monitor._last_heartbeat = time.time()
        monitor._prev_state = 'RUNNING'
        monitor._prev_filament_detected = True
        monitor._update_progress = Mock()
        monitor._process_status_locked(MoonrakerStatus(internal_state='RUNNING', filament_detected=False, filename='gear.gcode'))
        monitor._process_status_locked(MoonrakerStatus(internal_state='PAUSE', filament_detected=False, filename='gear.gcode'))
    assert observed == ['runout', 'paused']
    assert paused.run(2.1) == [1]
    assert alerts(paused)[-1]['metadata']['cause'] == 'filament'


def test_busy_worker_lock_prevents_another_pass(paused, monkeypatch):
    from contextlib import contextmanager
    @contextmanager
    def busy(name, *, blocking):
        assert name == '.pause-reminders.lock' and blocking is False
        yield False
    monkeypatch.setattr('core.audit_keys.file_lock', busy)
    assert pause_reminders.check(session_factory=paused.factory, now=BASE + timedelta(hours=2)) == []
    assert alerts(paused) == []


def test_education_owned_print_is_suppressed(review_db, monkeypatch):
    from sqlalchemy.orm import sessionmaker
    from core.system_config_values import write_config_text
    now = BASE + timedelta(hours=3)
    review_db.execute(text("UPDATE printers SET gcode_state='PAUSE',last_seen=:now WHERE id=9"), {'now': now})
    review_db.execute(text("INSERT INTO print_jobs(printer_id,scheduled_job_id,status,started_at,filename) "
                           "VALUES(9,13,'running',:at,'gear.gcode')"), {'at': BASE})
    attempt = review_db.execute(text('SELECT economic_uid FROM print_jobs WHERE printer_id=9 ORDER BY id DESC LIMIT 1')).scalar()
    write_config_text(review_db, 'pause_episode:9', json.dumps({'paused': True, 'since': BASE.isoformat(), 'attempt': attempt}))
    review_db.commit()
    monkeypatch.setattr(alert_dispatcher, 'deliver_staged_alert', lambda *_: pytest.fail('Education delivery'))
    assert pause_reminders.run_pass(sessionmaker(bind=review_db.get_bind()), now=now) == []
    assert review_db.execute(text('SELECT count(*) FROM alerts')).scalar() == 0


from tests.test_contracts.test_education_review_workflow import review_db  # noqa: E402,F401


def test_pause_before_a_physical_print_record_exists(paused):
    execute(paused, "UPDATE printers SET gcode_state='RUNNING' WHERE id=1")
    execute(paused, "INSERT INTO printers(id,name,api_type,is_active,gcode_state,last_seen) "
                    "VALUES(2,'Printer Two','bambu',1,'PAUSE',:at)", at=BASE)
    with paused.factory() as db:
        job_faults.stage(db, {**paused.item, 'printer': 2, 'attempt': None, 'context': {'filename': 'early.gcode'}})
        db.commit()
    execute(paused, 'DELETE FROM alerts')
    assert paused.run(2) == [2]
    assert alerts(paused)[0]['title'] == 'Printer Two: early still paused after 2 hours'


def test_repeated_runout_does_not_reset_pause_or_move_its_evidence(paused):
    transition(paused, 'resumed', 1)
    transition(paused, 'runout', 1.1)
    transition(paused, 'paused', 1.2)
    transition(paused, 'runout', 1.3, attempt=None)
    before = len(alerts(paused))
    transition(paused, 'paused', 1.4)
    assert len(alerts(paused)) == before  # the first-pause alert stays once per episode
    assert paused.run(3.2) == [1]
    assert alerts(paused)[-1]['metadata']['cause'] == 'filament'
    assert alerts(paused)[-1]['metadata']['since'] == (BASE + timedelta(hours=1.2)).isoformat()


def test_attention_lane_assigns_identity_to_an_ordinary_print(paused, monkeypatch):
    import core.db
    monkeypatch.setattr(core.db, 'engine', paused.engine)
    monkeypatch.setattr(alert_dispatcher, '_deliver_staged_alert', lambda *_: None)
    transition(paused, 'resumed', 1)
    job_faults._notify(1, 'paused', None, occurred=BASE + timedelta(hours=2))
    with paused.factory() as db:
        assert read_json(db, 'pause_episode:1')['attempt'] == 'print_record:1'
    assert paused.run(4) == [1]
