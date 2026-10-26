"""The one place that changes ``jobs.status``.

Every status change in the codebase goes through :func:`transition_job`, which
performs a single guarded, atomic UPDATE::

    UPDATE jobs SET status=:to, <extra fields> WHERE id=:job
      AND status IN (<allowed from-states for this actor>) [AND <extra predicate>]

and reports whether it applied. The database row, not a Python read, decides,
so concurrent writers cannot overwrite each other's transitions. A census test
(tests/test_contracts/test_cert_D_job_state_machine.py) fails if any other
module writes ``jobs.status``.

Actors
------
operator   lifecycle routes driven by a person (start, complete, fail, cancel,
           reset, move, manual link of an observed print)
approval   the generic approval workflow (approve, reject, resubmit)
scheduler  the scheduler and queue bookkeeping (assign, unassign, stale cleanup)
dispatch   a dispatch the printer accepted
monitor    observed printer events (MQTT, Moonraker, PrusaLink/Elegoo events, HMS)
plate      operator confirmation that the bed is clear
education  the Education lifecycle services (which add their own predicates)

Terminal transitions and rejection always clear ``queue_position``.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Mapping, Optional

PENDING, SUBMITTED, REJECTED, SCHEDULED = "pending", "submitted", "rejected", "scheduled"
PRINTING, PAUSED, COMPLETED, FAILED, CANCELLED = "printing", "paused", "completed", "failed", "cancelled"
WAITING_PLATE_CLEAR = "waiting_plate_clear"

TERMINAL = frozenset({COMPLETED, FAILED, CANCELLED})
LIVE = frozenset({PENDING, SCHEDULED, PRINTING, PAUSED})

# actor -> to-status -> allowed from-statuses
TRANSITIONS: dict[str, dict[str, frozenset[str]]] = {
    "operator": {
        PRINTING: frozenset({PENDING, SCHEDULED}),
        # Completed/failed may be corrected into each other after inspection.
        COMPLETED: frozenset({PENDING, SCHEDULED, PRINTING, PAUSED, FAILED}),
        FAILED: frozenset({PENDING, SCHEDULED, PRINTING, PAUSED, COMPLETED}),
        CANCELLED: frozenset({PENDING, SCHEDULED, SUBMITTED}),
        # Reset starts a new attempt: only from a finished outcome.
        PENDING: frozenset({COMPLETED, FAILED, CANCELLED}),
        SCHEDULED: frozenset({PENDING, SCHEDULED}),
    },
    "outside_binding": {
        PENDING: frozenset({COMPLETED}),
        SCHEDULED: frozenset({COMPLETED}),
        PRINTING: frozenset({COMPLETED}),
    },
    "approval": {
        PENDING: frozenset({SUBMITTED}),
        REJECTED: frozenset({SUBMITTED}),
        SUBMITTED: frozenset({REJECTED}),
    },
    "scheduler": {
        SCHEDULED: frozenset({PENDING}),
        PENDING: frozenset({PENDING, SCHEDULED}),
    },
    "dispatch": {
        PRINTING: frozenset({PENDING, SCHEDULED, PRINTING}),
    },
    "monitor": {
        PRINTING: frozenset({PENDING, SCHEDULED, PRINTING}),
        PENDING: frozenset({SCHEDULED}),
        # A print end never overwrites a terminal, unapproved or rejected job.
        COMPLETED: LIVE,
        FAILED: LIVE,
        CANCELLED: LIVE,
    },
    "plate": {
        PENDING: frozenset({WAITING_PLATE_CLEAR}),
    },
    "education": {
        SCHEDULED: frozenset({PENDING}),
        PENDING: frozenset({SCHEDULED, SUBMITTED}),
        SUBMITTED: frozenset({PENDING, SCHEDULED, PRINTING}),
        REJECTED: frozenset({SUBMITTED}),
        PRINTING: frozenset({SCHEDULED, PRINTING}),
        COMPLETED: frozenset({PRINTING}),
        FAILED: frozenset({PRINTING}),
        CANCELLED: frozenset({PRINTING}),
    },
}

_CLEARS_QUEUE_POSITION = TERMINAL | {REJECTED}
_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")
_NAMED = re.compile(r"(?<!:):([A-Za-z_][A-Za-z0-9_]*)")


_TARGET_COLUMNS = frozenset({"target_type", "target_filter"})


class Expr:
    """A SQL expression for a SET column, e.g. Expr('COALESCE(actual_start, :start)', start=now)."""

    def __init__(self, sql: str, **params: Any):
        self.sql = sql
        self.params = params


class TransitionError(ValueError):
    pass


def allowed_from(actor: str, to: str) -> frozenset[str]:
    try:
        return TRANSITIONS[actor][to]
    except KeyError:
        raise TransitionError(f"{actor} may not move a job to {to}") from None


def can_transition(actor: str, current: Optional[str], to: str) -> bool:
    value = getattr(current, "value", current)
    return value in TRANSITIONS.get(actor, {}).get(to, frozenset())


def _status_value(value):
    return getattr(value, "value", value)


def transition_job(executor, job_id: int, to, actor: str, *,
                   extra_fields: Optional[Mapping[str, Any]] = None,
                   from_states: Optional[Iterable[str]] = None,
                   where: Optional[str] = None,
                   params: Optional[Mapping[str, Any]] = None,
                   include_null: bool = False,
                   retarget: bool = False) -> bool:
    """Atomically move one job to ``to`` if its current status allows it.

    ``executor`` may be a SQLAlchemy Session/Connection, a DBAPI connection or a
    DBAPI cursor (the monitor paths). ``from_states`` may narrow, never widen,
    the actor's allowed from-set. ``where`` adds a predicate (named parameters
    from ``params``). ``include_null`` also admits a legacy row whose status is
    NULL (used only by the operator /complete route, as before the state
    machine). ``retarget`` admits target_type/target_filter in ``extra_fields``;
    only an explicit operator printer choice (move_job) passes it, so no
    reset/resubmit/requeue can rewrite a pin or a deleted:<id> marker.
    Returns True when exactly this call changed the row.
    """
    to = _status_value(to)
    allowed = allowed_from(actor, to)
    if from_states is not None:
        narrowed = frozenset(_status_value(s) for s in from_states)
        if not narrowed <= allowed:
            raise TransitionError(f"{actor}: {sorted(narrowed - allowed)} -> {to} is not an allowed transition")
        allowed = narrowed
    if not allowed:
        return False
    values: dict[str, Any] = {"sm_to": to, "sm_job": int(job_id)}
    sets = ["status=:sm_to"]
    fields = dict(extra_fields or {})
    if to in _CLEARS_QUEUE_POSITION:
        fields.setdefault("queue_position", None)
    for index, (column, value) in enumerate(fields.items()):
        if column == "status" or not _IDENT.match(column):
            raise TransitionError(f"Invalid job column {column!r}")
        if column in _TARGET_COLUMNS and not retarget:
            # A status change never retargets a job: an explicit pin (including a
            # deleted:<id> marker) survives every reset/resubmit/requeue.
            raise TransitionError(f"A status transition may not write {column!r}")
        if isinstance(value, Expr):
            sets.append(f"{column}={value.sql}")
            values.update(value.params)
        else:
            name = f"sm_v{index}"
            sets.append(f"{column}=:{name}")
            values[name] = value
    placeholders = []
    for index, state in enumerate(sorted(allowed)):
        name = f"sm_f{index}"
        placeholders.append(f":{name}")
        values[name] = state
    status_clause = f"status IN ({', '.join(placeholders)})"
    if include_null:
        status_clause = f"({status_clause} OR status IS NULL)"
    statement = f"UPDATE jobs SET {', '.join(sets)} WHERE id=:sm_job AND {status_clause}"
    if where:
        statement += f" AND ({where})"
        values.update(params or {})
    return _execute(executor, statement, values) == 1


def _execute(executor, statement: str, values: dict) -> int:
    from sqlalchemy.engine import Connection
    from sqlalchemy.orm import Session
    from sqlalchemy import text
    # SQLAlchemy sessions/connections and SQLAlchemy-style views (anything that
    # exposes a dialect, e.g. core TransactionView) take text() + named params.
    if isinstance(executor, (Session, Connection)) or hasattr(executor, "dialect"):
        return executor.execute(text(statement), values).rowcount
    ordered: list[str] = []

    def replace(match):
        ordered.append(match.group(1))
        return "?"
    positional = _NAMED.sub(replace, statement)
    arguments = tuple(values[name] for name in ordered)
    cursor = executor.cursor() if hasattr(executor, "cursor") and not hasattr(executor, "fetchone") else executor
    result = cursor.execute(positional, arguments)
    count = getattr(result, "rowcount", None)
    if count is None or count == -1:
        count = getattr(cursor, "rowcount", -1)
    return count


def align_identity(job, status, **attributes) -> None:
    """Mirror a transition that already happened in SQL onto an ORM instance, without a write."""
    from sqlalchemy.orm.attributes import set_committed_value
    set_committed_value(job, "status", status)
    for name, value in attributes.items():
        set_committed_value(job, name, value)


class _Outcome:
    __slots__ = ("rowcount",)

    def __init__(self, applied: bool):
        self.rowcount = 1 if applied else 0


def transition_result(executor, job_id: int, to, actor: str, **options) -> _Outcome:
    """transition_job for callers that check ``.rowcount`` like a DBAPI result."""
    return _Outcome(transition_job(executor, job_id, to, actor, **options))
