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
                policy = license_lock.snapshot(db)
            self._seen = self._marker(policy)
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
        marker = self._marker(stored)
        if marker != self._seen:
            return True
        deadline = stored.deadline
        if deadline:
            try:
                from license_manager import license_clock
                from core.license_clock import instant
                return license_clock().sample()[0] >= instant(deadline) and not self._deadline_applied(stored)
            except Exception:
                return False
        # Exempt records resolve or retire on their own schedule (30 s bound).
        return bool(stored.exempt_attempts)

    @staticmethod
    def _marker(policy):
        return (policy.revision, tuple(sorted(policy.printers)),
                tuple(sorted((k, v.get('kind')) for k, v in (policy.exempt_attempts or {}).items())))

    @staticmethod
    def _deadline_applied(stored):
        return stored.entitlement.get('state') == 'verified_expired'


def drain(stoppers, joiners, seconds=TEARDOWN_SECONDS):
    """Signal every removal first, then join them against one shared deadline.

    stoppers/joiners: callables (no args / timeout). Returns the joiners that did
    not finish in time; callers abandon them (they are already retired)."""
    for stop in stoppers:
        try:
            stop()
        except Exception:
            pass
    deadline = time.monotonic() + seconds
    late = []
    for join in joiners:
        remaining = max(0.0, deadline - time.monotonic())
        try:
            if join(remaining) is False:
                late.append(join)
        except Exception:
            late.append(join)
    return late
