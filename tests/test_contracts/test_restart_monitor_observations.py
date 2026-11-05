"""Cross-process evidence expiry and throttled explicit-state polling writes."""
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text

from core import monitor_observations as evidence
from modules.printers.monitors.live_state import observe_status


@pytest.fixture
def store(monkeypatch):
    engine = create_engine('sqlite://')
    with engine.begin() as conn:
        conn.execute(text('CREATE TABLE system_config(key TEXT PRIMARY KEY,value TEXT)'))
    clock = [100.0]
    monkeypatch.setattr(evidence, 'time', SimpleNamespace(time=lambda: clock[0]))
    yield engine, clock
    engine.dispose()


def test_postboot_and_expiry_required_even_when_a_row_exists(store):
    engine, clock = store
    with engine.begin() as conn:
        evidence.persist(conn, 9, 'RUNNING')
        assert evidence.live_state(conn, 9, 101) is None
        assert evidence.live_state(conn, 9, 99) == 'RUNNING'
        clock[0] += 61
        assert evidence.live_state(conn, 9, 99) is None


def test_retained_or_old_report_cannot_refresh_state(store):
    engine, clock = store
    with engine.begin() as conn:
        evidence.persist(conn, 9, 'RUNNING')
        clock[0] = 161
        evidence.persist(conn, 9, 'RUNNING', retained=True)
        evidence.persist(conn, 9, 'RUNNING', report_age_seconds=61)
        evidence.persist(conn, 9, None)
        assert evidence.live_state(conn, 9, 99) is None


@pytest.mark.parametrize('protocol,raw', [
    ('moonraker', {'print_stats': {'state': 'printing'}}),
    ('prusalink', {'printer': {'state': 'PRINTING'}}),
    ('prusalink', {'state': {'flags': {'printing': True}}}),
    ('elegoo', {'Status': {'PrintInfo': {'Status': 8}}}),
    ('elegoo', {'Data': {'Status': {'CurrentStatus': [1]}}}),
])
def test_explicit_monitor_state_is_shared_but_partial_reports_do_not_refresh(store, protocol, raw):
    engine, clock = store
    observe_status(engine, 9, SimpleNamespace(raw_data=raw, internal_state='PRINTING'), protocol)
    with engine.connect() as conn:
        assert evidence.live_state(conn, 9, 99) == 'PRINTING'
    clock[0] = 161
    observe_status(engine, 9, SimpleNamespace(raw_data={}, internal_state='PRINTING'), protocol)
    with engine.connect() as conn:
        assert evidence.live_state(conn, 9, 99) is None


def test_invalid_cache_is_not_authority(store):
    engine, _ = store
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO system_config VALUES('printer.live_state.9','not-json')"))
        assert evidence.live_state(conn, 9, 99) is None


@pytest.mark.parametrize('protocol', ['moonraker', 'prusalink', 'elegoo'])
def test_malformed_partial_packet_does_not_interrupt_lifecycle(store, protocol):
    engine, _ = store
    raw = {'Status': [], 'Data': None, 'printer': None, 'state': 'unknown', 'print_stats': []}
    observe_status(engine, 9, SimpleNamespace(raw_data=raw, internal_state='PRINTING'), protocol)
    with engine.connect() as conn:
        assert evidence.live_state(conn, 9, 99) is None


@pytest.mark.parametrize('dbapi', [False, True])
def test_publisher_throttles_per_printer_but_state_changes_write_immediately(store, dbapi):
    from contextlib import contextmanager
    engine, clock = store
    writes = []
    @contextmanager
    def connect():
        writes.append(clock[0])
        if dbapi:
            conn = engine.raw_connection()
            try:
                yield conn
            finally:
                conn.close()
        else:
            with engine.begin() as conn:
                yield conn
    first, second = evidence.ObservationPublisher(), evidence.ObservationPublisher()
    for tick in range(61):
        clock[0] = 100 + tick
        first.publish(connect, 9, 'RUNNING', dbapi=dbapi)
    assert writes == [100, 115, 130, 145, 160]
    clock[0] = 161
    second.publish(connect, 10, 'RUNNING', dbapi=dbapi)
    first.publish(connect, 9, 'PAUSE', dbapi=dbapi)
    first.publish(connect, 9, 'pause', dbapi=dbapi)
    assert writes == [100, 115, 130, 145, 160, 161, 161]
    with engine.connect() as conn:
        assert evidence.live_state(conn, 9, 99) == 'PAUSE'
        assert evidence.live_state(conn, 10, 99) == 'RUNNING'


@pytest.mark.parametrize('dbapi', [False, True])
def test_failed_commit_does_not_throttle_retry(store, dbapi):
    from contextlib import contextmanager
    engine, _ = store
    publisher = evidence.ObservationPublisher()
    @contextmanager
    def fail_commit():
        with engine.begin() as conn:
            class FailedCommit:
                def execute(self, *args):
                    return conn.connection.execute(*args)
                def commit(self):
                    raise RuntimeError('synthetic commit failure')
            yield FailedCommit() if dbapi else conn
            raise RuntimeError('synthetic commit failure')
    with pytest.raises(RuntimeError, match='synthetic commit failure'):
        publisher.publish(fail_commit, 9, 'RUNNING', dbapi=dbapi)
    with engine.connect() as conn:
        assert evidence.live_state(conn, 9, 99) is None
    publisher.publish(engine.begin, 9, 'RUNNING')
    with engine.connect() as conn:
        assert evidence.live_state(conn, 9, 99) == 'RUNNING'


@pytest.mark.parametrize('protocol,raw', [
    ('moonraker', {'print_stats': {'state': 'printing'}}),
    ('prusalink', {'printer': {'state': 'PRINTING'}}),
    ('elegoo', {'Status': {'CurrentStatus': [1]}}),
])
def test_polling_monitor_throttle_does_not_delay_state_change(store, protocol, raw):
    import json
    engine, clock = store
    owner = SimpleNamespace()
    status = SimpleNamespace(raw_data=raw, internal_state='PRINTING')
    for tick in range(30):
        clock[0] = 100 + tick
        observe_status(engine, 9, status, protocol, owner=owner)
    with engine.connect() as conn:
        cached = json.loads(conn.execute(text('SELECT value FROM system_config')).scalar_one())
        assert cached['at'] == 115
    status.internal_state = 'PAUSE'
    if protocol == 'moonraker':
        raw['print_stats']['state'] = 'paused'
    observe_status(engine, 9, status, protocol, owner=owner)
    with engine.connect() as conn:
        assert evidence.live_state(conn, 9, 99) in ('PAUSE', 'PAUSED')
