"""License retirement for printer-monitor processes (B2c).

Rollout on (ODIN_LICENSE_FLEET_LOCK_ENABLED): a monitor process starts a session
only for a printer the license lets it monitor (a member, or a newly locked
printer whose exempt attempt still grants 'M'). It never stops a session
in-process. When a printer that has a session (connected, connecting or
retrying) loses that permission, or is removed, deactivated or reconfigured,
the process logs one line and exits with code 75; supervisord (autorestart,
startsecs=0) starts it again and it starts only the allowed printers. Exiting
closes every socket and ends every thread, whatever locks or transports they
hold, so the bound does not depend on any monitor's cooperation.

Rollout off: the legacy daemon behaviour (start the monitors once, then wait),
with no policy query.
"""
import hashlib
import os
import threading
import time

from core import license_lock

RETIRE_EXIT_CODE = 75
LICENSE_PASS_SECONDS = 2
INVENTORY_SECONDS = 10
UNREADABLE_EXIT_SECONDS = 5
POLICY_READ_SECONDS = 1.5  # a read still running after this counts as unreadable
POLICY_BUSY_TIMEOUT_MS = 1000
POLICY_ABANDON_SECONDS = 10  # a read hung this long is left behind and a new one starts


def connection_fingerprint(*values):
    """Digest of a printer's connection settings: protocol, host and port, serial,
    credential. Never telemetry, discovered fields or the display name."""
    return hashlib.sha256(repr(values).encode()).hexdigest()


class MonitorLicense:
    """Bounded, fresh policy reads for one monitor process.

    Reads use their own connection on a dedicated engine (no pool wait; SQLite
    busy timeout 1 s, PostgreSQL statement timeout 1 s) and run on a helper
    thread, so a caller waits at most POLICY_READ_SECONDS. One read at a time:
    while an earlier read is still running, the policy counts as unreadable (a read
    hung for POLICY_ABANDON_SECONDS is abandoned, so a stuck call cannot block
    every later read)."""

    def __init__(self, engine):
        self._engine = engine
        self._bounded = None
        self._bounded_url = None
        self._exempt = False
        self._reader = None
        self._reader_started = 0.0
        self._guard = threading.Lock()

    def _policy_engine(self):
        from sqlalchemy import event
        from sqlalchemy.pool import NullPool
        from core.database_config import create_database_engine
        from core.db import concrete_engine
        url = concrete_engine(self._engine).url
        if self._bounded is None or self._bounded_url != url:
            bounded = create_database_engine(url, role=os.getenv('ODIN_DB_ROLE', 'api'), poolclass=NullPool,
                                             sqlite_busy_timeout_ms=POLICY_BUSY_TIMEOUT_MS)
            if bounded.dialect.name == 'postgresql':
                @event.listens_for(bounded, 'connect')
                def _bound_statements(dbapi_connection, _record):
                    cursor = dbapi_connection.cursor()
                    try:
                        cursor.execute(f'SET statement_timeout = {POLICY_BUSY_TIMEOUT_MS}')
                    finally:
                        cursor.close()
                    dbapi_connection.commit()
            self._bounded, self._bounded_url = bounded, url
        return self._bounded

    def _read(self):
        from sqlalchemy.orm import Session
        with Session(self._policy_engine()) as db:
            # While an exemption exists, reconcile on every read (no process cache), so
            # an attempt that ended stops granting monitoring within one pass.
            return license_lock.snapshot(db, fresh=self._exempt)

    def policy(self, timeout=POLICY_READ_SECONDS):
        """A fresh snapshot, or None when it could not be read within timeout."""
        result = {}

        def read():
            try:
                result['policy'] = self._read()
            except Exception:
                pass
        with self._guard:
            if (self._reader is not None and self._reader.is_alive()
                    and time.monotonic() - self._reader_started < POLICY_ABANDON_SECONDS):
                return None
            reader = threading.Thread(target=read, name='license-policy-read', daemon=True)
            self._reader, self._reader_started = reader, time.monotonic()
        reader.start()
        reader.join(timeout)
        policy = None if reader.is_alive() else result.get('policy')
        if policy is not None:
            self._exempt = bool(policy.exempt_attempts)
        return policy

    def wanted(self, candidates):
        """Subset of candidate printer ids this process may monitor; None if unreadable.

        Rollout off: the candidates unchanged, with no policy query."""
        candidates = set(candidates)
        if not license_lock.enabled():
            return candidates
        policy = self.policy()
        if policy is None:
            return None
        return {pid for pid in candidates if license_lock.printer_allowed(policy, pid, 'M')}


