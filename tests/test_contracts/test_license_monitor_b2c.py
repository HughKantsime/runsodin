"""B2c: monitor processes watch only printers the license lets them monitor."""
import threading
import time

import pytest
from sqlalchemy import event

from core import license_lock as policy
from core import license_monitor
from core.license_monitor import MonitorLicense, drain
from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401
from tests.test_contracts.test_license_policy_b1 import policy_db, pick  # noqa: F401
from tests.test_contracts.test_license_lock_b2b_exempt_attempt import _attempt, _telemetry

NINE, TEN = 9, 10


def test_wanted_is_members_plus_monitoring_exemptions(policy_db):
    view = MonitorLicense(policy_db.get_bind())
    assert view.wanted([NINE, TEN]) == {NINE}
    _attempt(policy_db, NINE)
    _telemetry(policy_db, NINE)
    pick(policy_db, users=[3], printers=[TEN])  # 9 locked mid-print: exempt (M)
    policy._cache.clear()
    assert view.wanted([NINE, TEN]) == {NINE, TEN}


def test_changed_detects_a_selection_change_cheaply(policy_db):
    view = MonitorLicense(policy_db.get_bind())
    view.wanted([NINE, TEN])
    assert view.changed() is False
    pick(policy_db, users=[3], printers=[TEN])
    assert view.changed() is True
    policy._cache.clear()
    assert view.wanted([NINE, TEN]) == {TEN}
    assert view.changed() is False


def test_rollout_off_passes_candidates_without_queries(policy_db, monkeypatch):
    monkeypatch.delenv('ODIN_LICENSE_FLEET_LOCK_ENABLED')
    engine = policy_db.get_bind()
    seen = []
    listener = lambda *a, **k: seen.append(a[2])
    event.listen(engine, 'before_cursor_execute', listener)
    try:
        view = MonitorLicense(engine)
        assert view.wanted([NINE, TEN]) == {NINE, TEN}
        assert view.changed() is False
    finally:
        event.remove(engine, 'before_cursor_execute', listener)
    assert seen == []


def test_unreadable_policy_keeps_the_current_set(policy_db, monkeypatch):
    view = MonitorLicense(policy_db.get_bind())
    monkeypatch.setattr(policy, 'snapshot', lambda db, **k: (_ for _ in ()).throw(RuntimeError('db down')))
    assert view.wanted([NINE, TEN]) is None


class _Thread:
    def __init__(self, pid, name='p', host='h', *, hang=False):
        self.pid, self.hang = pid, hang
        self.stopped = threading.Event()
        self.started = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped.set()

    def join(self, timeout=None):
        if not self.hang:
            return
        time.sleep(timeout or 0)

    def is_alive(self):
        return self.started and (self.hang or not self.stopped.is_set())


def test_drain_uses_one_shared_deadline_for_hung_clients():
    hung = [_Thread(i, hang=True) for i in range(3)]
    for t in hung:
        t.start()
    started = time.monotonic()
    from core.license_monitor import _joiner
    late = drain([t.stop for t in hung], [_joiner(t) for t in hung], seconds=0.5)
    elapsed = time.monotonic() - started
    assert all(t.stopped.is_set() for t in hung)  # every removal signalled before any join
    assert len(late) == 3 and elapsed < 1.0  # not 3 x 0.5 s


def test_anycubic_reconcile_stops_locked_and_starts_selected(policy_db):
    from core.license_monitor import reconcile_threads
    view = MonitorLicense(policy_db.get_bind())
    inventory = {NINE: ('nine', 'h9'), TEN: ('ten', 'h10')}
    threads = {}
    allowed = view.wanted(inventory)
    reconcile_threads(threads, {p: i for p, i in inventory.items() if p in allowed}, _Thread, 'test')
    assert set(threads) == {NINE}
    nine = threads[NINE][1]
    pick(policy_db, users=[3], printers=[TEN])
    assert view.changed()
    policy._cache.clear()
    allowed = view.wanted(inventory)
    started = time.monotonic()
    reconcile_threads(threads, {p: i for p, i in inventory.items() if p in allowed}, _Thread, 'test')
    assert time.monotonic() - started < license_monitor.TEARDOWN_SECONDS
    assert nine.stopped.is_set() and set(threads) == {TEN} and threads[TEN][1].started


