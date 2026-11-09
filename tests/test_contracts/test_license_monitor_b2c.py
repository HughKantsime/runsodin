"""B2c: monitor processes retire a printer by restarting (exit 75), never in-process.

Rollout on: a process starts sessions only for printers the license lets it monitor;
a session's printer losing that permission, an unreadable policy for more than 5 s
while sessions exist, or a session's printer removed, deactivated or reconfigured
ends the process with exit 75 (supervisord restarts it). Rollout off: the legacy
daemons, with no policy query.
"""
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from sqlalchemy import event, text

from core import license_lock as policy
from core import license_monitor
from core.license_monitor import MonitorLicense, Retirement
from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401
from tests.test_contracts.test_license_policy_b1 import policy_db, pick  # noqa: F401
from tests.test_contracts.test_license_lock_b2b_exempt_attempt import _attempt, _telemetry

NINE, TEN = 9, 10
ROOT = Path(__file__).resolve().parents[2]
CHILD = Path(__file__).with_name('_license_monitor_child.py')


# --- Policy view -------------------------------------------------------------------

def test_wanted_is_members_plus_monitoring_exemptions(policy_db):
    view = MonitorLicense(policy_db.get_bind())
    assert view.wanted([NINE, TEN]) == {NINE}
    _attempt(policy_db, NINE)
    _telemetry(policy_db, NINE)
    pick(policy_db, users=[3], printers=[TEN])  # 9 locked mid-print: exempt (M)
    policy._cache.clear()
    assert view.wanted([NINE, TEN]) == {NINE, TEN}


def test_rollout_off_passes_candidates_without_queries(policy_db, monkeypatch):
    monkeypatch.delenv('ODIN_LICENSE_FLEET_LOCK_ENABLED')
    engine = policy_db.get_bind()
    seen = []
    listener = lambda *a, **k: seen.append(a[2])
    event.listen(engine, 'before_cursor_execute', listener)
    try:
        assert MonitorLicense(engine).wanted([NINE, TEN]) == {NINE, TEN}
    finally:
        event.remove(engine, 'before_cursor_execute', listener)
    assert seen == []


def test_unreadable_policy_is_none(policy_db, monkeypatch):
    view = MonitorLicense(policy_db.get_bind())
    monkeypatch.setattr(policy, 'snapshot', lambda db, **k: (_ for _ in ()).throw(RuntimeError('db down')))
    assert view.wanted([NINE, TEN]) is None
    assert view.policy() is None


def test_policy_read_is_bounded_and_never_stacks(policy_db, monkeypatch):
    release = threading.Event()
    calls = []

    def hang(db, **k):
        calls.append(1)
        release.wait(10)
        raise RuntimeError('late')
    monkeypatch.setattr(policy, 'snapshot', hang)
    view = MonitorLicense(policy_db.get_bind())
    started = time.monotonic()
    assert view.policy(timeout=0.3) is None
    assert time.monotonic() - started < 1.0
    assert view.policy(timeout=0.3) is None and len(calls) == 1  # the hung read is not stacked
    monkeypatch.setattr(license_monitor, 'POLICY_ABANDON_SECONDS', 0.5)
    time.sleep(0.6)
    assert view.policy(timeout=0.3) is None and len(calls) == 2  # a stuck read is abandoned
    release.set()


def test_policy_reads_use_their_own_short_busy_timeout(policy_db):
    view = MonitorLicense(policy_db.get_bind())
    assert view.policy() is not None
    bounded = view._policy_engine()
    assert bounded is not policy_db.get_bind()
    with bounded.connect() as conn:
        assert conn.exec_driver_sql('PRAGMA busy_timeout').scalar() == license_monitor.POLICY_BUSY_TIMEOUT_MS


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


def test_fingerprint_is_connection_settings_only():
    fp = license_monitor.connection_fingerprint
    assert fp('moonraker', 'h', 80, 'k') == fp('moonraker', 'h', 80, 'k')
    assert fp('moonraker', 'h', 80, 'k') != fp('moonraker', 'h', 81, 'k')
    assert fp('moonraker', 'h', 80, 'k') != fp('moonraker', 'h', 80, 'other')