class Retirement:
    """Rollout on: the sessions of one monitor process and the checks that end it.

    A session is registered before its first connect and stays registered for
    the life of the process (connected, connecting or retrying). The license pass
    runs on its own thread, independent of connect and reconnect work."""

    def __init__(self, engine, label, log=None, exit=None):
        self.license = MonitorLicense(engine)
        self.label, self.log = label, log
        self._exit = exit or os._exit
        self._sessions = {}
        self._lock = threading.Lock()
        self._latest = None
        self._unreadable_since = None
        self._first_pass = threading.Event()
        self.thread = None

    def add(self, printer_id, fingerprint):
        with self._lock:
            self._sessions[printer_id] = fingerprint

    def has(self, printer_id):
        with self._lock:
            return printer_id in self._sessions

    def sessions(self):
        with self._lock:
            return dict(self._sessions)

    def retire(self, reason):
        """Log one line and end the process; supervisord restarts it with the allowed set.

        os._exit skips signal handlers, atexit and thread joins that could block.
        The line is written straight to stderr (no logging lock)."""
        line = (f"{time.strftime('%Y-%m-%d %H:%M:%S')} [WARNING] {self.label} monitor: {reason}; "
                f"exiting with code {RETIRE_EXIT_CODE} so it restarts with the allowed printers\n")
        try:
            os.write(2, line.encode())
        except Exception:
            pass
        self._exit(RETIRE_EXIT_CODE)

    def license_pass(self):
        started = time.monotonic()
        try:
            policy = self.license.policy()
        except Exception:
            policy = None
        sessions = self.sessions()
        if policy is None:
            self._latest = None
            if not sessions:
                self._unreadable_since = None  # fail closed: start nothing, retry; never exit for it
                return None
            if self._unreadable_since is None:
                self._unreadable_since = started
            elif time.monotonic() - self._unreadable_since > UNREADABLE_EXIT_SECONDS:
                self.retire(f'license policy unreadable for more than {UNREADABLE_EXIT_SECONDS} s '
                            f'with {len(sessions)} printer session(s)')
            return None
        self._unreadable_since = None
        self._latest = (policy, started)
        locked = sorted(pid for pid in sessions if not license_lock.printer_allowed(policy, pid, 'M'))
        if locked:
            self.retire(f'printer(s) {locked} no longer licensed for monitoring')
        return policy

    def check_inventory(self, inventory):
        """inventory: {printer_id: fingerprint}. A session whose printer was removed,
        deactivated or reconfigured ends the process."""
        sessions = self.sessions()
        gone = sorted(pid for pid in sessions if pid not in inventory)
        changed = sorted(pid for pid, fp in sessions.items() if pid in inventory and inventory[pid] != fp)
        if gone or changed:
            self.retire(f'printer(s) {gone} removed or deactivated, {changed} reconfigured')

    def current(self):
        """The latest policy if it was read within the last two passes, else None."""
        self._first_pass.wait(LICENSE_PASS_SECONDS + POLICY_READ_SECONDS)
        latest = self._latest
        if latest is None or time.monotonic() - latest[1] > 2 * LICENSE_PASS_SECONDS + POLICY_READ_SECONDS:
            return None
        return latest[0]

    def _run(self, stop_event):
        while stop_event is None or not stop_event.is_set():
            started = time.monotonic()
            try:
                self.license_pass()
            finally:
                self._first_pass.set()
            delay = max(0.0, LICENSE_PASS_SECONDS - (time.monotonic() - started))
            if stop_event is None:
                threading.Event().wait(delay)
            else:
                stop_event.wait(delay)

    def start(self, stop_event=None):
        self.thread = threading.Thread(target=self._run, args=(stop_event,),
                                       name=f'{self.label}-license-retirement', daemon=True)
        self.thread.start()
        return self


def _stop_all(threads, seconds=5):
    """Shutdown (SIGTERM) only: ask every session to stop without waiting on a hung one."""
    threads = list(threads)

    def stop():
        for thread in threads:
            try:
                thread.stop()
            except Exception:
                pass
    worker = threading.Thread(target=stop, daemon=True)
    worker.start()
    worker.join(seconds)


def monitor_loop(engine, load_inventory, factory, label, stop_event, log=None, exit=None):
    """Rollout on: the shared daemon loop (Moonraker, PrusaLink, Elegoo, Anycubic).

    load_inventory() -> {printer_id: (fingerprint, args)}; factory(printer_id, *args)
    returns a thread-like session (start, is_alive, stop). Every INVENTORY_SECONDS:
    a session's printer removed, deactivated or reconfigured -> exit 75; a new or
    newly allowed printer, or a session whose thread died, starts in-process."""
    retirement = Retirement(engine, label, log, exit).start(stop_event)
    threads = {}
    try:
        while not stop_event.is_set():
            wait = INVENTORY_SECONDS
            try:
                inventory = load_inventory()
            except Exception:
                inventory = None
                wait = LICENSE_PASS_SECONDS
                if log is not None:
                    log.warning('%s monitor: printer inventory could not be read; retrying', label)
            if inventory is not None:
                retirement.check_inventory({pid: entry[0] for pid, entry in inventory.items()})
                pending = [pid for pid in inventory if pid not in threads or not threads[pid].is_alive()]
                policy = retirement.current() if pending else None
                if pending and policy is None:
                    wait = LICENSE_PASS_SECONDS  # unreadable: start nothing, retry soon
                for pid in pending if policy is not None else ():
                    if not license_lock.printer_allowed(policy, pid, 'M'):
                        continue
                    fingerprint, args = inventory[pid]
                    retirement.add(pid, fingerprint)  # a session from its first connect attempt
                    thread = factory(pid, *args)
                    threads[pid] = thread
                    thread.start()
            stop_event.wait(wait)
    finally:
        _stop_all(threads.values())
    return threads


def run_legacy(start_all, stop_event, label, log=None):
    """The pre-B2c daemon behaviour, kept while the rollout is off: start the
    monitors once (retry every 60 s only while none started), then wait."""
    started = []
    while not stop_event.is_set():
        started = start_all()
        if started:
            if log is not None:
                log.info('Monitoring %d %s printer(s)', len(started), label)
            stop_event.wait()
            break
        if log is not None:
            log.info('No %s printers found in database; sleeping 60s', label)
        stop_event.wait(60)
    for monitor in started:
        try:
            monitor.stop()
        except Exception:
            pass


def run_monitor_process(engine, load_inventory, factory, start_all, label, stop_event, log=None):
    """Restart retirement when the license fleet lock is enabled; otherwise unchanged behaviour."""
    if license_lock.enabled():
        from core.monitor_bootstrap import bootstrap_monitor_authority
        bootstrap_monitor_authority()  # start_all does this itself on the legacy path
        return monitor_loop(engine, load_inventory, factory, label, stop_event, log)
    return run_legacy(start_all, stop_event, label, log)
