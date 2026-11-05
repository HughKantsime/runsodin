"""Worker handoff for the migration-pinned audit_actor request binding contract."""
from core.audit_actor import _BINDING, RequestAuditScope

def capture_request_actor(db):
    """Capture only a live request binding; return a scoped worker-session binder.

    No public snapshot/peer arguments: workers cannot manufacture an actor through
    this handoff. The immutable snapshot survives request cleanup, but stamp_actor
    still checks the erasure ledger in the worker's insertion transaction.
    """
    from contextlib import contextmanager
    binding = db.info.get(_BINDING)
    if binding is None or not binding[0].live or db not in binding[0].sessions:
        return None
    peer, snapshot = binding[0].peer, binding[1]

    @contextmanager
    def bind_worker(worker_db):
        scope = RequestAuditScope(peer)
        scope.register(worker_db)
        worker_db.info[_BINDING] = (scope, snapshot)
        try:
            yield
        finally:
            scope.clear()
    return bind_worker