# --- M2 Moonraker handle ------------------------------------------------------

class _FakeMoonraker:
    def __init__(self, connect_ok=True, connect_delay=0.0):
        self.connect_ok, self.connect_delay = connect_ok, connect_delay
        self._status_lock = threading.RLock()
        self._retired = False
        self._running = False
        self._thread = None
        self.name = 'mk'
        self.disconnects = 0
        self.printer = type('P', (), {'disconnect': lambda s: None})()

    def connect(self, cancelled=None):
        cancelled = cancelled or (lambda: False)
        with self._status_lock:
            if cancelled():
                return False
            self._retired = False  # as the real connect() does
        time.sleep(self.connect_delay)
        if not self.connect_ok:
            return False
        if cancelled():
            self.disconnects += 1  # the real connect() abandons and disconnects
            self._retired, self._running = True, False
            return False
        self._running = True
        self._thread = threading.Thread(target=self._poll, daemon=True)
        self._thread.start()
        return True

    def _poll(self):
        while self._running:
            time.sleep(0.01)

    def disconnect(self):
        self.disconnects += 1
        self._running = False


def _handle(monkeypatch, fake):
    from modules.printers.monitors import moonraker_monitor
    monkeypatch.setattr(moonraker_monitor, 'MoonrakerMonitor', lambda **k: fake)
    return moonraker_monitor._MoonrakerHandle(1, 'mk', 'h', 80, '')


def test_moonraker_handle_connects_in_background_and_stops(monkeypatch):
    from core.license_monitor import _joiner
    fake = _FakeMoonraker()
    handle = _handle(monkeypatch, fake)
    handle.start()
    deadline = time.monotonic() + 2
    while fake._thread is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert handle.is_alive()
    handle.stop()
    assert fake._retired and not fake._running
    assert _joiner(handle)(1.0) is True and not handle.is_alive()


def test_moonraker_handle_stopped_while_connecting_disconnects(monkeypatch):
    fake = _FakeMoonraker(connect_delay=0.3)
    handle = _handle(monkeypatch, fake)
    handle.start()
    handle.stop()
    handle.join(2)
    assert fake.disconnects == 1 and not handle.is_alive()


def test_moonraker_failed_connect_ends_the_handle_for_a_retry(monkeypatch):
    fake = _FakeMoonraker(connect_ok=False)
    handle = _handle(monkeypatch, fake)
    handle.start()
    handle._connector.join(1)
    assert not handle.is_alive()  # the next inventory pass restarts it



# --- Review round 1 ---------------------------------------------------------------

def test_drain_one_blocking_stop_does_not_delay_the_others():
    from core.license_monitor import drain
    stopped = []
    def blocking():
        time.sleep(2)
    stoppers = [blocking, lambda: stopped.append(1), lambda: stopped.append(2)]
    joiners = [lambda t: False, lambda t: True, lambda t: True]
    started = time.monotonic()
    late = drain(stoppers, joiners, seconds=0.5)
    assert time.monotonic() - started < 1.0
    assert sorted(stopped) == [1, 2] and late == [0]


def test_no_replacement_while_the_old_thread_is_still_stopping():
    from core.license_monitor import reconcile_threads
    threads, lingering = {}, {}
    reconcile_threads(threads, {NINE: ('a',)}, lambda pid, *i: _Thread(pid, hang=True), 'test', None, lingering)
    old = threads[NINE][1]
    import core.license_monitor as lm
    original = lm.TEARDOWN_SECONDS
    try:
        lm.drain.__defaults__ = (0.2,)
        reconcile_threads(threads, {NINE: ('b',)}, _Thread, 'test', None, lingering)  # identity changed
    finally:
        lm.drain.__defaults__ = (original,)
    assert NINE not in threads and lingering[NINE] is old  # fenced: no second session
    old.hang = False
    old.stopped.set()
    reconcile_threads(threads, {NINE: ('b',)}, _Thread, 'test', None, lingering)
    assert NINE in threads and threads[NINE][0] == ('b',) and NINE not in lingering