def test_inventories_ignore_names_and_telemetry(policy_db, monkeypatch):
    from modules.printers.monitors import moonraker_monitor
    policy_db.execute(text("UPDATE printers SET api_type='moonraker', api_host='10.0.0.9:7125' WHERE id=9"))
    policy_db.commit()
    before = moonraker_monitor._inventory()[NINE][0]
    policy_db.execute(text("UPDATE printers SET name='renamed', gcode_state='RUNNING', last_seen=CURRENT_TIMESTAMP, "
                           "camera_url='rtsp://x' WHERE id=9"))
    policy_db.commit()
    assert moonraker_monitor._inventory()[NINE][0] == before
    policy_db.execute(text("UPDATE printers SET api_host='10.0.0.9:7126' WHERE id=9"))
    policy_db.commit()
    assert moonraker_monitor._inventory()[NINE][0] != before


# --- Retirement (in process; exit recorded instead of taken) ------------------------

def _retirement(policy_db, exits):
    def record(code):
        exits.append(code)
    return Retirement(policy_db.get_bind(), 'test', exit=record)


def test_license_pass_exits_75_only_for_a_session_that_lost_monitoring(policy_db):
    exits = []
    retirement = _retirement(policy_db, exits)
    retirement.add(NINE, 'fp')
    assert retirement.license_pass() is not None and exits == []
    pick(policy_db, users=[3], printers=[TEN])
    retirement.license_pass()
    assert exits == [75]


def test_locked_printer_without_a_session_never_exits(policy_db):
    exits = []
    retirement = _retirement(policy_db, exits)
    pick(policy_db, users=[3], printers=[TEN])
    retirement.license_pass()
    assert exits == []


