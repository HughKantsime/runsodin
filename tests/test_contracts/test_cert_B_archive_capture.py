"""Cert B: terminal print events -> event bus -> real archive persistence.

F02-2.8-07 / F02-2.8-08 / F02-2.8-09. The real InMemoryEventBus delivers
JOB_COMPLETED / JOB_FAILED / JOB_CANCELLED to the real archive subscribers,
which run the real create_print_archive against the bootstrapped SQLite schema.
Only provider delivery (Spoolman HTTP) and startup backlog recovery are
substituted; the subscription and persistence paths are real.
"""
import pytest
from sqlalchemy import text

from tests.test_contracts.test_archive_persistence_accounting_behavior import archive_case  # noqa: F401
from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401


def _bus(archive, monkeypatch):
    from core.event_bus import InMemoryEventBus
    monkeypatch.setattr(archive, 'recover_missing_archives', lambda *a, **k: {'attempted': 0, 'pending_count': 0})
    bus = InMemoryEventBus()
    archive.register_subscribers(bus)
    return bus


@pytest.mark.parametrize('kind,physical,expected', [
    ('JOB_COMPLETED', 'completed', 'completed'),
    ('JOB_FAILED', 'failed', 'failed'),
    ('JOB_CANCELLED', 'cancelled', 'cancelled'),
])
def test_terminal_event_on_bus_persists_one_archive_with_outcome(archive_case, monkeypatch, kind, physical, expected):
    from core import events
    from core.interfaces.event_bus import Event
    archive, engine = archive_case
    with engine.begin() as conn:
        conn.execute(text('UPDATE print_jobs SET status=:s WHERE id=7099'), {'s': physical})
    bus = _bus(archive, monkeypatch)
    payload = {'printer_id': 9, 'print_job_id': 7099, 'success': expected == 'completed', 'status': physical}
    bus.publish(Event(getattr(events, kind), 'notifications', payload))
    # A duplicate delivery of the same terminal attempt never creates a second record.
    bus.publish(Event(getattr(events, kind), 'notifications', payload))
    with engine.connect() as conn:
        rows = conn.execute(text('SELECT status,printer_id FROM print_archives WHERE print_job_id=7099')).all()
    assert [tuple(r) for r in rows] == [(expected, 9)]


def test_archive_subscriptions_are_exactly_the_three_terminal_events(monkeypatch):
    from core import events
    from core.event_bus import InMemoryEventBus
    from modules.archives import archive
    monkeypatch.setattr(archive, 'recover_missing_archives', lambda *a, **k: {'attempted': 0, 'pending_count': 0})
    seen = []
    bus = InMemoryEventBus()
    original = bus.subscribe
    monkeypatch.setattr(bus, 'subscribe', lambda kind, handler: (seen.append(kind), original(kind, handler)))
    archive.register_subscribers(bus)
    assert sorted(seen) == sorted([events.JOB_COMPLETED, events.JOB_FAILED, events.JOB_CANCELLED])


def test_archive_captures_every_advertised_field(archive_case, monkeypatch):
    from core import events
    from core.interfaces.event_bus import Event
    archive, engine = archive_case
    bus = _bus(archive, monkeypatch)
    bus.publish(Event(events.JOB_COMPLETED, 'notifications', {'printer_id': 9, 'print_job_id': 7099, 'success': True}))
    with engine.connect() as conn:
        row = conn.execute(text(
            'SELECT print_name,printer_id,status,started_at,completed_at,actual_duration_seconds,'
            'filament_used_grams,cost_estimate,thumbnail_b64,file_path,print_file_id,plate_count,user_id,job_id '
            'FROM print_archives WHERE print_job_id=7099')).mappings().one()
    assert row['print_name'] == 'Synthetic print'                      # job name
    assert row['printer_id'] == 9                                       # printer
    assert row['status'] == 'completed'                                 # status
    assert str(row['started_at']).startswith('2026-10-11 12:00:00')     # start time
    assert str(row['completed_at']).startswith('2026-10-11 13:00:00')   # end time
    assert row['actual_duration_seconds'] == 3600                      # duration
    assert row['filament_used_grams'] == 80                             # filament used
    assert row['cost_estimate'] == 2.5                                  # cost estimate
    assert row['thumbnail_b64'] == 'synthetic-thumbnail'                # thumbnail
    assert row['file_path'] == '/opaque/archive-synthetic.3mf'          # file path
    assert row['print_file_id'] == 7099                                 # print file ID
    assert row['plate_count'] == 2                                      # plate count
    assert row['user_id'] == 3                                          # user attribution
    assert row['job_id'] == 7099


def test_monitor_cancellation_path_reaches_archive_through_bus(archive_case, monkeypatch):
    """job_events.job_cancelled (the monitor-facing API) publishes JOB_CANCELLED -> archive."""
    from core import event_bus as bus_module
    from modules.notifications import job_events
    archive, engine = archive_case
    bus = _bus(archive, monkeypatch)
    monkeypatch.setattr(bus_module, 'get_event_bus', lambda: bus)
    monkeypatch.setattr(job_events, 'get_event_bus', lambda: bus, raising=False)
    monkeypatch.setattr(job_events, 'dispatch_alert', lambda **k: None, raising=False)
    from contextlib import contextmanager
    @contextmanager
    def database():
        conn = engine.raw_connection()
        try:
            yield conn
        finally:
            conn.close()
    monkeypatch.setattr(job_events, 'get_db', database, raising=False)
    with engine.begin() as conn:
        conn.execute(text("UPDATE print_jobs SET status='running',ended_at=NULL WHERE id=7099"))
    job_events.job_cancelled(9, 7099)
    with engine.connect() as conn:
        assert conn.execute(text('SELECT status FROM print_jobs WHERE id=7099')).scalar_one() == 'cancelled'
        assert conn.execute(text('SELECT status FROM print_archives WHERE print_job_id=7099')).scalars().all() == ['cancelled']
