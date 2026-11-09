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

    def connect(self):
        time.sleep(self.connect_delay)
        if not self.connect_ok:
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
