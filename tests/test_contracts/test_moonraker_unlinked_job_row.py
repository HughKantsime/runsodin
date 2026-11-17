"""Hand-started prints acquire one tenant-owned history job at their observed end."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import text

from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_education_monitor_policy import monitor_db
from tests.test_contracts.test_smart_plug_auto_power_behavior import auto_power
from tests.test_contracts.test_automatic_power_terminal_delivery import terminal_environment
from tests.test_contracts.test_product_bom_integrity import bom_client
from modules.archives.archive import create_print_archive as real_create_print_archive
from modules.notifications.alert_dispatch import dispatch_alert as real_dispatch_alert


@pytest.fixture
def hand_print(terminal_environment, monkeypatch):
    from core.db_utils import _ConnectionAdapter
    from modules.notifications import job_events, event_dispatcher, alert_dispatch
    from modules.archives import archive
    engine, bus, timers, calls, mqtt, moon = terminal_environment

    @contextmanager
    def database():
        raw = engine.raw_connection()
        try:
            yield _ConnectionAdapter(raw, False)
        finally:
            raw.close()

    monkeypatch.setattr(job_events, 'get_db', database)
    alerts, archives, care = Mock(), Mock(), Mock()
    monkeypatch.setattr(archive, 'create_print_archive', archives)
    monkeypatch.setattr(archive, 'get_db', database)
    monkeypatch.setattr(archive, '_deliver_outbox_in_background', Mock())
    for module in (job_events, event_dispatcher, alert_dispatch):
        monkeypatch.setattr(module, 'dispatch_alert', alerts)
    monkeypatch.setattr(job_events, 'record_error', Mock())
    monkeypatch.setattr(job_events, 'increment_care_counters', care)
    monkeypatch.setattr(event_dispatcher, 'increment_care_counters', care)
    monkeypatch.setattr(job_events, '_start_bed_cooled_monitor', Mock())
    archive.register_subscribers(bus)
    events = []
    from core import events as ev
    for topic in (ev.JOB_COMPLETED, ev.JOB_FAILED, ev.JOB_CANCELLED):
        bus.subscribe(topic, events.append)
    start = (datetime.now(timezone.utc) - timedelta(hours=1)).replace(tzinfo=None, microsecond=0)
    with engine.begin() as conn:
        conn.execute(text('UPDATE print_jobs SET scheduled_job_id=NULL,started_at=:start WHERE id=553'), {'start': start})
        conn.execute(text('DELETE FROM jobs WHERE id=552'))

    def end(source, status, identifier=553):
        if source == 'bambu':
            mqtt.record_job_ended(551, 'Owned terminal', identifier, None, status,
                                  {'subtask_name': 'owned.gcode'}, {}, Mock())
        elif source in ('moonraker', 'kobra'):
            monitor = moon.MoonrakerMonitor(551, 'Owned terminal', '127.0.0.1')
            monitor.printer = SimpleNamespace(is_kobra_s1=source == 'kobra')
            monitor._current_job_db_id = identifier
            monitor._last_filename = 'owned.gcode'
            monitor._job_ended(status, SimpleNamespace(filename='owned.gcode', filament_used_mm=0, error_message=None))
        elif status == 'cancelled':
            job_events.job_cancelled(551, identifier)
        else:
            job_events.job_completed(551, identifier, success=status == 'completed', duration_seconds=3600)

    return SimpleNamespace(engine=engine, end=end, start=start, alerts=alerts,
                           archives=archives, care=care, events=events)


@pytest.mark.parametrize('source', ['moonraker', 'kobra', 'generic', 'bambu'])
@pytest.mark.parametrize('status', ['completed', 'failed', 'cancelled'])
def test_hand_started_end_links_once_with_times_and_existing_effects(hand_print, source, status):
    from core.db_compat import as_utc_datetime
    from core.print_attempts import bound_print_attempt, sqlalchemy_runner
    case = hand_print
    with case.engine.begin() as conn:
        conn.execute(text('UPDATE printers SET api_type=:type WHERE id=551'),
                     {'type': 'prusalink' if source == 'generic' else 'bambu' if source == 'bambu' else 'moonraker'})
    case.end(source, status)
    case.end(source, status)
    with case.engine.connect() as conn:
        row = conn.execute(text('SELECT scheduled_job_id,status,started_at,ended_at FROM print_jobs WHERE id=553')).one()
        job = conn.execute(text('SELECT * FROM jobs WHERE id=:id'), {'id': row[0]}).mappings().one()
        assert conn.execute(text('SELECT count(*) FROM jobs WHERE printer_id=551')).scalar_one() == 1
        assert job['status'] == row[1] == status
        assert as_utc_datetime(job['actual_start']) == as_utc_datetime(row[2]) == as_utc_datetime(case.start)
        assert as_utc_datetime(job['actual_end']) == as_utc_datetime(row[3])
        assert job['duration_hours'] == pytest.approx(1, abs=.002)
        assert job['charged_to_org_id'] == 1
        assert (job['quantity'], job['quantity_on_bed'], job['hold'], job['is_locked']) == (1, 1, 0, 0)
        assert bound_print_attempt(sqlalchemy_runner(conn), 553, job['id']) == 0
    assert case.alerts.call_count == 1
    # Generic archives use event subscribers; other monitors archive directly.
    assert case.archives.call_count == (2 if status == 'completed' and source != 'generic' else 1)
    assert case.care.call_count == (1 if status == 'completed' else 0)
    assert len(case.events) == (1 if source == 'generic' or status == 'completed' else 0)


@pytest.mark.parametrize('source', ['moonraker', 'generic', 'bambu'])
@pytest.mark.parametrize('kind', ['reserved', 'dispatched', 'unknown', 'midprint'])
def test_existing_link_and_bambu_skip_rules(hand_print, source, kind):
    case = hand_print
    with case.engine.begin() as conn:
        if kind == 'reserved':
            conn.execute(text('UPDATE print_jobs SET filename=:name,job_name=:name WHERE id=553'),
                         {'name': 'odin-' + 'a' * 32 + '.gcode'})
        elif kind == 'dispatched':
            conn.execute(text("INSERT INTO jobs(id,item_name,status,printer_id,charged_to_org_id) VALUES(552,'Dispatched','printing',551,1)"))
            conn.execute(text('UPDATE print_jobs SET scheduled_job_id=552 WHERE id=553'))
        elif kind == 'unknown':
            from core.physical_print import CLOSED_UNKNOWN
            conn.execute(text("UPDATE print_jobs SET status='cancelled',ended_at=NULL,error_code=:reason WHERE id=553"), {'reason': CLOSED_UNKNOWN})
        else:
            conn.execute(text("UPDATE print_jobs SET error_code='First seen mid-print; full duration and filament use are unverified.' WHERE id=553"))
    case.end(source, 'completed')
    with case.engine.connect() as conn:
        linked = conn.execute(text('SELECT scheduled_job_id FROM print_jobs WHERE id=553')).scalar_one()
        count = conn.execute(text('SELECT count(*) FROM jobs WHERE printer_id=551')).scalar_one()
    assert count == (1 if kind in ('dispatched', 'midprint') else 0)
    assert (linked == 552) if kind == 'dispatched' else (linked is not None) == (kind == 'midprint')


@pytest.mark.parametrize('source', ['moonraker', 'generic', 'bambu'])
def test_job_creation_failure_rolls_back_the_print_end(hand_print, monkeypatch, source):
    from core import observed_jobs
    def fail(*args, **kwargs):
        raise RuntimeError('injected job insert failure')
    monkeypatch.setattr(observed_jobs, 'insert_job', fail)
    hand_print.end(source, 'completed')
    with hand_print.engine.connect() as conn:
        assert conn.execute(text('SELECT status,ended_at,scheduled_job_id FROM print_jobs WHERE id=553')).one() == ('running', None, None)
    assert hand_print.alerts.call_count == hand_print.archives.call_count == 0
    assert hand_print.events == []


@pytest.mark.parametrize('source', ['moonraker', 'generic', 'bambu'])
@pytest.mark.parametrize('org', [1, None])
def test_list_detail_and_analytics_use_printer_organization(hand_print, bom_client, monkeypatch, source, org, pro_license):
    from core.dependencies import get_current_user
    from modules.jobs.routes import jobs_crud
    from modules.reporting.routes import analytics
    for router in (jobs_crud.router, analytics.router):
        bom_client.app.include_router(router)
    actor = {'id': 3, 'role': 'operator', 'group_id': 1, 'is_active': True}
    bom_client.app.dependency_overrides[get_current_user] = lambda: actor
    with hand_print.engine.begin() as conn:
        conn.execute(text('UPDATE printers SET org_id=:org WHERE id=551'), {'org': org})
    before = bom_client.get('/analytics').json()['summary']['completed_jobs']
    hand_print.end(source, 'completed')
    with hand_print.engine.connect() as conn:
        job_id = conn.execute(text('SELECT scheduled_job_id FROM print_jobs WHERE id=553')).scalar_one()
        assert conn.execute(text('SELECT charged_to_org_id FROM jobs WHERE id=:id'), {'id': job_id}).scalar_one() == org
    for group in (1, 2):
        actor['group_id'] = group
        visible = group == 1 or org is None
        response = bom_client.get('/jobs')
        assert response.status_code == 200, response.text
        rows = response.json()
        if isinstance(rows, dict):
            rows = rows['items']
        assert (job_id in [row['id'] for row in rows]) == visible
        assert bom_client.get(f'/jobs/{job_id}').status_code == (200 if visible else 404)
        summary = bom_client.get('/analytics').json()['summary']
        assert summary['completed_jobs'] == (before + 1 if visible else 0)


def test_same_prints_keep_stats_maintenance_and_feed_totals(bom_client, review_db, pro_license):
    from core.observed_jobs import link_ended_print
    from modules.reporting.routes import analytics
    from modules.system.maintenance_status import failure_rates
    from modules.printers.integration_floor import day_totals
    bom_client.app.include_router(analytics.router)
    now = datetime.now(timezone.utc)
    start, end = (now - timedelta(hours=1)).replace(tzinfo=None), now.replace(tzinfo=None)
    for status in ('completed', 'failed', 'cancelled'):
        review_db.execute(text("INSERT INTO print_jobs(printer_id,job_name,status,started_at,ended_at) VALUES(9,'hand.gcode',:status,:start,:end)"),
                          {'status': status, 'start': start, 'end': end})
    review_db.commit()
    actor = {'id': 3, 'role': 'operator', 'group_id': 1}
    def numbers():
        response = bom_client.get('/stats', params={'days': 7})
        assert response.status_code == 200, response.text
        return (response.json(), failure_rates(review_db, [9], now),
                day_totals(review_db, actor, 1, False, [9], now, timezone.utc))
    before = numbers()
    old_analytics = bom_client.get('/analytics').json()['summary']
    ids = review_db.execute(text("SELECT id FROM print_jobs WHERE job_name='hand.gcode'")).scalars().all()
    for identifier in ids:
        job = link_ended_print(review_db.connection(), identifier)
        assert link_ended_print(review_db.connection(), identifier) == job
    review_db.commit()
    assert numbers() == before
    new_analytics = bom_client.get('/analytics').json()['summary']
    # Analytics has always been job-only: adding the missing jobs must add them once.
    assert new_analytics['total_jobs'] == old_analytics['total_jobs'] + 3
    assert new_analytics['completed_jobs'] == old_analytics['completed_jobs'] + 1
    assert new_analytics['total_print_hours'] == old_analytics['total_print_hours'] + 1


@pytest.mark.parametrize('source', ['moonraker', 'generic', 'bambu'])
@pytest.mark.parametrize('status', ['completed', 'failed', 'cancelled'])
def test_real_archive_is_single_and_missing_material_evidence_is_not_guessed(hand_print, monkeypatch, source, status):
    from modules.archives import archive
    from modules.archives.attempt_accounting import capture_attempt_spools
    monkeypatch.setattr(archive, 'create_print_archive', real_create_print_archive)
    with hand_print.engine.begin() as conn:
        assert capture_attempt_spools(conn, 553, 551, source='contract-hand-start')
    hand_print.end(source, status)
    hand_print.end(source, status)
    with hand_print.engine.connect() as conn:
        row = conn.execute(text('SELECT job_id,status,accounting_state FROM print_archives WHERE print_job_id=553')).one()
        linked = conn.execute(text('SELECT scheduled_job_id FROM print_jobs WHERE id=553')).scalar_one()
        assert row == (linked, status, 'unresolved')
        assert conn.execute(text('SELECT count(*) FROM spool_usage WHERE print_job_id=553')).scalar_one() == 0


def test_shared_helper_accepts_qmark_cursor_and_identity_survives_deletion(review_db):
    from core.observed_jobs import link_ended_print
    from core.db_utils import _ConnectionAdapter
    review_db.execute(text("INSERT INTO print_jobs(id,printer_id,job_name,status,started_at,ended_at) VALUES(8001,9,'hand.gcode','completed','2026-11-16 12:00:00','2026-11-16 13:00:00')"))
    review_db.commit()
    raw = review_db.get_bind().raw_connection()
    try:
        cursor = _ConnectionAdapter(raw, False).cursor()
        first = link_ended_print(cursor, 8001)
        cursor.execute('UPDATE print_jobs SET scheduled_job_id=NULL WHERE id=8001')
        cursor.execute('DELETE FROM jobs WHERE id=?', (first,))
        second = link_ended_print(cursor, 8001)
        assert second > first
        raw.rollback()
    finally:
        raw.close()


@pytest.mark.parametrize('status', ['completed', 'failed', 'cancelled'])
@pytest.mark.parametrize('link_job', [False, True])
def test_single_slot_keeps_existing_ownership_and_filament_behavior(hand_print, monkeypatch, status, link_job):
    from modules.archives import archive
    from modules.archives.attempt_accounting import capture_attempt_spools
    if not link_job:
        from core import observed_jobs
        monkeypatch.setattr(observed_jobs, 'link_ended_print', lambda *args, **kwargs: None)
    monkeypatch.setattr(archive, 'create_print_archive', real_create_print_archive)
    with hand_print.engine.begin() as conn:
        conn.execute(text("UPDATE printers SET api_type='moonraker',slot_count=1 WHERE id=551"))
        conn.execute(text("INSERT INTO filament_library(id,brand,name) VALUES(9001,'Fixture','PLA')"))
        conn.execute(text('INSERT INTO spools(id,filament_id,remaining_weight_g,org_id) VALUES(9001,9001,1000,1)'))
        conn.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type,assigned_spool_id) VALUES(551,1,'PLA',9001)"))
        assert capture_attempt_spools(conn, 553, 551, source='contract-single-slot')
        snapshot = conn.execute(text('SELECT spool_snapshot FROM print_jobs WHERE id=553')).scalar_one()
    # The generic end preserves recorded measured usage; no new measurement is invented.
    with hand_print.engine.begin() as conn:
        conn.execute(text('UPDATE print_jobs SET filament_used_g=12 WHERE id=553'))
    hand_print.end('generic', status)
    hand_print.end('generic', status)
    with hand_print.engine.connect() as conn:
        assert conn.execute(text('SELECT spool_snapshot FROM print_jobs WHERE id=553')).scalar_one() == snapshot
        accounting = conn.execute(text('SELECT accounting_state,accounting_reason FROM print_archives WHERE print_job_id=553')).one()
        assert accounting == ('unresolved', 'archive_ownership_unresolved')
        assert conn.execute(text('SELECT count(*) FROM spool_usage WHERE print_job_id=553')).scalar_one() == 0
        assert conn.execute(text('SELECT remaining_weight_g FROM spools WHERE id=9001')).scalar_one() == 1000


@pytest.mark.parametrize('source', ['moonraker', 'generic', 'bambu'])
@pytest.mark.parametrize('status', ['completed', 'failed', 'cancelled'])
def test_fresh_attempt_delivers_one_alert_webhook_and_archive(hand_print, monkeypatch, source, status):
    from core.attempt_start_observation import observe_start
    from core.registry import registry
    from modules.archives import archive
    from modules.notifications import job_events, event_dispatcher, alert_dispatch, alert_dispatcher, channels, quiet_hours, alert_outbox
    from modules.organizations.services import OrgSettingsService
    monkeypatch.setitem(registry._providers, 'OrgSettingsProvider', OrgSettingsService())
    monkeypatch.setattr(archive, 'create_print_archive', real_create_print_archive)
    for module in (job_events, event_dispatcher, alert_dispatch):
        monkeypatch.setattr(module, 'dispatch_alert', real_dispatch_alert)
    monkeypatch.setattr(alert_dispatch, 'engine', hand_print.engine)
    monkeypatch.setattr(quiet_hours, 'should_suppress_notification', lambda **kwargs: False)
    monkeypatch.setattr(alert_dispatcher, 'should_suppress_notification', lambda **kwargs: False)
    webhook = Mock()
    monkeypatch.setattr(alert_dispatcher, '_send_org_webhook', webhook)
    monkeypatch.setattr(channels, 'send_webhook', Mock())
    monkeypatch.setattr(channels, 'send_email', Mock())
    monkeypatch.setattr(channels, 'send_push_notification', Mock())
    monkeypatch.setattr(alert_dispatcher, '_deliver_email', Mock())
    monkeypatch.setattr(alert_dispatcher, '_deliver_browser_push', Mock())
    monkeypatch.setattr(alert_outbox, 'deliver_mobile', Mock())
    with hand_print.engine.begin() as conn:
        conn.execute(text('DELETE FROM print_jobs WHERE id=553'))
        conn.execute(text('UPDATE users SET is_active=FALSE WHERE id<>3'))
        conn.execute(text('UPDATE groups SET settings_json=:settings WHERE id=1'),
                     {'settings': '{"webhook_url":"https://fixture.example.invalid/hook"}'})
        observed = observe_start(conn, printer_id=551, protocol='moonraker',
                                 filename='owned.gcode', job_name='Owned terminal',
                                 physical_values={'started_at': hand_print.start}, source='contract-hand-start')
        identifier = observed.id
        # Unlinked starts deliberately have no economic identity; use the actual
        # legacy alert path, without manufacturing a dispatched print's identity.
        assert conn.execute(text('SELECT economic_uid FROM print_jobs WHERE id=:id'), {'id': identifier}).scalar_one() is None
    hand_print.end(source, status, identifier)
    hand_print.end(source, status, identifier)
    with hand_print.engine.connect() as conn:
        linked = conn.execute(text('SELECT scheduled_job_id FROM print_jobs WHERE id=:id'), {'id': identifier}).scalar_one()
        assert linked is not None
        assert conn.execute(text('SELECT user_id FROM alerts WHERE job_id=:job'), {'job': linked}).all() == [(3,)]
        assert conn.execute(text('SELECT count(*) FROM print_archives WHERE print_job_id=:id'), {'id': identifier}).scalar_one() == 1
        assert conn.execute(text('SELECT count(*) FROM job_summary_outbox WHERE job_id=:job'), {'job': linked}).scalar_one() == 0
    assert webhook.call_count == 1