def test_changed_reports_exemptions_even_with_a_license_deadline(policy_db, monkeypatch):
    from types import SimpleNamespace as NS
    view = MonitorLicense(policy_db.get_bind())
    view.wanted([NINE, TEN])
    stored = policy.stored_snapshot(policy_db)
    with_exempt = NS(revision=stored.revision, printers=stored.printers, entitlement={'deadline': '2099-01-01T00:00:00+00:00'},
                     deadline='2099-01-01T00:00:00+00:00', exempt_attempts={'9': {'kind': 'pending'}})
    view._seen = view._marker(with_exempt)
    monkeypatch.setattr(policy, 'stored_snapshot', lambda conn: with_exempt)
    assert view.changed() is True


def test_monitor_reads_a_fresh_policy_while_an_exemption_exists(policy_db, monkeypatch):
    view = MonitorLicense(policy_db.get_bind())
    calls = []
    real = policy.snapshot
    monkeypatch.setattr(policy, 'snapshot', lambda db, **k: calls.append(k.get('fresh')) or real(db, **k))
    view.wanted([NINE])
    _attempt(policy_db, NINE)
    _telemetry(policy_db, NINE)
    pick(policy_db, users=[3], printers=[TEN])
    policy._cache.clear()
    view.wanted([NINE, TEN])  # sees the exemption
    view.wanted([NINE, TEN])
    assert calls == [False, False, True]


def test_rollout_off_keeps_the_legacy_daemon_behaviour(monkeypatch):
    from core import license_monitor as lm
    monkeypatch.delenv('ODIN_LICENSE_FLEET_LOCK_ENABLED', raising=False)
    stop = threading.Event()
    started = []
    class M:
        def stop(self):
            started.append('stopped')
    def start_all():
        started.append('start')
        stop.set()
        return [M()]
    monkeypatch.setattr(lm, 'monitor_loop', lambda *a, **k: pytest.fail('new loop used with rollout off'))
    lm.run_monitor_process(None, None, None, start_all, 'X', stop)
    assert started == ['start', 'stopped']


# --- Real monitor classes ---------------------------------------------------------

class _FakeMoonrakerPrinter:
    def __init__(self, on_connect=None):
        self.on_connect, self.disconnected = on_connect, 0
    def connect(self):
        if self.on_connect:
            self.on_connect()
        return True
    def disconnect(self):
        self.disconnected += 1
    def get_webcam_urls(self):
        return {}


def _real_moonraker(monkeypatch):
    from modules.printers.monitors import moonraker_monitor
    monitor = moonraker_monitor.MoonrakerMonitor(printer_id=1, name='mk', host='127.0.0.1', port=1, api_key='')
    monkeypatch.setattr(monitor, '_recover_after_reconnect', lambda: None)
    monkeypatch.setattr(monitor, '_discover_and_save_camera', lambda: None)
    return monitor


def test_moonraker_connect_cancelled_before_start_stays_retired(monkeypatch):
    monitor = _real_moonraker(monkeypatch)
    monitor.printer = _FakeMoonrakerPrinter()
    monitor._retired = True
    assert monitor.connect(cancelled=lambda: True) is False
    assert monitor._retired is True and monitor._thread is None


def test_moonraker_connect_cancelled_mid_connect_abandons(monkeypatch):
    monitor = _real_moonraker(monkeypatch)
    cancel = threading.Event()
    monitor.printer = _FakeMoonrakerPrinter(on_connect=cancel.set)  # stop arrives during the transport connect
    assert monitor.connect(cancelled=cancel.is_set) is False
    assert monitor._retired is True and monitor._running is False
    assert monitor.printer.disconnected == 1 and monitor._thread is None


def test_elegoo_stop_during_connect_closes_the_late_socket():
    from modules.printers.monitors.elegoo_monitor import ElegooMonitorThread
    thread = ElegooMonitorThread(printer_id=1, name='el', host='127.0.0.1')
    calls = []
    class Client:
        _connected = False
        def connect(self):
            thread.stop()  # stop lands while the socket is being opened
            self._connected = True
            calls.append('connect')
            return True
        def disconnect(self):
            calls.append('disconnect')
        def on_status(self, cb):
            pass
    thread.client = Client()
    thread._running = True
    thread._retired = False
    thread.run()
    assert calls[0] == 'disconnect' and calls[-1] == 'disconnect' and 'connect' in calls


