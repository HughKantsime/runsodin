"""Actual session deadline accounting with controlled clock and adapter."""
from types import SimpleNamespace
import pytest


@pytest.mark.parametrize('startup,connection', [(0,4.5),(2,2),(0,5)])
def test_status_waits_share_one_total_budget(monkeypatch, startup, connection):
    from modules.printers.telemetry.bambu import session
    clock = [100.0]
    waits, cleanup = [], []
    class Event:
        def __init__(self): self.index = len(waits); waits.append([])
        def set(self): pass
        def wait(self, timeout):
            waits[self.index].append(timeout)
            clock[0] += min(connection, timeout) if self.index == 0 else timeout
            return self.index == 0
    class Adapter:
        def __init__(self, *args, **kwargs): pass
        def start(self): clock[0] += startup
        def status(self): return object()
        def stop(self): cleanup.append('stop')
    monkeypatch.setattr(session, 'threading', SimpleNamespace(Event=Event, TIMEOUT_MAX=session.threading.TIMEOUT_MAX))
    monkeypatch.setattr(session.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(session, 'BambuTelemetryAdapter', Adapter)
    monkeypatch.setattr(session, 'BambuV2StatusView', lambda value: value)
    result = session.read_status_once(object(), timeout=5)
    assert result.success and result.section is None
    assert clock[0] <= 105
    assert cleanup == ['stop']


def test_start_failure_sanitized_and_adapter_stopped(monkeypatch):
    from modules.printers.telemetry.bambu import session
    cleanup = []
    class Adapter:
        def __init__(self, *args, **kwargs): pass
        def start(self): raise RuntimeError('synthetic private marker')
        def stop(self): cleanup.append('stop')
    monkeypatch.setattr(session, 'BambuTelemetryAdapter', Adapter)
    result = session.read_status_once(object(), timeout=5)
    assert not result.success
    assert 'synthetic private marker' not in result.error
    assert cleanup == ['stop']


@pytest.mark.parametrize('timeout', [0, -1, True, None, float('nan'), float('inf'), '5', 10**1000, 1e100])
def test_invalid_timeout_refused_before_adapter_creation(monkeypatch, timeout):
    from modules.printers.telemetry.bambu import session
    def forbidden(*args, **kwargs):
        raise AssertionError('Invalid timeout must not acquire an adapter')
    monkeypatch.setattr(session, 'BambuTelemetryAdapter', forbidden)
    result = session.read_status_once(object(), timeout=timeout)
    assert not result.success
    assert result.error == 'Invalid status timeout'
