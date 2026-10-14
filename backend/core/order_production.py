"""Registry boundary for atomic linked-job production transitions."""
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session
from sqlalchemy.engine import Connection
from core.registry import registry


def _bounded_parent_job_ids(job_ids):
    from itertools import islice
    values = tuple(islice(iter(job_ids), 17))
    if len(values) > 16 or any(type(value) is not int or value <= 0 for value in values):
        raise HTTPException(400, 'Invalid bounded production parent set')
    return tuple(sorted(set(values)))


def prepare_parent_graph(executor, job_ids):
    """Fence production parents before callers acquire submission/job locks.

    This captures linkage only. It does not attest financial ownership, authorize
    an observed start, lock jobs, commit, or reconcile production quantities.
    """
    identifiers = _bounded_parent_job_ids(job_ids)
    provider = registry.get_provider('OrderFulfillmentProvider')
    prepare = getattr(provider, 'prepare_parent_graph', None)
    if not callable(prepare):
        # Existing fail-closed provider boundary distinguishes unrelated jobs.
        if prepare_job_write(executor, identifiers):
            raise HTTPException(503, 'Order parent preparation unavailable')
        return ((), (), ())
    return prepare(executor, identifiers)


def _validated_parent_graph(graph):
    if type(graph) is not tuple or len(graph) != 3:
        raise HTTPException(400, 'Invalid prepared production parent graph')
    orders, items, rows = graph
    if any(type(values) is not tuple for values in graph) or len(rows) > 16:
        raise HTTPException(400, 'Invalid prepared production parent graph')
    if any(type(row) is not tuple or len(row) != 6 for row in rows):
        raise HTTPException(400, 'Invalid prepared production parent graph')
    if any(type(row[0]) is not int or row[0] <= 0 or any(value is not None and (type(value) is not int or value <= 0) for value in row[1:]) for row in rows):
        raise HTTPException(400, 'Invalid prepared production parent graph')
    identifiers = _bounded_parent_job_ids(row[0] for row in rows)
    if tuple(row[0] for row in rows) != identifiers:
        raise HTTPException(400, 'Invalid prepared production parent graph')
    expected_orders = tuple(sorted({value for row in rows for value in (row[2], row[4], row[5]) if value is not None}))
    expected_items = tuple(sorted({value for row in rows for value in (row[1], row[3]) if value is not None}))
    if any(type(value) is not int or value <= 0 for values in (orders, items) for value in values) or orders != expected_orders or items != expected_items:
        raise HTTPException(400, 'Invalid prepared production parent graph')
    return identifiers


def recheck_parent_graph(executor, prepared_graph):
    """SELECT-only ancestry reproof after caller job locks; never reacquire parents."""
    _validated_parent_graph(prepared_graph)
    provider = registry.get_provider('OrderFulfillmentProvider')
    recheck = getattr(provider, 'recheck_parent_graph', None)
    if not callable(recheck):
        raise HTTPException(503, 'Order parent recheck unavailable')
    return recheck(executor, prepared_graph)


def prepare_job_write(executor,job_ids,changes=None):
    provider=registry.get_provider('OrderFulfillmentProvider')
    if provider is None:
        # Absence is harmless for unrelated jobs, never for linked production.
        for identifier in set(job_ids):
            statement = ("SELECT 1 FROM jobs WHERE id=:id AND order_item_id IS NOT NULL "
                         "UNION SELECT 1 FROM order_production_jobs WHERE job_id=:id")
            if isinstance(executor, (Session, Connection)):
                linked = executor.execute(text(statement), {'id': identifier}).first()
            else:
                linked = executor.cursor().execute(statement.replace(':id', '?'),
                                                   (identifier, identifier)).fetchone()
            if linked is not None:
                raise HTTPException(503, 'Order production service unavailable')
        return []
    parents=provider.lock_for_jobs(executor,job_ids)
    if changes:
        for identifier in set(job_ids):provider.assert_job_edit_allowed(executor,identifier,changes)
    return parents


def reconcile_job_write(executor,parents):
    if not parents:return
    provider=registry.get_provider('OrderFulfillmentProvider')
    if provider is None:raise HTTPException(503,'Order production service unavailable')
    provider.reconcile_orders(executor,parents)


class ProductionLinkageChanged(RuntimeError):
    """Caller must roll back and retry from the newly observed parent linkage."""


def retry_production_linkage(function):
    """Retry only pre-mutation linkage races using a fresh caller transaction."""
    from functools import wraps
    import logging
    @wraps(function)
    def wrapped(*args, **kwargs):
        for _ in range(3):
            try:
                return function(*args, **kwargs)
            except ProductionLinkageChanged:
                continue
        logging.getLogger('odin.production').warning(
            'Print linkage kept changing; terminal observation requires reconciliation')
        return None
    return wrapped


def prepare_print_job_write(executor, print_job_id):
    """Acquire parent before print record, then validate the parent was current.

    A portable no-op UPDATE takes the print-row lock. If auto-link committed
    between the initial read and that lock, acquiring its parent now would
    invert lock order. Instead roll back the entire observation and retry.
    """
    def row(statement):
        if isinstance(executor, (Session, Connection)):
            return executor.execute(text(statement), {'id':print_job_id}).first()
        return executor.cursor().execute(statement.replace(':id', '?'),
                                         (print_job_id,)).fetchone()
    before = row('SELECT scheduled_job_id FROM print_jobs WHERE id=:id')
    identifier = before[0] if before else None
    parents = prepare_job_write(executor, [identifier]) if identifier else []
    locked = row('UPDATE print_jobs SET scheduled_job_id=scheduled_job_id WHERE id=:id RETURNING scheduled_job_id')
    if (locked[0] if locked else None) != identifier:
        raise ProductionLinkageChanged()
    return identifier, parents