def test_prusalink_cancelled_client_makes_no_requests(monkeypatch):
    from modules.printers.adapters import prusalink
    client = prusalink.PrusaLinkPrinter(host='127.0.0.1')
    monkeypatch.setattr(prusalink._http, 'get', lambda *a, **k: pytest.fail('HTTP after cancel'))
    client.cancel()
    assert client._get('/api/v1/status') is None
    client.get_status()  # including the legacy fallback requests


def test_bambu_teardown_fence_blocks_reconnect_until_disconnect_finishes(policy_db, monkeypatch):
    from modules.printers.monitors import mqtt_monitor
    import core.license_monitor as lm
    release = threading.Event()
    class Hung:
        def disconnect(self):
            release.wait(5)
    mon = mqtt_monitor.MQTTMonitorDaemon.__new__(mqtt_monitor.MQTTMonitorDaemon)
    monkeypatch.setattr(lm.drain, '__defaults__', (0.2,))
    mon._disconnect_all([('Bambu', Hung(), NINE)])
    assert mon._tearing_down_now(NINE) is True
    mon.monitors = {}
    mon._persist_offline = lambda pid: pytest.fail('OFFLINE written while fenced')
    mon._reconnect_bambu({'id': NINE, 'name': 'n', 'ip': 'i', 'serial': 's', 'access_code': 'a'})
    release.set()
    for _ in range(50):
        if not mon._tearing_down_now(NINE):
            break
        time.sleep(0.02)
    assert mon._tearing_down_now(NINE) is False


_CHILD = r"""
import os, sys, threading, time
from sqlalchemy import create_engine
from core.license_monitor import monitor_loop
log_path, stop_path, url = sys.argv[1], sys.argv[2], sys.argv[3]
def note(line):
    with open(log_path, 'a') as f:
        f.write(f'{time.monotonic():.3f} {line}\n')
class T:
    def __init__(self, pid, name):
        self.pid, self.alive, self.started = pid, False, False
    def start(self):
        self.alive = self.started = True
        note(f'start {self.pid}')
    def stop(self):
        self.alive = False
        note(f'stop {self.pid}')
    def join(self, timeout=None):
        pass
    def is_alive(self):
        return self.alive
stop = threading.Event()
def watch():
    while not os.path.exists(stop_path):
        time.sleep(0.05)
    stop.set()
threading.Thread(target=watch, daemon=True).start()
monitor_loop(create_engine(url), lambda: {9: ('nine',), 10: ('ten',)}, T, 'child', stop)
note('exit')
"""


def test_separate_process_monitor_stops_a_locked_printer_within_the_bound(policy_db, tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path
    log_path, stop_path = tmp_path / 'child.log', tmp_path / 'stop'
    root = Path(__file__).resolve().parents[2]
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(root / 'backend'), str(root)]))
    child = subprocess.Popen([sys.executable, '-c', _CHILD, str(log_path), str(stop_path),
                              str(policy_db.get_bind().url)], env=env, cwd=str(root / 'backend'))

    def lines():
        return log_path.read_text().splitlines() if log_path.exists() else []

    def wait_for(text, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            for line in lines():
                if line.split(' ', 1)[1] == text:
                    return float(line.split(' ', 1)[0])
            time.sleep(0.05)
        return None
    try:
        assert wait_for('start 9', 20) is not None, lines()
        assert all(not l.endswith('start 10') for l in lines())  # 10 is locked
        changed_at = time.monotonic()
        pick(policy_db, users=[3], printers=[TEN])
        stopped = wait_for('stop 9', 10)
        assert stopped is not None, lines()
        assert wait_for('start 10', 10) is not None, lines()
        assert stopped - changed_at <= license_monitor.TEARDOWN_SECONDS, (stopped - changed_at, lines())
    finally:
        stop_path.write_text('stop')
        child.wait(timeout=20)
