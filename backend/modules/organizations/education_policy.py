"""Central database policy checks for Education-owned resources."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import uuid
from contextlib import contextmanager

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError as SQLAlchemyIntegrityError
from sqlalchemy.orm import Session
from core.interfaces.education_policy import (
    is_education_reserved_filename,
    parse_education_token,
)


_ALLOWED_REMOTE_EXTENSIONS = frozenset({".3mf", ".gcode", ".bgcode"})
_NAMED_PARAMETER_RE = re.compile(r"(?<!:):([A-Za-z_][A-Za-z0-9_]*)")


def _resolve_monitor_tokens(*values):
    """Resolve all task identities without letting an internal path mask a token."""
    tokens = {token for value in values if (token := parse_education_token(value))}
    reserved = bool(tokens)
    return (next(iter(tokens)) if len(tokens) == 1 else None, reserved, len(tokens) > 1)


def education_token_digest(token: str) -> str:
    return hashlib.sha256(str(token).lower().encode("ascii")).hexdigest()


from modules.jobs.services import transition_job, transition_result, Expr

class _ExecutionAdapter:
    """Bound-value SQL adapter for ORM and monitor DBAPI transactions."""

    def __init__(self, executor):
        self.executor = executor
        self.sqlalchemy = isinstance(executor, (Session, Connection))

    def execute(self, statement: str, params: dict | None = None):
        values = params or {}
        if self.sqlalchemy:
            return self.executor.execute(text(statement), values)
        ordered_names: list[str] = []

        def replace(match: re.Match) -> str:
            ordered_names.append(match.group(1))
            return "?"

        positional_statement = _NAMED_PARAMETER_RE.sub(replace, statement)
        positional_values = tuple(values[name] for name in ordered_names)
        return self.executor.cursor().execute(positional_statement, positional_values)

    def fetchone(self, statement: str, params: dict | None = None) -> dict | None:
        result = self.execute(statement, params)
        row = result.fetchone()
        if row is None:
            return None
        if hasattr(row, "_mapping"):
            return dict(row._mapping)
        columns = [item[0] for item in result.description]
        return dict(zip(columns, row))

    def fetchall(self, statement: str, params: dict | None = None) -> list[dict]:
        result = self.execute(statement, params)
        rows = result.fetchall()
        if rows and hasattr(rows[0], "_mapping"):
            return [dict(row._mapping) for row in rows]
        columns = [item[0] for item in result.description]
        return [dict(zip(columns, row)) for row in rows]

    @contextmanager
    def savepoint(self):
        if self.sqlalchemy:
            with self.executor.begin_nested():
                yield
            return
        name = f"education_monitor_{uuid.uuid4().hex}"
        # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query, python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- UUID-only internal savepoint identifier; SQL parameters cannot name savepoints
        self.executor.cursor().execute(f"SAVEPOINT {name}")  # nosec B608 -- generated identifier
        try:
            yield
        except Exception:
            # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query, python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- UUID-only internal savepoint identifier; SQL parameters cannot name savepoints
            self.executor.cursor().execute(f"ROLLBACK TO SAVEPOINT {name}")  # nosec B608
            # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query, python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- UUID-only internal savepoint identifier; SQL parameters cannot name savepoints
            self.executor.cursor().execute(f"RELEASE SAVEPOINT {name}")  # nosec B608
            raise
        else:
            # nosemgrep: python.lang.security.audit.formatted-sql-query.formatted-sql-query, python.sqlalchemy.security.sqlalchemy-execute-raw-query.sqlalchemy-execute-raw-query -- UUID-only internal savepoint identifier; SQL parameters cannot name savepoints
            self.executor.cursor().execute(f"RELEASE SAVEPOINT {name}")  # nosec B608


class _MonitorTransitionMiss(RuntimeError):
    """Roll back a monitor-policy savepoint without owning its transaction."""


def _is_integrity_error(exc: Exception) -> bool:
    return isinstance(exc, (sqlite3.IntegrityError, SQLAlchemyIntegrityError)) or (
        exc.__class__.__name__ in {"IntegrityError", "UniqueViolation", "ForeignKeyViolation"}
    )


def _exists(db: Session, statement: str, params: dict) -> bool:
    return db.execute(text(statement), params).fetchone() is not None


def assert_generic_model_reference_allowed(db: Session, model_id: int) -> None:
    if _exists(db, "SELECT 1 FROM education_submissions s JOIN models m "
            "ON (s.model_id=m.id OR s.print_file_id=m.print_file_id) "
            "WHERE m.id=:id LIMIT 1", {"id": model_id}):
        raise HTTPException(status_code=409, detail="Education files and models require the Education submission and approval workflow")


def assert_generic_job_mutation_allowed(db: Session, job_id: int) -> None:
    if _exists(db, "SELECT 1 FROM education_submissions WHERE job_id=:id LIMIT 1", {"id": job_id}):
        raise HTTPException(status_code=409, detail=(
            "This job belongs to Education. Use the Education workflow; "
            "an unresolved printer command must be reconciled before further action."
        ))


def assert_user_tenant_change_allowed(db: Session, user_id: int) -> None:
    if _exists(
        db,
        "SELECT 1 FROM education_cost_center_grants "
        "WHERE user_id=:id OR granted_by=:id OR revoked_by=:id LIMIT 1",
        {"id": user_id},
    ) or _exists(
        db,
        "SELECT 1 FROM education_cost_center_printers "
        "WHERE granted_by=:id OR revoked_by=:id LIMIT 1",
        {"id": user_id},
    ) or _exists(
        db,
        "SELECT 1 FROM education_cost_centers WHERE created_by=:id LIMIT 1",
        {"id": user_id},
    ) or _exists(
        db,
        "SELECT 1 FROM education_upload_operations WHERE user_id=:id LIMIT 1",
        {"id": user_id},
    ) or _exists(
        db,
        "SELECT 1 FROM education_submissions WHERE submitted_by=:id OR approved_by=:id LIMIT 1",
        {"id": user_id},
    ) or _exists(
        db,
        "SELECT 1 FROM education_notification_outbox WHERE recipient_user_id=:id LIMIT 1",
        {"id": user_id},
    ) or _exists(
        db,
        "SELECT 1 FROM education_rate_counters WHERE user_id=:id LIMIT 1",
        {"id": user_id},
    ) or _exists(
        db,
        "SELECT 1 FROM education_storage_accounts WHERE user_id=:id LIMIT 1",
        {"id": user_id},
    ) or _exists(
        db,
        "SELECT 1 FROM education_commands WHERE actor_kind='user' AND actor_id=:actor_id LIMIT 1",
        {"actor_id": str(user_id)},
    ) or _exists(
        db,
        "SELECT 1 FROM education_audit_events WHERE actor_kind='user' AND actor_id=:actor_id LIMIT 1",
        {"actor_id": str(user_id)},
    ):
        raise HTTPException(
            status_code=409,
            detail="User tenant cannot change while Education grants or history exist",
        )


def assert_user_hard_delete_allowed(db: Session, user_id: int) -> None:
    if _exists(db, "SELECT 1 FROM education_memberships WHERE user_id=:id LIMIT 1", {"id": user_id}) or _exists(
        db,
        "SELECT 1 FROM education_cost_center_grants WHERE user_id=:id OR granted_by=:id OR revoked_by=:id LIMIT 1",
        {"id": user_id},
    ) or _exists(
        db,
        "SELECT 1 FROM education_cost_center_printers "
        "WHERE granted_by=:id OR revoked_by=:id LIMIT 1",
        {"id": user_id},
    ) or _exists(
        db,
        "SELECT 1 FROM education_cost_centers WHERE created_by=:id LIMIT 1",
        {"id": user_id},
    ) or _exists(
        db,
        "SELECT 1 FROM education_upload_operations WHERE user_id=:id LIMIT 1",
        {"id": user_id},
    ) or _exists(
        db,
        "SELECT 1 FROM education_submissions WHERE submitted_by=:id OR approved_by=:id LIMIT 1",
        {"id": user_id},
    ) or _exists(
        db,
        "SELECT 1 FROM education_notification_outbox WHERE recipient_user_id=:id LIMIT 1",
        {"id": user_id},
    ) or _exists(
        db,
        "SELECT 1 FROM education_rate_counters WHERE user_id=:id LIMIT 1",
        {"id": user_id},
    ) or _exists(
        db,
        "SELECT 1 FROM education_storage_accounts WHERE user_id=:id LIMIT 1",
        {"id": user_id},
    ) or _exists(
        db,
        "SELECT 1 FROM education_commands WHERE actor_kind='user' AND actor_id=:actor_id LIMIT 1",
        {"actor_id": str(user_id)},
    ) or _exists(
        db,
        "SELECT 1 FROM education_audit_events WHERE actor_kind='user' AND actor_id=:actor_id LIMIT 1",
        {"actor_id": str(user_id)},
    ):
        raise HTTPException(
            status_code=409,
            detail="User has Education history; use the GDPR erasure workflow",
        )


def assert_org_hard_delete_allowed(db: Session, org_id: int) -> None:
    tables = (
        "education_memberships",
        "education_cost_centers",
        "education_cost_center_grants",
        "education_cost_center_printers",
        "education_commands",
        "education_upload_operations",
        "education_submissions",
        "education_audit_events",
        "education_notification_outbox",
        "education_rate_counters",
        "education_storage_accounts",
        "education_monitor_claims",
    )
    for table_name in tables:
        if _exists(
            db,
            f"SELECT 1 FROM {table_name} WHERE org_id=:id LIMIT 1",  # nosec B608 -- immutable allowlist
            {"id": org_id},
        ):
            raise HTTPException(
                status_code=409,
                detail="Organization has Education history and cannot be hard deleted",
            )


def assert_printer_tenant_change_or_delete_allowed(db: Session, printer_id: int) -> None:
    if _exists(
        db,
        "SELECT 1 FROM education_cost_center_printers WHERE printer_id=:id LIMIT 1",
        {"id": printer_id},
    ) or _exists(
        db,
        "SELECT 1 FROM education_submissions WHERE approved_printer_id=:id LIMIT 1",
        {"id": printer_id},
    ) or _exists(
        db,
        "SELECT 1 FROM education_monitor_claims WHERE printer_id=:id LIMIT 1",
        {"id": printer_id},
    ):
        raise HTTPException(
            status_code=409,
            detail="Printer has Education entitlement or submission history",
        )


def printer_is_currently_entitled(
    db: Session, *, org_id: int, cost_center_id: int, printer_id: int
) -> bool:
    return _exists(
        db,
        "SELECT 1 FROM education_cost_center_printers e "
        "JOIN education_cost_centers c ON c.id=e.cost_center_id AND c.org_id=e.org_id "
        "JOIN printers p ON p.id=e.printer_id AND p.org_id=e.org_id "
        "WHERE e.org_id=:org_id AND e.cost_center_id=:center_id "
        "AND e.printer_id=:printer_id AND e.state='active' AND c.state='active' "
        "AND p.is_active IS TRUE AND (p.shared IS NULL OR p.shared IS FALSE)",
        {
            "org_id": org_id,
            "center_id": cost_center_id,
            "printer_id": printer_id,
        },
    )


def scheduler_context(db: Session, *, job_id: int) -> dict | None:
    """Resolve current scheduling authority for an Education-owned job."""
    row = db.execute(
        text(
            "SELECT s.id,s.org_id,s.cost_center_id,s.print_file_id,s.submitted_by,s.job_id,s.status,"
            "s.lifecycle_revision,s.approved_printer_id,j.status AS job_status,"
            "j.printer_id AS job_printer_id FROM education_submissions s "
            "JOIN jobs j ON j.id=s.job_id AND j.charged_to_org_id=s.org_id "
            "WHERE s.job_id=:job_id"
        ),
        {"job_id": job_id},
    ).fetchone()
    if not row:
        return None
    context = dict(row._mapping)
    matching_state = (
        context["status"] == "pending" and context["job_status"] == "pending"
    ) or (
        context["status"] == "scheduled" and context["job_status"] == "scheduled"
    )
    printer_id = context["approved_printer_id"]
    context["authorized"] = bool(
        matching_state
        and printer_id is not None
        and context["job_printer_id"] == printer_id
        and printer_is_currently_entitled(
            db,
            org_id=int(context["org_id"]),
            cost_center_id=int(context["cost_center_id"]),
            printer_id=int(printer_id),
        )
    )
    return context


def _record_scheduler_transition(
    db: Session,
    *,
    context: dict,
    action: str,
    from_status: str,
    to_status: str,
    revision: int,
    reason: str | None = None,
) -> None:
    details = {"from_status": from_status, "to_status": to_status}
    if reason:
        details["reason"] = str(reason)[:160]
    result = {
        "id": int(context["id"]),
        "job_id": int(context["job_id"]),
        "status": to_status,
        "approved_printer_id": (
            int(context["approved_printer_id"])
            if to_status != "submitted" and context["approved_printer_id"] is not None
            else None
        ),
        "lifecycle_revision": revision,
    }
    command_id = f"scheduler:{context['job_id']}:{context['lifecycle_revision']}:{to_status}"
    request_hash = hashlib.sha256(
        json.dumps(details, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    event_id = str(uuid.uuid4())
    db.execute(
        text(
            "INSERT INTO education_audit_events "
            "(event_id,org_id,actor_kind,actor_id,action,command_id,request_hash,resource_type,"
            "resource_id,cost_center_id,lifecycle_revision,details_json,result_json) "
            "VALUES (:event_id,:org_id,'system','scheduler',:action,:command_id,:request_hash,"
            "'submission',:resource_id,:center_id,:revision,:details,:result)"
        ),
        {
            "event_id": event_id,
            "org_id": context["org_id"],
            "action": action,
            "command_id": command_id,
            "request_hash": request_hash,
            "resource_id": str(context["id"]),
            "center_id": context["cost_center_id"],
            "revision": revision,
            "details": json.dumps(details, sort_keys=True),
            "result": json.dumps(result, sort_keys=True),
        },
    )
    db.execute(
        text(
            "INSERT INTO education_notification_outbox (event_id,org_id,recipient_user_id) "
            "SELECT :event_id,:org_id,u.id FROM users u WHERE u.id=:user_id "
            "AND u.group_id=:org_id AND u.is_active IS TRUE "
            "ON CONFLICT (event_id,recipient_user_id) DO NOTHING"
        ),
        {
            "event_id": event_id,
            "org_id": context["org_id"],
            "user_id": context["submitted_by"],
        },
    )


class _ScheduleTransitionMiss(RuntimeError):
    """Internal control flow used to roll back only a scheduler savepoint."""


def advance_schedule(
    db: Session,
    *,
    submission_id: int,
    job_id: int,
    printer_id: int,
    expected_revision: int,
) -> bool:
    """CAS pending to scheduled and record it without owning the transaction."""
    context = scheduler_context(db, job_id=job_id)
    if (
        not context
        or int(context["id"]) != submission_id
        or not context["authorized"]
        or context["status"] != "pending"
    ):
        return False
    try:
        with db.begin_nested():
            job_changed = transition_result(db, job_id, "scheduled", "education",
                from_states=("pending",),
                where="charged_to_org_id=:org_id AND printer_id=:printer_id "
                      "AND NOT EXISTS (SELECT 1 FROM printer_dispatch_claims dc WHERE dc.printer_id=:printer_id)",
                params={"org_id": context["org_id"], "printer_id": printer_id})
            if job_changed.rowcount != 1:
                raise _ScheduleTransitionMiss
            changed = db.execute(
                text(
                    "UPDATE education_submissions SET status='scheduled',"
                    "lifecycle_revision=lifecycle_revision+1,updated_at=CURRENT_TIMESTAMP "
                    "WHERE id=:submission_id AND job_id=:job_id AND status='pending' "
                    "AND lifecycle_revision=:revision AND approved_printer_id=:printer_id "
                    "AND NOT EXISTS (SELECT 1 FROM education_monitor_claims mc "
                    "WHERE mc.submission_id=education_submissions.id OR mc.job_id=:job_id) "
                    "AND EXISTS (SELECT 1 FROM education_cost_center_printers e "
                    "JOIN education_cost_centers c ON c.id=e.cost_center_id AND c.org_id=e.org_id "
                    "JOIN printers p ON p.id=e.printer_id AND p.org_id=e.org_id "
                    "WHERE e.org_id=education_submissions.org_id "
                    "AND e.cost_center_id=education_submissions.cost_center_id "
                    "AND e.printer_id=:printer_id AND e.state='active' AND c.state='active' "
                    "AND p.is_active IS TRUE AND p.shared IS NOT TRUE)"
                ),
                {
                    "submission_id": submission_id,
                    "job_id": job_id,
                    "revision": expected_revision,
                    "printer_id": printer_id,
                },
            )
            if changed.rowcount != 1:
                raise _ScheduleTransitionMiss
            context["job_id"] = job_id
            _record_scheduler_transition(
                db,
                context=context,
                action="submission.schedule",
                from_status="pending",
                to_status="scheduled",
                revision=expected_revision + 1,
            )
    except _ScheduleTransitionMiss:
        return False
    return True


def reset_stale_schedule(
    db: Session,
    *,
    submission_id: int,
    job_id: int,
    printer_id: int,
    expected_revision: int,
) -> bool:
    """CAS a still-authorized stale schedule back to pending without committing."""
    context = scheduler_context(db, job_id=job_id)
    if not context or int(context["id"]) != submission_id or not context["authorized"]:
        return False
    try:
        with db.begin_nested():
            changed = db.execute(
                text(
                    "UPDATE education_submissions SET status='pending',"
                    "lifecycle_revision=lifecycle_revision+1,updated_at=CURRENT_TIMESTAMP "
                    "WHERE id=:submission_id AND job_id=:job_id AND status='scheduled' "
                    "AND lifecycle_revision=:revision AND approved_printer_id=:printer_id "
                    "AND NOT EXISTS (SELECT 1 FROM education_monitor_claims mc "
                    "WHERE mc.submission_id=education_submissions.id OR mc.job_id=:job_id)"
                ),
                {
                    "submission_id": submission_id,
                    "job_id": job_id,
                    "revision": expected_revision,
                    "printer_id": printer_id,
                },
            )
            if changed.rowcount != 1:
                raise _ScheduleTransitionMiss
            job_changed = transition_result(db, job_id, "pending", "education",
                from_states=("scheduled",),
                extra_fields={"scheduled_start": None, "scheduled_end": None, "match_score": None},
                where="printer_id=:printer_id", params={"printer_id": printer_id})
            if job_changed.rowcount != 1:
                raise _ScheduleTransitionMiss
            context["job_id"] = job_id
            _record_scheduler_transition(
                db,
                context=context,
                action="submission.schedule_reset",
                from_status="scheduled",
                to_status="pending",
                revision=expected_revision + 1,
            )
    except _ScheduleTransitionMiss:
        return False
    return True


def reconcile_schedule_denial(
    db: Session,
    *,
    submission_id: int,
    job_id: int,
    expected_revision: int,
    reason: str,
) -> bool:
    """Return policy/compatibility drift to submitted without committing."""
    context = scheduler_context(db, job_id=job_id)
    if not context or int(context["id"]) != submission_id:
        return False
    sanitized = str(reason or "scheduler_policy_denied")[:160]
    try:
        with db.begin_nested():
            changed = db.execute(
                text(
                    "UPDATE education_submissions SET status='submitted',approved_printer_id=NULL,"
                    "approved_by=NULL,compatibility_engine_version=NULL,"
                    "lifecycle_revision=lifecycle_revision+1,updated_at=CURRENT_TIMESTAMP "
                    "WHERE id=:submission_id AND job_id=:job_id AND lifecycle_revision=:revision "
                    "AND status IN ('pending','scheduled') "
                    "AND NOT EXISTS (SELECT 1 FROM education_monitor_claims mc "
                    "WHERE mc.submission_id=education_submissions.id OR mc.job_id=:job_id)"
                ),
                {
                    "submission_id": submission_id,
                    "job_id": job_id,
                    "revision": expected_revision,
                },
            )
            if changed.rowcount != 1:
                raise _ScheduleTransitionMiss
            job_changed = transition_result(db, job_id, "submitted", "education",
                from_states=("pending", "scheduled"),
                extra_fields={"printer_id": None, "scheduled_start": None, "scheduled_end": None,
                              "match_score": None,
                              "notes": Expr("CASE WHEN notes IS NULL OR notes='' THEN :reason "
                                            "ELSE notes || '\n' || :reason END",
                                            reason=f"Education scheduler reset: {sanitized}")})
            if job_changed.rowcount != 1:
                raise _ScheduleTransitionMiss
            context["job_id"] = job_id
            _record_scheduler_transition(
                db,
                context=context,
                action="submission.schedule_reconcile",
                from_status=context["status"],
                to_status="submitted",
                revision=expected_revision + 1,
                reason=sanitized,
            )
    except _ScheduleTransitionMiss:
        return False
    return True


def authorize_dispatch(
    db: Session, *, job_id: int, printer_id: int, expected_revision: int
) -> dict | None:
    """Re-resolve the current Education lifecycle immediately before dispatch."""
    row = db.execute(
        text(
            "SELECT s.id, s.org_id, s.cost_center_id, s.status, "
            "s.lifecycle_revision, s.approved_printer_id, j.status AS job_status, "
            "j.printer_id AS job_printer_id "
            "FROM education_submissions s "
            "JOIN jobs j ON j.id=s.job_id AND j.charged_to_org_id=s.org_id "
            "WHERE s.job_id=:job_id"
        ),
        {"job_id": job_id},
    ).fetchone()
    if not row:
        return None
    context = dict(row._mapping)
    context["authorized"] = bool(
        int(context["lifecycle_revision"]) == int(expected_revision)
        and context["status"] == "scheduled"
        and context["job_status"] == "scheduled"
        and context["approved_printer_id"] == printer_id
        and context["job_printer_id"] == printer_id
        and printer_is_currently_entitled(
            db,
            org_id=int(context["org_id"]),
            cost_center_id=int(context["cost_center_id"]),
            printer_id=printer_id,
        )
    )
    return context


def reconcile_dispatch_denial(
    db: Session,
    *,
    submission_id: int,
    job_id: int,
    expected_revision: int,
    reason: str,
) -> bool:
    """CAS a drifted approval back to submitted without silently reassigning it."""
    sanitized_reason = str(reason or "dispatch_policy_denied")[:160]
    result = db.execute(
        text(
            "UPDATE education_submissions SET status='submitted', "
            "approved_printer_id=NULL, approved_by=NULL, "
            "compatibility_engine_version=NULL, "
            "lifecycle_revision=lifecycle_revision+1, updated_at=CURRENT_TIMESTAMP "
            "WHERE id=:submission_id AND job_id=:job_id "
            "AND lifecycle_revision=:revision AND status IN ('pending','scheduled')"
        ),
        {
            "submission_id": submission_id,
            "job_id": job_id,
            "revision": expected_revision,
        },
    )
    if result.rowcount != 1:
        db.rollback()
        return False
    # The submission and its job move together: if the job is no longer
    # pending/scheduled/printing, undo the submission change as well.
    if not transition_job(db, job_id, "submitted", "education", extra_fields={
            "printer_id": None,
            "notes": Expr("CASE WHEN notes IS NULL OR notes='' THEN :reason ELSE notes || '\n' || :reason END",
                          reason=f"Education dispatch reset: {sanitized_reason}")}):
        db.rollback()
        return False
    db.commit()
    return True


def _record_monitor_transition(
    executor: _ExecutionAdapter,
    *,
    context: dict,
    action: str,
    revision: int,
    details: dict,
    result: dict,
) -> None:
    """Write the only notification evidence produced by Education monitoring."""
    event_id = str(uuid.uuid4())
    claim_id = str(context.get("claim_id") or "unclaimed")
    command_id = f"monitor:{claim_id}:{action}"[:64]
    request_hash = hashlib.sha256(
        json.dumps(details, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    executor.execute(
        "INSERT INTO education_audit_events "
        "(event_id,org_id,actor_kind,actor_id,action,command_id,request_hash,resource_type,"
        "resource_id,cost_center_id,lifecycle_revision,details_json,result_json) "
        "VALUES (:event_id,:org_id,'system','monitor',:action,:command_id,:request_hash,"
        "'submission',:resource_id,:center_id,:revision,:details,:result)",
        {
            "event_id": event_id,
            "org_id": context["org_id"],
            "action": action,
            "command_id": command_id,
            "request_hash": request_hash,
            "resource_id": str(context["submission_id"]),
            "center_id": context["cost_center_id"],
            "revision": revision,
            "details": json.dumps(details, sort_keys=True),
            "result": json.dumps(result, sort_keys=True),
        },
    )
    executor.execute(
        "INSERT INTO education_notification_outbox (event_id,org_id,recipient_user_id) "
        "SELECT :event_id,:org_id,u.id FROM users u WHERE u.id=:user_id "
        "AND u.group_id=:org_id AND u.is_active IS TRUE "
        "ON CONFLICT (event_id,recipient_user_id) DO NOTHING",
        {
            "event_id": event_id,
            "org_id": context["org_id"],
            "user_id": context["submitted_by"],
        },
    )


def reserve_dispatch(
    executor,
    *,
    job_id: int,
    printer_id: int,
    expected_revision: int,
    extension: str,
) -> dict | None:
    """Reserve an opaque physical correlation token before hardware is called."""
    normalized_extension = str(extension or "").lower()
    if normalized_extension and not normalized_extension.startswith("."):
        normalized_extension = f".{normalized_extension}"
    if normalized_extension not in _ALLOWED_REMOTE_EXTENSIONS:
        return None

    adapter = _ExecutionAdapter(executor)
    token = secrets.token_hex(16)
    claim_id = str(uuid.uuid4())
    digest = education_token_digest(token)
    context: dict | None = None
    try:
        with adapter.savepoint():
            submission_locked = adapter.execute(
                "UPDATE education_submissions SET updated_at=updated_at "
                "WHERE job_id=:job_id AND status='scheduled' "
                "AND lifecycle_revision=:revision AND approved_printer_id=:printer_id "
                "AND EXISTS (SELECT 1 FROM education_cost_center_printers e "
                "JOIN education_cost_centers c ON c.id=e.cost_center_id AND c.org_id=e.org_id "
                "JOIN printers p ON p.id=e.printer_id AND p.org_id=e.org_id "
                "WHERE e.org_id=education_submissions.org_id "
                "AND e.cost_center_id=education_submissions.cost_center_id "
                "AND e.printer_id=:printer_id AND e.state='active' AND c.state='active' "
                "AND p.is_active IS TRUE AND p.shared IS NOT TRUE)",
                {
                    "job_id": job_id,
                    "revision": expected_revision,
                    "printer_id": printer_id,
                },
            )
            if submission_locked.rowcount != 1:
                raise _MonitorTransitionMiss
            context = adapter.fetchone(
                "SELECT id AS submission_id,org_id,cost_center_id,submitted_by,job_id,"
                "lifecycle_revision,approved_printer_id FROM education_submissions "
                "WHERE job_id=:job_id",
                {"job_id": job_id},
            )
            if not context:
                raise _MonitorTransitionMiss
            job_locked = adapter.execute(
                "UPDATE jobs SET status=status WHERE id=:job_id AND charged_to_org_id=:org_id "
                "AND status='scheduled' AND printer_id=:printer_id",
                {
                    "job_id": job_id,
                    "org_id": context["org_id"],
                    "printer_id": printer_id,
                },
            )
            if job_locked.rowcount != 1:
                raise _MonitorTransitionMiss
            adapter.execute(
                "INSERT INTO education_monitor_claims "
                "(claim_id,org_id,submission_id,job_id,printer_id,authority_revision,token_digest,state) "
                "VALUES (:claim_id,:org_id,:submission_id,:job_id,:printer_id,:revision,:digest,'reserved')",
                {
                    "claim_id": claim_id,
                    "org_id": context["org_id"],
                    "submission_id": context["submission_id"],
                    "job_id": job_id,
                    "printer_id": printer_id,
                    "revision": expected_revision,
                    "digest": digest,
                },
            )
    except _MonitorTransitionMiss:
        return None
    except Exception as exc:
        if _is_integrity_error(exc):
            return None
        raise

    assert context is not None
    return {
        **context,
        "education_owned": True,
        "authorized": True,
        "claim_id": claim_id,
        "token": token,
        "remote_filename": f"odin-{token}{normalized_extension}",
        "printer_id": printer_id,
        "authority_revision": expected_revision,
    }


def lock_dispatch_reservation(executor, *, reservation: dict) -> bool:
    """Lock center then submission/job/claim and validate the exact reservation.

    Caller owns commit/rollback. NOWAIT avoids waiting across a conflicting
    resource order on PostgreSQL; SQLite takes its bounded engine write lock.
    """
    if not isinstance(executor, (Session, Connection)) or not isinstance(reservation, dict):
        return False
    fields=('submission_id','job_id','printer_id','authority_revision','cost_center_id','org_id')
    if any(type(reservation.get(name)) is not int or reservation[name]<=0 for name in fields):
        return False
    token=reservation.get('token')
    claim_id=reservation.get('claim_id')
    if not isinstance(token,str) or not re.fullmatch('[0-9a-f]{32}',token) or not isinstance(claim_id,str):
        return False
    postgres=executor.get_bind().dialect.name=='postgresql' if isinstance(executor,Session) else executor.dialect.name=='postgresql'
    locks=(
        ('education_cost_centers','id=:cost_center_id AND org_id=:org_id','revision'),
        ('education_cost_center_printers','cost_center_id=:cost_center_id AND printer_id=:printer_id AND org_id=:org_id','state'),
        ('education_submissions','id=:submission_id AND job_id=:job_id AND org_id=:org_id','updated_at'),
        ('jobs','id=:job_id','status'),
        ('education_monitor_claims','claim_id=:claim_id','updated_at'),
    )
    # Identifiers below are internal constants, never request text.
    for table,where,column in locks:
        sql=f'SELECT 1 FROM {table} WHERE {where} FOR UPDATE NOWAIT' if postgres else f'UPDATE {table} SET {column}={column} WHERE {where} RETURNING 1'
        if executor.execute(text(sql),reservation).first() is None:return False  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- table/predicate/column iterate the literal locks tuple in this function (closed set); values are bound
    context=authorize_dispatch(executor,job_id=reservation['job_id'],printer_id=reservation['printer_id'],expected_revision=reservation['authority_revision'])
    if not context or not context.get('authorized') or context['id']!=reservation['submission_id'] or context['org_id']!=reservation['org_id'] or context['cost_center_id']!=reservation['cost_center_id']:
        return False
    matching=executor.execute(text('SELECT 1 FROM education_monitor_claims WHERE claim_id=:claim_id '
        "AND state='reserved' AND submission_id=:submission_id AND job_id=:job_id AND printer_id=:printer_id "
        'AND org_id=:org_id AND authority_revision=:authority_revision AND token_digest=:digest'),
        {**reservation,'digest':education_token_digest(token)}).first()
    return matching is not None


def cancel_dispatch_reservation(
    executor,
    *,
    claim_id: str,
    submission_id: int,
    job_id: int,
    printer_id: int,
    expected_revision: int,
) -> bool:
    """Cancel only the still-current reservation created by this dispatch attempt."""
    adapter = _ExecutionAdapter(executor)
    try:
        with adapter.savepoint():
            submission_locked = adapter.execute(
                "UPDATE education_submissions SET updated_at=updated_at "
                "WHERE id=:submission_id AND job_id=:job_id AND status='scheduled' "
                "AND lifecycle_revision=:revision AND approved_printer_id=:printer_id",
                {
                    "submission_id": submission_id,
                    "job_id": job_id,
                    "revision": expected_revision,
                    "printer_id": printer_id,
                },
            )
            if submission_locked.rowcount != 1:
                raise _MonitorTransitionMiss
            job_locked = adapter.execute(
                "UPDATE jobs SET status=status WHERE id=:job_id AND status='scheduled' "
                "AND printer_id=:printer_id",
                {"job_id": job_id, "printer_id": printer_id},
            )
            if job_locked.rowcount != 1:
                raise _MonitorTransitionMiss
            deleted = adapter.execute(
                "DELETE FROM education_monitor_claims WHERE claim_id=:claim_id "
                "AND submission_id=:submission_id AND job_id=:job_id "
                "AND printer_id=:printer_id AND authority_revision=:revision "
                "AND state='reserved'",
                {
                    "claim_id": claim_id,
                    "submission_id": submission_id,
                    "job_id": job_id,
                    "printer_id": printer_id,
                    "revision": expected_revision,
                },
            )
            if deleted.rowcount != 1:
                raise _MonitorTransitionMiss
    except _MonitorTransitionMiss:
        return False
    return True


def confirm_dispatch_started(
    executor,
    *,
    claim_id: str,
    submission_id: int,
    job_id: int,
    printer_id: int,
    expected_revision: int,
) -> dict:
    """Atomically confirm scheduled authority after the hardware accepted the print."""
    adapter = _ExecutionAdapter(executor)
    context = adapter.fetchone(
        "SELECT s.id AS submission_id,s.org_id,s.cost_center_id,s.submitted_by,s.job_id,"
        "mc.claim_id FROM education_submissions s JOIN education_monitor_claims mc "
        "ON mc.submission_id=s.id AND mc.org_id=s.org_id WHERE mc.claim_id=:claim_id",
        {"claim_id": claim_id},
    )
    if not context or int(context["submission_id"]) != int(submission_id):
        return {"education_owned": True, "authorized": False, "transitioned": False}
    revision = expected_revision + 1
    try:
        with adapter.savepoint():
            submission_changed = adapter.execute(
                "UPDATE education_submissions SET status='printing',"
                "lifecycle_revision=lifecycle_revision+1,updated_at=CURRENT_TIMESTAMP "
                "WHERE id=:submission_id AND job_id=:job_id AND org_id=:org_id "
                "AND status='scheduled' AND lifecycle_revision=:revision "
                "AND approved_printer_id=:printer_id",
                {
                    "submission_id": submission_id,
                    "job_id": job_id,
                    "org_id": context["org_id"],
                    "revision": expected_revision,
                    "printer_id": printer_id,
                },
            )
            if submission_changed.rowcount != 1:
                raise _MonitorTransitionMiss
            job_changed = transition_result(adapter.executor, job_id, "printing", "education",
                from_states=("scheduled",),
                extra_fields={"actual_start": Expr("COALESCE(actual_start,CURRENT_TIMESTAMP)")},
                where="charged_to_org_id=:org_id AND printer_id=:printer_id",
                params={"org_id": context["org_id"], "printer_id": printer_id})
            if job_changed.rowcount != 1:
                raise _MonitorTransitionMiss
            claim_changed = adapter.execute(
                "UPDATE education_monitor_claims SET state='awaiting',authority_revision=:next_revision,"
                "updated_at=CURRENT_TIMESTAMP WHERE claim_id=:claim_id AND submission_id=:submission_id "
                "AND job_id=:job_id AND printer_id=:printer_id AND authority_revision=:revision "
                "AND state='reserved'",
                {
                    "claim_id": claim_id,
                    "submission_id": submission_id,
                    "job_id": job_id,
                    "printer_id": printer_id,
                    "revision": expected_revision,
                    "next_revision": revision,
                },
            )
            if claim_changed.rowcount != 1:
                raise _MonitorTransitionMiss
            result = {
                "education_owned": True,
                "authorized": True,
                "transitioned": True,
                "submission_id": submission_id,
                "job_id": job_id,
                "lifecycle_revision": revision,
            }
            _record_monitor_transition(
                adapter,
                context=context,
                action="submission.dispatch_started",
                revision=revision,
                details={"from_status": "scheduled", "to_status": "printing"},
                result=result,
            )
    except _MonitorTransitionMiss:
        return {
            "education_owned": True,
            "authorized": False,
            "transitioned": False,
            "submission_id": submission_id,
            "job_id": job_id,
        }
    return result


def start_identity_matches(context, *identities):
    """Use the existing canonical multi-identity conflict resolver."""
    token, _, conflict = _resolve_monitor_tokens(*identities)
    return (not conflict and token is not None and isinstance(context, dict)
            and education_token_digest(token) == context.get('token_digest'))


def discover_start_authority(executor, *, printer_id, observed_filename):
    """Read canonical token candidate only; not authority without later locks."""
    token = parse_education_token(observed_filename)
    if token is None:
        return None
    return _ExecutionAdapter(executor).fetchone(
        "SELECT mc.claim_id,mc.org_id,mc.submission_id,mc.job_id,mc.printer_id,"
        "mc.authority_revision,mc.state,mc.token_digest,s.cost_center_id,s.submitted_by "
        "FROM education_monitor_claims mc JOIN education_submissions s "
        "ON s.id=mc.submission_id AND s.org_id=mc.org_id "
        "WHERE mc.token_digest=:digest AND mc.printer_id=:printer_id",
        {'digest': education_token_digest(token), 'printer_id': printer_id})


def recheck_start_authority(executor, context):
    """Exact reserved/awaiting/running predicates; no observation row required."""
    if not isinstance(context, dict):
        return False
    fields = ('org_id','submission_id','job_id','printer_id','authority_revision','cost_center_id')
    if any(type(context.get(name)) is not int or context[name] <= 0 for name in fields):
        return False
    state = context.get('state')
    if state not in ('reserved','awaiting','running'):
        return False
    expected = 'scheduled' if state == 'reserved' else 'printing'
    return _ExecutionAdapter(executor).fetchone(
        "SELECT 1 FROM education_monitor_claims mc JOIN education_submissions s "
        "ON s.id=mc.submission_id AND s.org_id=mc.org_id JOIN jobs j ON j.id=mc.job_id "
        "AND j.charged_to_org_id=mc.org_id JOIN education_cost_centers c "
        "ON c.id=s.cost_center_id AND c.org_id=s.org_id "
        "JOIN education_cost_center_printers g ON g.org_id=c.org_id AND g.cost_center_id=c.id "
        "AND g.printer_id=mc.printer_id JOIN printers p ON p.id=mc.printer_id AND p.org_id=mc.org_id "
        "WHERE mc.claim_id=:claim_id AND mc.org_id=:org_id AND mc.submission_id=:submission_id "
        "AND mc.job_id=:job_id AND mc.printer_id=:printer_id AND mc.authority_revision=:authority_revision "
        "AND mc.token_digest=:token_digest AND mc.state=:state AND s.job_id=mc.job_id "
        "AND s.lifecycle_revision=mc.authority_revision AND s.status=:expected "
        "AND s.cost_center_id=:cost_center_id AND s.submitted_by=:submitted_by "
        "AND s.approved_printer_id=mc.printer_id AND j.printer_id=mc.printer_id "
        "AND CAST(j.status AS TEXT)=:expected AND c.state='active' AND g.state='active' "
        "AND p.is_active IS TRUE AND p.shared IS NOT TRUE", {**context,'expected':expected}) is not None


def lock_start_authority(executor, context):
    """Parent-first caller then center/grant/submission/job/claim acquisition."""
    if not isinstance(context,dict):
        return False
    fields = ('org_id','submission_id','job_id','printer_id','authority_revision','cost_center_id')
    if any(type(context.get(name)) is not int or context[name]<=0 for name in fields):
        return False
    from core.dispatch_observation_executor import TransactionView
    view=TransactionView(executor)
    for table,where,column in (
        ('education_cost_centers','id=:cost_center_id AND org_id=:org_id','revision'),
        ('education_cost_center_printers','cost_center_id=:cost_center_id AND printer_id=:printer_id AND org_id=:org_id','state'),
        ('education_submissions','id=:submission_id AND job_id=:job_id AND org_id=:org_id','updated_at'),
        ('jobs','id=:job_id','status'),
        ('education_monitor_claims','claim_id=:claim_id','updated_at'),
    ):
        statement=(f'SELECT 1 FROM {table} WHERE {where} FOR UPDATE NOWAIT' if view.dialect.name=='postgresql' else f'UPDATE {table} SET {column}={column} WHERE {where} RETURNING 1')
        if view.execute(text(statement),context).first() is None:  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- table/predicate/column iterate the literal locks tuple in this function (closed set); values are bound
            return False
    return recheck_start_authority(executor,context)


def finalize_start_authority(executor, *, scope, plan, print_job_id, context):
    """Only plan-owned fresh row or exact retained resume may widen link CAS."""
    from core.attempt_start_plan import _record
    from core.economic_source_creation import SourceCreationError
    record=_record(executor,scope,plan)
    created=record['created']
    expected_id=plan.resume_id if plan.branch in ('resume','legacy_resume') else (created.id if created else None)
    if expected_id!=print_job_id or record.get('education')!=context or plan.job_id!=context.get('job_id'):
        raise SourceCreationError('Education prepared capability differs')
    return claim_monitor_observation(executor,print_job_id=print_job_id,
        printer_id=context['printer_id'],observed_filename=(record['observed']['job_name'] if record['protocol']=='bambu' else record['observed']['filename']),
        _prepared_start=(scope,plan))


def claim_monitor_observation(
    executor,
    *,
    print_job_id: int,
    printer_id: int,
    observed_filename: str | None,
    _prepared_start=None,
) -> dict:
    """Claim an exact-token observation or quarantine the reserved namespace."""
    token = parse_education_token(observed_filename)
    adapter = _ExecutionAdapter(executor)
    if token is None:
        linked = adapter.fetchone(
            "SELECT s.id AS submission_id,s.job_id,s.lifecycle_revision "
            "FROM print_jobs p JOIN education_submissions s ON s.job_id=p.scheduled_job_id "
            "WHERE p.id=:print_job_id AND p.printer_id=:printer_id",
            {"print_job_id": print_job_id, "printer_id": printer_id},
        )
        if linked:
            return {
                "education_owned": True,
                "education_reserved": False,
                "authorized": False,
                "transitioned": False,
                **linked,
            }
        return {"education_owned": False, "education_reserved": False, "authorized": False}

    observation = adapter.fetchone(
        "SELECT filename,job_name FROM print_jobs WHERE id=:print_job_id "
        "AND printer_id=:printer_id AND status='running'",
        {"print_job_id": print_job_id, "printer_id": printer_id},
    )
    persisted_token = (
        parse_education_token(observation.get("filename"))
        or parse_education_token(observation.get("job_name"))
        if observation
        else None
    )
    if persisted_token != token:
        return {
            "education_owned": False,
            "education_reserved": True,
            "authorized": False,
            "transitioned": False,
        }

    digest = education_token_digest(token)
    context = adapter.fetchone(
        "SELECT mc.claim_id,mc.org_id,mc.submission_id,mc.job_id,mc.printer_id,"
        "mc.authority_revision,mc.state,mc.token_digest,s.cost_center_id,s.submitted_by "
        "FROM education_monitor_claims mc JOIN education_submissions s "
        "ON s.id=mc.submission_id AND s.org_id=mc.org_id "
        "WHERE mc.token_digest=:digest AND mc.printer_id=:printer_id",
        {"digest": digest, "printer_id": printer_id},
    )
    if not context:
        return {
            "education_owned": False,
            "education_reserved": True,
            "authorized": False,
            "transitioned": False,
        }

    revision = int(context["authority_revision"])
    claim_state = context["state"]
    if claim_state == "running":
        from core.dispatch_observation_executor import TransactionView
        view=TransactionView(executor)
        # Resume revalidates the same authority order as the transition path.
        for table,where,column in (
            ('education_submissions','id=:submission_id AND org_id=:org_id','updated_at'),
            ('jobs','id=:job_id','status'),
            ('education_monitor_claims','claim_id=:claim_id','updated_at'),
            ('print_jobs','id=:print_job_id AND printer_id=:printer_id','status'),
        ):
            statement=(f'SELECT 1 FROM {table} WHERE {where} FOR UPDATE NOWAIT' if view.dialect.name=='postgresql' else f'UPDATE {table} SET {column}={column} WHERE {where} RETURNING 1')
            if view.execute(text(statement),{**context,'print_job_id':print_job_id}).first() is None:  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- table/predicate/column iterate the literal locks tuple in this function (closed set); values are bound
                return {'education_owned':True,'education_reserved':True,'authorized':False,'transitioned':False}
        linked = adapter.fetchone(
            "SELECT id FROM print_jobs WHERE id=:print_job_id AND printer_id=:printer_id "
            "AND scheduled_job_id=:job_id AND status='running'",
            {
                "print_job_id": print_job_id,
                "printer_id": printer_id,
                "job_id": context["job_id"],
            },
        )
        from core.dispatch_observations import education_observation_authority
        linked=bool(linked) and education_observation_authority(view,{**context,'authority_revision':revision},print_job_id)
        result = {
            "education_owned": True,
            "education_reserved": True,
            "dispatch_receipt_binding": 'refused',
            "authorized": bool(linked),
            "transitioned": False,
            "idempotent": bool(linked),
            "submission_id": context["submission_id"],
            "job_id": context["job_id"],
            "lifecycle_revision": revision,
        }
        if linked:
            from core.dispatch_observations import bind_education_observation
            from core import auth
            result['dispatch_receipt_binding']=bind_education_observation(executor,print_job_id=print_job_id,context={**context,'authority_revision':revision},key=auth.SECRET_KEY.encode())
        return result

    link_guard = 'scheduled_job_id IS NULL'
    if _prepared_start is not None:
        from core.attempt_start_plan import _record
        from core.economic_source_creation import SourceCreationError
        scope, plan = _prepared_start
        record = _record(executor, scope, plan)
        created = record['created']
        if created is None or created.id != print_job_id or record.get('education') != context or plan.job_id != context['job_id']:
            raise SourceCreationError('Education prepared link differs')
        link_guard = '(scheduled_job_id IS NULL OR scheduled_job_id=:job_id)'

    recovered = claim_state == "reserved"
    expected_submission_status = "scheduled" if recovered else "printing"
    expected_job_status = "scheduled" if recovered else "printing"
    next_revision = revision + 1 if recovered else revision
    try:
        with adapter.savepoint():
            if recovered:
                submission_changed = adapter.execute(
                    "UPDATE education_submissions SET status='printing',"
                    "lifecycle_revision=lifecycle_revision+1,updated_at=CURRENT_TIMESTAMP "
                    "WHERE id=:submission_id AND org_id=:org_id AND job_id=:job_id "
                    "AND status=:status AND lifecycle_revision=:revision "
                    "AND approved_printer_id=:printer_id",
                    {**context, "status": expected_submission_status, "revision": revision},
                )
            else:
                submission_changed = adapter.execute(
                    "UPDATE education_submissions SET updated_at=updated_at "
                    "WHERE id=:submission_id AND org_id=:org_id AND job_id=:job_id "
                    "AND status=:status AND lifecycle_revision=:revision "
                    "AND approved_printer_id=:printer_id",
                    {**context, "status": expected_submission_status, "revision": revision},
                )
            if submission_changed.rowcount != 1:
                raise _MonitorTransitionMiss
            if recovered:
                job_changed = transition_result(adapter.executor, context["job_id"], "printing", "education",
                    from_states=(expected_job_status,),
                    extra_fields={"actual_start": Expr("COALESCE(actual_start,CURRENT_TIMESTAMP)")},
                    where="charged_to_org_id=:org_id AND printer_id=:printer_id",
                    params={"org_id": context["org_id"], "printer_id": context["printer_id"]})
            else:
                job_changed = adapter.execute(
                    "UPDATE jobs SET status=status WHERE id=:job_id AND charged_to_org_id=:org_id "
                    "AND status=:status AND printer_id=:printer_id",
                    {**context, "status": expected_job_status},
                )
            if job_changed.rowcount != 1:
                raise _MonitorTransitionMiss
            claim_changed = adapter.execute(
                "UPDATE education_monitor_claims SET state='running',"
                "authority_revision=:next_revision,updated_at=CURRENT_TIMESTAMP "
                "WHERE claim_id=:claim_id AND state=:claim_state "
                "AND authority_revision=:revision AND printer_id=:printer_id",
                {
                    **context,
                    "claim_state": claim_state,
                    "revision": revision,
                    "next_revision": next_revision,
                },
            )
            if claim_changed.rowcount != 1:
                raise _MonitorTransitionMiss
            observation_changed = adapter.execute(
                "UPDATE print_jobs SET scheduled_job_id=:job_id "
                "WHERE id=:print_job_id AND printer_id=:printer_id "
                f"AND {link_guard} AND status='running'",
                {
                    "job_id": context["job_id"],
                    "print_job_id": print_job_id,
                    "printer_id": printer_id,
                },
            )
            if observation_changed.rowcount != 1:
                raise _MonitorTransitionMiss
            from core.print_attempts import bind_print_attempt
            bind_print_attempt(adapter.execute, print_job_id, context["job_id"])
            result = {
                "education_owned": True,
                "education_reserved": True,
                "authorized": True,
                "transitioned": True,
                "recovered": recovered,
                "submission_id": context["submission_id"],
                "job_id": context["job_id"],
                "lifecycle_revision": next_revision,
            }
            from core.dispatch_observations import bind_education_observation
            from core import auth
            result['dispatch_receipt_binding']=bind_education_observation(executor,print_job_id=print_job_id,context={**context,'authority_revision':next_revision},key=auth.SECRET_KEY.encode())
            _record_monitor_transition(
                adapter,
                context=context,
                action=(
                    "submission.dispatch_recovered" if recovered else "submission.print_observed"
                ),
                revision=next_revision,
                details={"claim_state": claim_state, "print_job_id": print_job_id},
                result=result,
            )
    except _MonitorTransitionMiss:
        return {
            "education_owned": True,
            "education_reserved": True,
            "authorized": False,
            "transitioned": False,
            "submission_id": context["submission_id"],
            "job_id": context["job_id"],
            "lifecycle_revision": revision,
        }
    return result


def classify_monitor_observation(
    executor,
    *,
    printer_id: int,
    print_job_id: int | None = None,
    observed_filename: str | None = None,
) -> dict:
    """Classify a monitor packet before any generic external side effects."""
    adapter = _ExecutionAdapter(executor)
    identities = [observed_filename]
    if print_job_id is not None:
        linked = adapter.fetchone(
            "SELECT s.id AS submission_id,s.job_id,s.lifecycle_revision,p.filename,p.job_name "
            "FROM print_jobs p LEFT JOIN education_submissions s ON s.job_id=p.scheduled_job_id "
            "WHERE p.id=:print_job_id AND p.printer_id=:printer_id",
            {"print_job_id": print_job_id, "printer_id": printer_id},
        )
        if linked:
            identities.extend((linked.get("filename"), linked.get("job_name")))
            _, reserved, conflict = _resolve_monitor_tokens(*identities)
            if conflict:
                return {"education_owned": False, "education_reserved": reserved, "authorized": False}
            if linked.get("submission_id") is not None:
                return {
                    "education_owned": True,
                    "education_reserved": True,
                    "authorized": True,
                    "submission_id": linked["submission_id"],
                    "job_id": linked["job_id"],
                    "lifecycle_revision": linked["lifecycle_revision"],
                }
    token, reserved, conflict = _resolve_monitor_tokens(*identities)
    if token is None or conflict:
        return {"education_owned": False, "education_reserved": reserved, "authorized": False}
    claim = adapter.fetchone(
        "SELECT submission_id,job_id,authority_revision FROM education_monitor_claims "
        "WHERE token_digest=:digest AND printer_id=:printer_id",
        {"digest": education_token_digest(token), "printer_id": printer_id},
    )
    if claim:
        return {
            "education_owned": True,
            "education_reserved": True,
            "authorized": True,
            "submission_id": claim["submission_id"],
            "job_id": claim["job_id"],
            "lifecycle_revision": claim["authority_revision"],
        }
    return {"education_owned": False, "education_reserved": True, "authorized": False}


def terminal_monitor_observation(
    executor,
    *,
    print_job_id: int,
    printer_id: int,
    terminal_status: str,
    ended_at=None,
    duration_seconds: float | None = None,
    error_code: str | None = None,
    observed_filename: str | None = None,
) -> dict:
    """Atomically apply terminal truth for an Education-owned observation."""
    if terminal_status not in {"completed", "failed", "cancelled"}:
        raise ValueError("Unsupported Education terminal status")
    sanitized_error = str(error_code)[:100] if error_code else None
    bounded_duration = (
        max(0.0, min(float(duration_seconds), 31_536_000.0))
        if duration_seconds is not None
        else None
    )
    adapter = _ExecutionAdapter(executor)
    context = adapter.fetchone(
        "SELECT p.id AS print_job_id,p.status AS print_status,p.scheduled_job_id,p.filename,p.job_name,"
        "s.id AS submission_id,s.org_id,s.cost_center_id,s.submitted_by,s.job_id,"
        "s.status AS submission_status,s.lifecycle_revision,s.approved_printer_id,"
        "j.status AS job_status,mc.claim_id,mc.state AS claim_state,"
        "mc.authority_revision,mc.token_digest FROM print_jobs p "
        "LEFT JOIN education_submissions s ON s.job_id=p.scheduled_job_id "
        "LEFT JOIN jobs j ON j.id=s.job_id AND j.charged_to_org_id=s.org_id "
        "LEFT JOIN education_monitor_claims mc ON mc.job_id=s.job_id AND mc.org_id=s.org_id "
        "WHERE p.id=:print_job_id AND p.printer_id=:printer_id",
        {"print_job_id": print_job_id, "printer_id": printer_id},
    )
    if not context:
        return {"education_owned": False, "education_reserved": False, "transitioned": False}
    _, reserved_identity, conflicting_identity = _resolve_monitor_tokens(
        context.get("filename"), context.get("job_name"), observed_filename)
    if conflicting_identity:
        return {"education_owned": context.get("submission_id") is not None,
                "education_reserved": reserved_identity, "authorized": False,
                "transitioned": False}
    if context.get("submission_id") is None:
        token, reserved, conflict = _resolve_monitor_tokens(
            context.get("filename"), context.get("job_name"), observed_filename)
        if not reserved:
            return {"education_owned": False, "education_reserved": False, "transitioned": False}
        if conflict or token is None:
            return {"education_owned": False, "education_reserved": True,
                    "authorized": False, "transitioned": False}
        if observed_filename is not None and parse_education_token(observed_filename) != token:
            return {"education_owned": False, "education_reserved": True,
                    "authorized": False, "transitioned": False}
        quarantined = adapter.execute(
            "UPDATE print_jobs SET status=:status,ended_at=COALESCE(:ended_at,CURRENT_TIMESTAMP),"
            "error_code=COALESCE(:error_code,error_code) WHERE id=:print_job_id "
            "AND printer_id=:printer_id AND scheduled_job_id IS NULL AND status='running' "
            "AND COALESCE(filename,'')=COALESCE(:observed_stored_filename,'') "
            "AND COALESCE(job_name,'')=COALESCE(:observed_stored_job_name,'')",
            {
                "status": terminal_status,
                "ended_at": ended_at,
                "error_code": sanitized_error,
                "print_job_id": print_job_id,
                "printer_id": printer_id,
                "observed_stored_filename": context.get("filename"),
                "observed_stored_job_name": context.get("job_name"),
            },
        )
        return {
            "education_owned": False,
            "education_reserved": True,
            "authorized": False,
            "transitioned": quarantined.rowcount == 1,
            "quarantined": quarantined.rowcount == 1,
        }

    base_result = {
        "education_owned": True,
        "education_reserved": True,
        "submission_id": context["submission_id"],
        "job_id": context["job_id"],
        "lifecycle_revision": context["lifecycle_revision"],
    }
    expected_digest = None
    if observed_filename is not None:
        observed_token = parse_education_token(observed_filename)
        expected_digest = education_token_digest(observed_token) if observed_token else None
        if expected_digest is None or expected_digest != context.get("token_digest"):
            return {**base_result, "transitioned": False, "idempotent": False, "authorized": False}
    if (
        context["submission_status"] == terminal_status
        and context["job_status"] == terminal_status
        and context["print_status"] == terminal_status
        and context.get("claim_id") is None
    ):
        return {
            **base_result,
            "transitioned": False,
            "idempotent": True,
            "authorized": True,
        }
    def physical_end() -> bool:
        # The job's Education authority refused this end before any change
        # (operator cancel, revoked claim, stale revision...), but the printer's
        # own linked print really ended: record that physical fact only. Submission, job and claim are
        # untouched; monitors publish the physical completion without the job.
        return adapter.execute(
            "UPDATE print_jobs SET status=:status,ended_at=COALESCE(:ended_at,CURRENT_TIMESTAMP),"
            "error_code=COALESCE(:error_code,error_code) WHERE id=:print_job_id "
            "AND printer_id=:printer_id AND scheduled_job_id=:job_id AND status='running'",
            {"status": terminal_status, "ended_at": ended_at, "error_code": sanitized_error,
             "print_job_id": print_job_id, "printer_id": printer_id, "job_id": context["job_id"]},
        ).rowcount == 1

    if (
        context.get("claim_id") is None
        or context.get("claim_state") != "running"
        or context["submission_status"] != "printing"
        or context["job_status"] != "printing"
        or int(context["authority_revision"]) != int(context["lifecycle_revision"])
        or int(context["approved_printer_id"]) != int(printer_id)
    ):
        return {
            **base_result,
            "transitioned": False,
            "idempotent": False,
            "authorized": False,
            "physical_ended": physical_end(),
        }

    revision = int(context["lifecycle_revision"])
    next_revision = revision + 1
    try:
        with adapter.savepoint():
            submission_changed = adapter.execute(
                "UPDATE education_submissions SET status=:status,"
                "lifecycle_revision=lifecycle_revision+1,updated_at=CURRENT_TIMESTAMP "
                "WHERE id=:submission_id AND org_id=:org_id AND job_id=:job_id "
                "AND status='printing' AND lifecycle_revision=:revision "
                "AND approved_printer_id=:printer_id",
                {**context, "status": terminal_status, "revision": revision, "printer_id": printer_id},
            )
            if submission_changed.rowcount != 1:
                raise _MonitorTransitionMiss
            job_changed = transition_result(adapter.executor, context["job_id"], terminal_status, "education",
                from_states=("printing",),
                extra_fields={"actual_end": Expr("COALESCE(:ended_at,CURRENT_TIMESTAMP)", ended_at=ended_at)},
                where="charged_to_org_id=:org_id AND printer_id=:printer_id",
                params={"org_id": context["org_id"], "printer_id": printer_id})
            if job_changed.rowcount != 1:
                raise _MonitorTransitionMiss
            claim_deleted = adapter.execute(
                "DELETE FROM education_monitor_claims WHERE claim_id=:claim_id "
                "AND submission_id=:submission_id AND job_id=:job_id AND printer_id=:printer_id "
                "AND authority_revision=:revision AND state='running' "
                "AND (CAST(:expected_digest AS TEXT) IS NULL OR token_digest=:expected_digest)",
                {**context, "revision": revision, "printer_id": printer_id, "expected_digest": expected_digest},
            )
            if claim_deleted.rowcount != 1:
                raise _MonitorTransitionMiss
            observation_changed = adapter.execute(
                "UPDATE print_jobs SET status=:status,ended_at=COALESCE(:ended_at,CURRENT_TIMESTAMP),"
                "error_code=COALESCE(:error_code,error_code) WHERE id=:print_job_id "
                "AND printer_id=:printer_id AND scheduled_job_id=:job_id AND status='running'",
                {
                    **context,
                    "status": terminal_status,
                    "ended_at": ended_at,
                    "error_code": sanitized_error,
                    "printer_id": printer_id,
                },
            )
            if observation_changed.rowcount != 1:
                raise _MonitorTransitionMiss
            result = {
                **base_result,
                "transitioned": True,
                "idempotent": False,
                "authorized": True,
                "lifecycle_revision": next_revision,
            }
            _record_monitor_transition(
                adapter,
                context=context,
                action=f"submission.print_{terminal_status}",
                revision=next_revision,
                details={
                    "from_status": "printing",
                    "to_status": terminal_status,
                    "duration_seconds": bounded_duration,
                    "error_code": sanitized_error,
                    "print_job_id": print_job_id,
                },
                result=result,
            )
    except _MonitorTransitionMiss:
        final = adapter.fetchone(
            "SELECT p.status AS print_status,s.status AS submission_status,"
            "s.lifecycle_revision,j.status AS job_status,mc.claim_id "
            "FROM print_jobs p JOIN education_submissions s ON s.job_id=p.scheduled_job_id "
            "JOIN jobs j ON j.id=s.job_id AND j.charged_to_org_id=s.org_id "
            "LEFT JOIN education_monitor_claims mc ON mc.job_id=s.job_id AND mc.org_id=s.org_id "
            "WHERE p.id=:print_job_id AND p.printer_id=:printer_id",
            {"print_job_id": print_job_id, "printer_id": printer_id},
        )
        if (
            final
            and final["submission_status"] == terminal_status
            and final["job_status"] == terminal_status
            and final["print_status"] == terminal_status
            and final.get("claim_id") is None
        ):
            return {
                **base_result,
                "transitioned": False,
                "idempotent": True,
                "authorized": True,
                "lifecycle_revision": final["lifecycle_revision"],
            }
        # Authority changed concurrently (e.g. the claim digest was replaced):
        # identity is uncertain, so nothing -- not even the physical end -- changes.
        return {
            **base_result,
            "transitioned": False,
            "idempotent": False,
            "authorized": False,
        }
    return result


def resolve_active_monitor_observation(executor, *, printer_id: int) -> dict:
    """Resolve the unique durable Education observation after monitor restart."""
    adapter = _ExecutionAdapter(executor)
    rows = adapter.fetchall(
        "SELECT p.id AS print_job_id,mc.submission_id,mc.job_id,mc.authority_revision "
        "FROM education_monitor_claims mc JOIN print_jobs p "
        "ON p.scheduled_job_id=mc.job_id AND p.printer_id=mc.printer_id "
        "WHERE mc.printer_id=:printer_id AND mc.state='running' AND p.status='running' "
        "ORDER BY p.id LIMIT 2",
        {"printer_id": printer_id},
    )
    if len(rows) > 1:
        return {
            "education_owned": True,
            "education_reserved": True,
            "authorized": False,
            "ambiguous": True,
        }
    if len(rows) == 1:
        return {
            "education_owned": True,
            "education_reserved": True,
            "authorized": True,
            **rows[0],
        }
    quarantine_rows = adapter.fetchall(
        "SELECT id AS print_job_id,filename,job_name FROM print_jobs "
        "WHERE printer_id=:printer_id AND scheduled_job_id IS NULL AND status='running' "
        "ORDER BY id DESC LIMIT 20",
        {"printer_id": printer_id},
    )
    quarantined = [
        row
        for row in quarantine_rows
        if _resolve_monitor_tokens(row.get("filename"), row.get("job_name"))[1]
    ]
    if len(quarantined) == 1:
        return {
            "education_owned": False,
            "education_reserved": True,
            "authorized": not _resolve_monitor_tokens(
                quarantined[0].get("filename"), quarantined[0].get("job_name"))[2],
            "print_job_id": quarantined[0]["print_job_id"],
        }
    return {
        "education_owned": False,
        "education_reserved": bool(quarantined),
        "authorized": False,
        "ambiguous": len(quarantined) > 1,
    }


def recovery_authority(db, *, evidence, principal, lock=False):
    """Recovery authority works for retained running claims, never dispatches.

    School administrators retain authority over every school center. Platform
    admins must use a school account; printer ownership alone grants no school
    authority. No lifecycle, reservation, quota or accounting changes occur.
    """
    from modules.organizations.education_access import _education_token_allowed
    if not _education_token_allowed(principal,write=True):
        return False
    actor = db.execute(text('SELECT id,role,group_id,is_active FROM users WHERE id=:id'),{'id':principal['id']}).mappings().first()
    if not actor or not actor['is_active'] or actor['role']!='admin' or actor['group_id'] is None:
        return False
    pg = db.get_bind().dialect.name=='postgresql'
    for submission in evidence['submissions']:
        if submission['org_id']!=actor['group_id'] or submission['approved_printer_id']!=evidence['printer']['id'] or submission['org_id']!=evidence['job']['charged_to_org_id']:
            return False
        args = {'org':submission['org_id'],'center':submission['cost_center_id'],'pid':evidence['printer']['id'],'sid':submission['id'],'jid':submission['job_id']}
        for query in (
            'SELECT id FROM education_cost_centers WHERE id=:center AND org_id=:org AND state=\'active\'',
            'SELECT printer_id FROM education_cost_center_printers WHERE cost_center_id=:center AND org_id=:org AND printer_id=:pid AND state=\'active\'',
            'SELECT id FROM education_submissions WHERE id=:sid AND job_id=:jid AND org_id=:org',
            'SELECT id FROM jobs WHERE id=:jid',
            'SELECT claim_id FROM education_monitor_claims WHERE submission_id=:sid AND org_id=:org AND job_id=:jid AND printer_id=:pid ORDER BY claim_id',
        ):
            if not db.execute(text(query+(' FOR UPDATE NOWAIT' if lock and pg else '')),args).first():  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- verified safe — constant query plus a fixed dialect suffix; values are bound
                # Terminal submissions may legitimately have no monitor claim.
                if not query.startswith('SELECT claim_id'): return False
    return bool(evidence['submissions'])


def recovery_terminal_resolved(db, *, evidence):
    """Require genuine monitor lifecycle history, not physical-only terminal."""
    if evidence['reservations'] or len(evidence['submissions'])!=1 or not evidence['claim']:
        return False
    submission = evidence['submissions'][0]
    job = evidence['job']
    claim = evidence['claim']
    if submission['status'] not in ('completed','failed','cancelled') or submission['status']!=job['status']:
        return False
    receipt = next((r for r in evidence['receipts'] if r['claim_token']==claim['token']),None)
    observation = next((o for o in evidence['observations'] if receipt and o['id']==receipt['observed_print_job_id'] and o['scheduled_job_id']==job['id']),None)
    if not observation or observation['status']!=job['status'] or not observation['ended_at'] or not job['actual_end']:
        return False
    events = db.execute(text("SELECT details_json,result_json FROM education_audit_events WHERE org_id=:org AND resource_type='submission' AND resource_id=:sid AND actor_kind='system' AND actor_id='monitor' AND action=:action AND lifecycle_revision=:rev"),
        {'org':submission['org_id'],'sid':str(submission['id']),'action':'submission.print_'+job['status'],'rev':submission['lifecycle_revision']}).mappings().all()
    if len(events)!=1:
        return False
    try:
        details=json.loads(events[0]['details_json']); result=json.loads(events[0]['result_json'])
        if details.get('print_job_id')!=observation['id'] or details.get('from_status')!='printing' or details.get('to_status')!=job['status'] or result.get('authorized') is not True or result.get('transitioned') is not True or result.get('job_id')!=job['id']:
            return False
        # Receipt filename binding is a keyed proof of the Education token
        # observed by the monitor; the original token remains reserved forever.
        from core.dispatch_receipts import observed_filename_matches
        from core.auth import SECRET_KEY
        names=db.execute(text('SELECT filename,job_name FROM print_jobs WHERE id=:id'),{'id':observation['id']}).mappings().one()
        protocol=evidence['printer']['api_type']
        return observed_filename_matches(SECRET_KEY.encode(),claim['token'],protocol,names['job_name'] if protocol=='bambu' else names['filename'],receipt['observed_filename_key'])
    except (ValueError,TypeError,KeyError):
        return False


def recovery_visibility(db, *, evidence, principal):
    """Use the established submission audience for minimal inspection fields.

    Read visibility never implies the admin and Education-write authority
    required to mutate a retained reservation.
    """
    from core.education_archive_audience import _SUBMISSION_AUDIENCE
    actor=db.execute(text('SELECT id,role,group_id,is_active FROM users WHERE id=:id'),{'id':principal.get('id')}).mappings().first()
    if not actor or not actor['is_active'] or actor['group_id'] is None:
        return False
    for submission in evidence['submissions']:
        if submission['org_id']!=actor['group_id'] or submission['org_id']!=evidence['job']['charged_to_org_id']:
            return False
        query='SELECT 1 FROM education_submissions s JOIN education_cost_centers c ON c.id=s.cost_center_id AND c.org_id=s.org_id WHERE s.id=:sid AND s.org_id=:org AND '+_SUBMISSION_AUDIENCE
        if not db.execute(text(query),{'sid':submission['id'],'org':submission['org_id'],'user_id':actor['id'],'is_admin':int(actor['role']=='admin')}).first():  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- verified safe — query is a module constant; values are bound
            return False
    return bool(evidence['submissions'])
