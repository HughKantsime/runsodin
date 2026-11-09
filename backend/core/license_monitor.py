"""License view for printer-monitor processes (B2c).

Each monitor process computes, every cycle, which of its own candidate printers
it may watch: members of the license selection, plus a newly locked printer
whose one exempt attempt still grants monitoring (license_lock.printer_allowed
'M'). Rollout off: candidates pass unchanged with no policy query.
"""
import time

from core import license_lock

LICENSE_PASS_SECONDS = 2
TEARDOWN_SECONDS = 5


class MonitorLicense:
    """One per monitor process; not shared across threads."""

    def __init__(self, engine):
        self._engine = engine
        self._seen = None
        self._exempt = False

    def wanted(self, candidates):
        """Subset of candidate printer ids this process may watch, or None if unknown.

        None means the policy could not be read: keep the current set rather than
        tearing everything down on a transient database error."""
        candidates = set(candidates)
        if not license_lock.enabled():
            return candidates
        from sqlalchemy.orm import Session
        from core.db import concrete_engine
        try:
            with Session(concrete_engine(self._engine)) as db:
                # While an exemption exists, reconcile every pass (no 30 s cache) so a
                # finished or replaced attempt stops being monitored within the bound.
                policy = license_lock.snapshot(db, fresh=self._exempt)
            self._seen = self._marker(policy)
            self._exempt = bool(policy.exempt_attempts)
            return {pid for pid in candidates if license_lock.printer_allowed(policy, pid, 'M')}
        except Exception:
            return None

    def changed(self):
        """Cheap probe for the fast pass: did the stored policy (or a deadline) move?

        A plain read with no lock and no reconcile; wanted() does the real check."""
        if not license_lock.enabled():
            return False
        from core.db import concrete_engine
        try:
            with concrete_engine(self._engine).connect() as conn:
                stored = license_lock.stored_snapshot(conn)
        except Exception:
            return False
        if stored is None:
            return self._seen is not None
        if self._marker(stored) != self._seen:
            return True
        if stored.exempt_attempts or self._exempt:
            return True  # exemptions resolve, expire or retire between revisions
        deadline = stored.deadline
        if deadline:
            try:
                from license_manager import license_clock
                from core.license_clock import instant
                return license_clock().sample()[0] >= instant(deadline) and not self._deadline_applied(stored)
            except Exception:
                return False
        return False

    @staticmethod
    def _marker(policy):
        return (policy.revision, tuple(sorted(policy.printers)),
                tuple(sorted((k, v.get('kind')) for k, v in (policy.exempt_attempts or {}).items())))

    @staticmethod
    def _deadline_applied(stored):
        return stored.entitlement.get('state') == 'verified_expired'


def drain(stoppers, joiners, seconds=TEARDOWN_SECONDS):
    """Stop and join every removal in parallel against one shared deadline.

    stoppers[i]() and then joiners[i](remaining) run on their own daemon worker,
    so a stop that blocks (a transport disconnect, a held lock) never delays the
    others. Returns the indexes of removals not confirmed stopped in time; their
    workers are abandoned. Callers must not start a replacement for those."""
    import threading
    deadline = time.monotonic() + seconds
    done = [False] * len(stoppers)

    def work(i):
        try:
            stoppers[i]()
        except Exception:
            pass
        try:
            done[i] = joiners[i](max(0.0, deadline - time.monotonic())) is not False
        except Exception:
            done[i] = False

    workers = [threading.Thread(target=work, args=(i,), daemon=True) for i in range(len(stoppers))]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(max(0.0, deadline - time.monotonic()))
    return [i for i, ok in enumerate(done) if not ok]


INVENTORY_SECONDS = 10


def _joiner(thread):
    def join(timeout):
        thread.join(timeout=timeout)
        return not thread.is_alive()
    return join


def reconcile_threads(threads, wanted, factory, label, log=None, lingering=None):
    """threads: {printer_id: (identity, thread)}. Stop removed, changed or dead
    threads against one shared deadline, then start additions with factory(pid, *identity).

    lingering: {printer_id: thread} still stopping from an earlier pass; no
    replacement starts for a printer until its old thread has really ended."""
    lingering = {} if lingering is None else lingering
    removals = [(pid, thread) for pid, (identity, thread) in threads.items()
                if wanted.get(pid) != identity or not thread.is_alive()]
    late = drain([thread.stop for _, thread in removals], [_joiner(thread) for _, thread in removals])
    if late and log is not None:
        log.warning('%s monitor: %d thread(s) did not stop within %ss; replacements wait',
                    label, len(late), TEARDOWN_SECONDS)
    for pid, thread in removals:
        del threads[pid]
        lingering[pid] = thread
    for pid in [p for p, t in lingering.items() if not t.is_alive()]:
        del lingering[pid]
    for pid, identity in wanted.items():
        if pid not in threads and pid not in lingering:  # never two sessions for one printer
            thread = factory(pid, *identity)
            threads[pid] = (identity, thread)
            thread.start()


def monitor_loop(engine, load_inventory, factory, label, stop_event, log=None):
    """Per-process loop: inventory every 10 s, license pass every 2 s.

    load_inventory() -> {printer_id: identity tuple}; a changed identity restarts
    that printer's thread. A failed inventory read keeps the previous one; an
    unreadable policy keeps the current permitted set."""
    threads, lingering, view = {}, {}, MonitorLicense(engine)
    inventory, permitted, next_inventory = {}, set(), 0.0
    try:
        while not stop_event.is_set():
            refresh = time.monotonic() >= next_inventory
            if refresh:
                try:
                    inventory = load_inventory()
                except Exception:
                    if log is not None:
                        log.warning('%s monitor: printer inventory could not be read; keeping the current set', label)
                next_inventory = time.monotonic() + INVENTORY_SECONDS
            if refresh or view.changed():
                allowed = view.wanted(inventory)
                if allowed is not None:
                    permitted = allowed
                reconcile_threads(threads, {p: i for p, i in inventory.items() if p in permitted},
                                  factory, label, log, lingering)
            stop_event.wait(LICENSE_PASS_SECONDS)
    finally:
        reconcile_threads(threads, {}, factory, label, log, lingering)
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
    """B2c loop when the license fleet lock is enabled; otherwise unchanged behaviour."""
    if license_lock.enabled():
        from core.monitor_bootstrap import bootstrap_monitor_authority
        bootstrap_monitor_authority()  # start_all does this itself on the legacy path
        return monitor_loop(engine, load_inventory, factory, label, stop_event, log)
    return run_legacy(start_all, stop_event, label, log)