def test_unreadable_policy_exits_only_with_sessions_after_five_seconds(policy_db, monkeypatch):
    exits = []
    retirement = _retirement(policy_db, exits)
    clock = [1000.0]
    monkeypatch.setattr(license_monitor.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(retirement.license, 'policy', lambda: None)
    for _ in range(10):
        retirement.license_pass()
        clock[0] += 2
    assert exits == []  # no session: start nothing, never exit
    retirement.add(NINE, 'fp')
    for _ in range(3):  # 0, 2, 4 s unreadable
        retirement.license_pass()
        clock[0] += 2
    assert exits == []
    retirement.license_pass()  # 6 s
    assert exits == [75]


def test_inventory_exits_for_removed_or_reconfigured_sessions_only(policy_db):
    exits = []
    retirement = _retirement(policy_db, exits)
    retirement.add(NINE, 'a')
    retirement.check_inventory({NINE: 'a', TEN: 'new printer'})
    assert exits == []
    retirement.check_inventory({NINE: 'b'})
    retirement.check_inventory({TEN: 'x'})
    assert exits == [75, 75]


def test_rollout_off_keeps_the_legacy_daemon_behaviour(monkeypatch):
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
    monkeypatch.setattr(license_monitor, 'monitor_loop', lambda *a, **k: pytest.fail('new loop used with rollout off'))
    monkeypatch.setattr(license_monitor, 'Retirement', lambda *a, **k: pytest.fail('retirement with rollout off'))
    license_monitor.run_monitor_process(None, None, None, start_all, 'X', stop)
    assert started == ['start', 'stopped']


def test_rollout_on_uses_the_retirement_loop(policy_db, monkeypatch):
    import core.monitor_bootstrap as bootstrap
    monkeypatch.setattr(bootstrap, 'bootstrap_monitor_authority', lambda: None)
    seen = []
    monkeypatch.setattr(license_monitor, 'monitor_loop', lambda *a, **k: seen.append(a[3]))
    license_monitor.run_monitor_process(policy_db.get_bind(), dict, None,
                                        lambda: pytest.fail('legacy start used'), 'X', threading.Event())
    assert seen == ['X']


# --- Separate processes ----------------------------------------------------------------

class _Peer:
    """TCP listener the child's sessions connect to; shows their sockets close on exit."""

    def __init__(self):
        self.server = socket.socket()
        self.server.bind(('127.0.0.1', 0))
        self.server.listen(16)
        self.port = self.server.getsockname()[1]
        self.conns = []
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            self.conns.append(conn)

    def all_closed(self, seconds=3):
        deadline = time.monotonic() + seconds
        for conn in list(self.conns):
            conn.settimeout(max(0.1, deadline - time.monotonic()))
            try:
                while conn.recv(64):
                    pass
            except (socket.timeout, OSError):
                return False
        return True

    def close(self):
        self.server.close()
        for conn in self.conns:
            conn.close()


class _Child:
    def __init__(self, tmp_path, db, mode, *, rollout=True, unreadable=False, tag='a', die=None):
        import license_manager
        self.log, self.flag = tmp_path / f'{tag}.log', tmp_path / f'{tag}.flag'
        self.out = open(tmp_path / f'{tag}.out', 'w')
        if unreadable:
            self.flag.write_text('x')
        self.peer = _Peer()
        env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(ROOT / 'backend'), str(ROOT)]),
                   DATABASE_URL=f'sqlite:///{db.get_bind().url.database}',
                   LICENSE_DIR=license_manager.LICENSE_DIR, B2C_PUBLIC_KEY=license_manager.ODIN_PUBLIC_KEY,
                   B2C_LOG=str(self.log), B2C_FLAG=str(self.flag), B2C_MODE=mode,
                   B2C_PEER_PORT=str(self.peer.port), B2C_DIE=str(die or ''))
        if not rollout:
            env.pop('ODIN_LICENSE_FLEET_LOCK_ENABLED', None)
        self.proc = subprocess.Popen([sys.executable, str(CHILD)], env=env, cwd=str(ROOT / 'backend'),
                                     stdout=self.out, stderr=subprocess.STDOUT)

    def lines(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def events(self):
        return [line.split(' ', 1)[1] for line in self.lines()]

    def output(self):
        self.out.flush()
        return Path(self.out.name).read_text()[-3000:]

    def wait_for(self, text, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if text in self.events():
                return True
            if self.proc.poll() is not None:
                return False
            time.sleep(0.05)
        return False

    def wait_exit(self, seconds):
        """(exit code, seconds waited) or (None, seconds) if still running."""
        started = time.monotonic()
        deadline = started + seconds
        while time.monotonic() < deadline:
            code = self.proc.poll()
            if code is not None:
                return code, time.monotonic() - started
            time.sleep(0.02)
        return None, time.monotonic() - started

    def close(self):
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(10)
        self.out.close()
        self.peer.close()


@pytest.fixture
def child(tmp_path, policy_db):
    children = []

    def spawn(mode, **kwargs):
        c = _Child(tmp_path, policy_db, mode, tag=f'c{len(children)}', **kwargs)
        children.append(c)
        return c
    yield spawn
    for c in children:
        c.close()


def _protocol(db, api_type, *printers):
    for pid in printers:
        db.execute(text("UPDATE printers SET api_type=:t, api_host=:h, api_key=NULL WHERE id=:p"),
                   {'t': api_type, 'h': f'127.0.0.{pid}', 'p': pid})
    db.commit()


def _alive(c, seconds):
    code, _ = c.wait_exit(seconds)
    return code is None


SHARED = {'moonraker_monitor': 'moonraker', 'prusalink_monitor': 'prusalink',
          'elegoo_monitor': 'elegoo', 'anycubic_lan_monitor': 'anycubic_lan'}


@pytest.mark.parametrize('module', sorted(SHARED))
def test_shared_loop_exits_75_within_3s_when_a_blocked_session_is_locked(policy_db, child, module):
    _protocol(policy_db, SHARED[module], NINE, TEN)
    c = child(module)
    assert c.wait_for('start 9', 30), c.output()
    assert _alive(c, 1) and 'start 10' not in c.events()  # 10 is outside the selection
    pick(policy_db, users=[3], printers=[TEN])  # drops 9, whose thread holds its lock forever
    code, waited = c.wait_exit(3)
    assert code == 75 and waited <= 3, (code, waited, c.output())
    assert c.peer.all_closed()  # the kernel closed the blocked session's socket


def test_shared_loop_restart_starts_only_allowed_and_handles_changes(policy_db, child, tier_license):
    _protocol(policy_db, 'prusalink', NINE, TEN)
    pick(policy_db, users=[3], printers=[TEN])
    c = child('prusalink_monitor')
    assert c.wait_for('start 10', 30), c.output()
    assert _alive(c, 3) and 'start 9' not in c.events()  # restarted with 9 disallowed: not started
    tier_license('enterprise', max_users=1, max_printers=2)
    pick(policy_db, users=[3], printers=[TEN, NINE])  # newly allowed: starts in-process
    assert c.wait_for('start 9', 15), c.output()
    assert _alive(c, 2)
    changed = time.monotonic()
    policy_db.execute(text("UPDATE printers SET api_host='127.0.0.99' WHERE id=9"))  # reconfigured
    policy_db.commit()
    code, _ = c.wait_exit(15)
    assert code == 75 and time.monotonic() - changed <= license_monitor.INVENTORY_SECONDS + 3, c.output()


def test_shared_loop_exits_75_when_a_session_thread_dies(policy_db, child):
    """A dead session thread may leave its transport open, so it is never replaced in-process."""
    _protocol(policy_db, 'elegoo', NINE, TEN)
    c = child('elegoo_monitor', die=NINE)
    assert c.wait_for('start 9', 30), c.output()
    code, _ = c.wait_exit(license_monitor.INVENTORY_SECONDS + 5)
    assert code == 75, c.output()
    assert c.events().count('start 9') == 1  # never restarted in-process
    assert c.peer.all_closed()


def test_shared_loop_unreadable_policy(policy_db, child, tmp_path):
    _protocol(policy_db, 'elegoo', NINE, TEN)
    idle = child('elegoo_monitor', unreadable=True)  # no session yet: fail closed, never exit
    assert _alive(idle, 8) and not any(e.startswith('start') for e in idle.events()), idle.output()
    idle.flag.unlink()
    assert idle.wait_for('start 9', 15), idle.output()
    idle.close()
    busy = child('elegoo_monitor')
    assert busy.wait_for('start 9', 30), busy.output()
    busy.flag.write_text('x')
    made_unreadable = time.monotonic()
    code, _ = busy.wait_exit(15)
    elapsed = time.monotonic() - made_unreadable
    assert code == 75 and license_monitor.UNREADABLE_EXIT_SECONDS < elapsed <= 10, (elapsed, busy.output())


def test_shared_loop_rollout_off_never_queries_or_exits(policy_db, child):
    _protocol(policy_db, 'prusalink', NINE, TEN)
    c = child('prusalink_monitor', rollout=False)
    assert c.wait_for('start 9', 30) and c.wait_for('start 10', 5), c.output()  # legacy: no license filter
    pick(policy_db, users=[3], printers=[TEN])
    policy_db.execute(text("UPDATE printers SET api_host='127.0.0.99' WHERE id=9"))
    policy_db.commit()
    assert _alive(c, 6), c.output()
    assert 'QUERY' not in c.events()


# --- Bambu (mqtt_monitor daemon) ----------------------------------------------------------

def _bambu(db, monkeypatch, *printers, hang=()):
    from cryptography.fernet import Fernet
    import core.crypto as crypto
    if not os.environ.get('ENCRYPTION_KEY'):
        monkeypatch.setenv('ENCRYPTION_KEY', Fernet.generate_key().decode())
    for pid in printers:
        db.execute(text("UPDATE printers SET api_type='bambu', api_host=:h, api_key=:k WHERE id=:p"),
                   {'h': 'hang' if pid in hang else f'127.0.0.{pid}', 'k': crypto.encrypt(f'SERIAL{pid}|code'),
                    'p': pid})
    db.commit()


def test_bambu_exits_75_within_3s_even_while_a_connect_hangs(policy_db, child, tier_license, monkeypatch):
    from core import license_lock
    _bambu(policy_db, monkeypatch, NINE, TEN, hang=(TEN,))
    policy_db.execute(text('UPDATE printers SET is_active=0 WHERE id=10'))
    policy_db.commit()
    tier_license('enterprise', max_users=1, max_printers=2)
    license_lock.reconcile(policy_db)
    policy_db.commit()
    c = child('bambu')
    assert c.wait_for('start 9', 30), c.output()
    policy_db.execute(text('UPDATE printers SET is_active=1 WHERE id=10'))  # an added printer
    policy_db.commit()
    license_lock.reconcile(policy_db)
    policy_db.commit()
    assert c.wait_for('connect 10', 15), c.output()  # started in-process; its connect never returns
    assert _alive(c, 3), c.output()
    tier_license('enterprise', max_users=1, max_printers=1)
    pick(policy_db, users=[3], printers=[TEN])  # 9's thread is blocked holding its lock
    code, waited = c.wait_exit(3)
    assert code == 75 and waited <= 3, (code, waited, c.output())
    assert c.peer.all_closed()


def test_bambu_reconfiguration_exits_75_even_while_a_connect_hangs(policy_db, child, tier_license, monkeypatch):
    """The removal/reconfiguration check runs on its own thread, not behind the connect loop."""
    from core import license_lock
    from core.crypto import encrypt
    _bambu(policy_db, monkeypatch, NINE, TEN, hang=(TEN,))
    tier_license('enterprise', max_users=1, max_printers=2)
    license_lock.reconcile(policy_db)
    policy_db.commit()
    c = child('bambu')
    assert c.wait_for('connect 10', 30), c.output()  # the main loop is now stuck in 10's connect
    if 'connect 9' in c.events():
        assert c.wait_for('start 9', 5), c.output()
    policy_db.execute(text('UPDATE printers SET api_key=:k WHERE id=9'), {'k': encrypt('OTHER|code')})
    policy_db.commit()
    changed = time.monotonic()
    code, _ = c.wait_exit(license_monitor.INVENTORY_SECONDS + 5)
    assert code == 75 and time.monotonic() - changed <= license_monitor.INVENTORY_SECONDS + 3, c.output()


def test_bambu_restart_starts_only_allowed_and_exits_on_reconfiguration(policy_db, child, monkeypatch):
    from core.crypto import encrypt
    _bambu(policy_db, monkeypatch, NINE, TEN)
    pick(policy_db, users=[3], printers=[TEN])
    c = child('bambu')
    assert c.wait_for('start 10', 30), c.output()
    assert _alive(c, 3) and 'connect 9' not in c.events()
    changed = time.monotonic()
    policy_db.execute(text('UPDATE printers SET api_key=:k WHERE id=10'), {'k': encrypt('OTHER|code')})
    policy_db.commit()
    code, _ = c.wait_exit(15)
    assert code == 75 and time.monotonic() - changed <= license_monitor.INVENTORY_SECONDS + 3, c.output()


def test_bambu_unreadable_policy(policy_db, child, monkeypatch):
    _bambu(policy_db, monkeypatch, NINE, TEN)
    idle = child('bambu', unreadable=True)
    assert _alive(idle, 8) and not any(e.startswith('connect') for e in idle.events()), idle.output()
    idle.close()
    busy = child('bambu')
    assert busy.wait_for('start 9', 30), busy.output()
    busy.flag.write_text('x')
    made_unreadable = time.monotonic()
    code, _ = busy.wait_exit(15)
    elapsed = time.monotonic() - made_unreadable
    assert code == 75 and license_monitor.UNREADABLE_EXIT_SECONDS < elapsed <= 10, (elapsed, busy.output())


def test_bambu_rollout_off_never_queries_or_exits(policy_db, child, monkeypatch):
    from core.crypto import encrypt
    _bambu(policy_db, monkeypatch, NINE, TEN)
    c = child('bambu', rollout=False)
    assert c.wait_for('start 9', 30) and c.wait_for('start 10', 5), c.output()
    pick(policy_db, users=[3], printers=[TEN])
    policy_db.execute(text('UPDATE printers SET api_key=:k WHERE id=9'), {'k': encrypt('OTHER|code')})
    policy_db.commit()
    assert _alive(c, 6), c.output()
    assert 'QUERY' not in c.events()
