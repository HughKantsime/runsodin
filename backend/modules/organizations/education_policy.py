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


def education_token_digest(token: str) -> str:
    return hashlib.sha256(str(token).lower().encode("ascii")).hexdigest()


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
    if _exists(
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
            job_changed = db.execute(
                text(
                    "UPDATE jobs SET status='scheduled' WHERE id=:job_id "
                    "AND charged_to_org_id=:org_id AND status='pending' "
                    "AND printer_id=:printer_id"
                ),
                {
                    "job_id": job_id,
                    "org_id": context["org_id"],
                    "printer_id": printer_id,
                },
            )
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
            job_changed = db.execute(
                text(
                    "UPDATE jobs SET status='pending',printer_id=:printer_id,scheduled_start=NULL,"
                    "scheduled_end=NULL,match_score=NULL WHERE id=:job_id AND status='scheduled' "
                    "AND printer_id=:printer_id"
                ),
                {"job_id": job_id, "printer_id": printer_id},
            )
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
            job_changed = db.execute(
                text(
                    "UPDATE jobs SET status='submitted',printer_id=NULL,scheduled_start=NULL,"
                    "scheduled_end=NULL,match_score=NULL,notes=CASE WHEN notes IS NULL OR notes='' "
                    "THEN :reason ELSE notes || '\n' || :reason END WHERE id=:job_id "
                    "AND status IN ('pending','scheduled')"
                ),
                {"job_id": job_id, "reason": f"Education scheduler reset: {sanitized}"},
            )
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
    db.execute(
        text(
            "UPDATE jobs SET status='submitted', printer_id=NULL, "
            "notes=CASE WHEN notes IS NULL OR notes='' THEN :reason "
            "ELSE notes || '\n' || :reason END WHERE id=:job_id"
        ),
        {"job_id": job_id, "reason": f"Education dispatch reset: {sanitized_reason}"},
    )
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
            job_changed = adapter.execute(
                "UPDATE jobs SET status='printing',actual_start=COALESCE(actual_start,CURRENT_TIMESTAMP) "
                "WHERE id=:job_id AND charged_to_org_id=:org_id AND status='scheduled' "
                "AND printer_id=:printer_id",
                {
                    "job_id": job_id,
                    "org_id": context["org_id"],
                    "printer_id": printer_id,
                },
            )
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


def claim_monitor_observation(
    executor,
    *,
    print_job_id: int,
    printer_id: int,
    observed_filename: str | None,
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
        "mc.authority_revision,mc.state,s.cost_center_id,s.submitted_by "
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
        linked = adapter.fetchone(
            "SELECT id FROM print_jobs WHERE id=:print_job_id AND printer_id=:printer_id "
            "AND scheduled_job_id=:job_id AND status='running'",
            {
                "print_job_id": print_job_id,
                "printer_id": printer_id,
                "job_id": context["job_id"],
            },
        )
        return {
            "education_owned": True,
            "education_reserved": True,
            "authorized": bool(linked),
            "transitioned": False,
            "idempotent": bool(linked),
            "submission_id": context["submission_id"],
            "job_id": context["job_id"],
            "lifecycle_revision": revision,
        }

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
                job_changed = adapter.execute(
                    "UPDATE jobs SET status='printing',actual_start=COALESCE(actual_start,CURRENT_TIMESTAMP) "
                    "WHERE id=:job_id AND charged_to_org_id=:org_id AND status=:status "
                    "AND printer_id=:printer_id",
                    {**context, "status": expected_job_status},
                )
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
                "AND scheduled_job_id IS NULL AND status='running'",
                {
                    "job_id": context["job_id"],
                    "print_job_id": print_job_id,
                    "printer_id": printer_id,
                },
            )
            if observation_changed.rowcount != 1:
                raise _MonitorTransitionMiss
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
    filename = observed_filename
    if print_job_id is not None:
        linked = adapter.fetchone(
            "SELECT s.id AS submission_id,s.job_id,s.lifecycle_revision,p.filename,p.job_name "
            "FROM print_jobs p LEFT JOIN education_submissions s ON s.job_id=p.scheduled_job_id "
            "WHERE p.id=:print_job_id AND p.printer_id=:printer_id",
            {"print_job_id": print_job_id, "printer_id": printer_id},
        )
        if linked:
            filename = filename or linked.get("filename") or linked.get("job_name")
            if linked.get("submission_id") is not None:
                return {
                    "education_owned": True,
                    "education_reserved": True,
                    "authorized": True,
                    "submission_id": linked["submission_id"],
                    "job_id": linked["job_id"],
                    "lifecycle_revision": linked["lifecycle_revision"],
                }
    token = parse_education_token(filename)
    if token is None:
        return {"education_owned": False, "education_reserved": False, "authorized": False}
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
        "mc.authority_revision FROM print_jobs p "
        "LEFT JOIN education_submissions s ON s.job_id=p.scheduled_job_id "
        "LEFT JOIN jobs j ON j.id=s.job_id AND j.charged_to_org_id=s.org_id "
        "LEFT JOIN education_monitor_claims mc ON mc.job_id=s.job_id AND mc.org_id=s.org_id "
        "WHERE p.id=:print_job_id AND p.printer_id=:printer_id",
        {"print_job_id": print_job_id, "printer_id": printer_id},
    )
    if not context:
        return {"education_owned": False, "education_reserved": False, "transitioned": False}
    if context.get("submission_id") is None:
        token = parse_education_token(context.get("filename") or context.get("job_name"))
        if token is None:
            return {"education_owned": False, "education_reserved": False, "transitioned": False}
        adapter.execute(
            "UPDATE print_jobs SET status=:status,ended_at=COALESCE(:ended_at,CURRENT_TIMESTAMP),"
            "error_code=COALESCE(:error_code,error_code) WHERE id=:print_job_id "
            "AND printer_id=:printer_id AND scheduled_job_id IS NULL",
            {
                "status": terminal_status,
                "ended_at": ended_at,
                "error_code": sanitized_error,
                "print_job_id": print_job_id,
                "printer_id": printer_id,
            },
        )
        return {
            "education_owned": False,
            "education_reserved": True,
            "authorized": False,
            "transitioned": True,
            "quarantined": True,
        }

    base_result = {
        "education_owned": True,
        "education_reserved": True,
        "submission_id": context["submission_id"],
        "job_id": context["job_id"],
        "lifecycle_revision": context["lifecycle_revision"],
    }
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
            job_changed = adapter.execute(
                "UPDATE jobs SET status=:status,actual_end=COALESCE(:ended_at,CURRENT_TIMESTAMP) "
                "WHERE id=:job_id AND charged_to_org_id=:org_id AND status='printing' "
                "AND printer_id=:printer_id",
                {
                    **context,
                    "status": terminal_status,
                    "ended_at": ended_at,
                    "printer_id": printer_id,
                },
            )
            if job_changed.rowcount != 1:
                raise _MonitorTransitionMiss
            claim_deleted = adapter.execute(
                "DELETE FROM education_monitor_claims WHERE claim_id=:claim_id "
                "AND submission_id=:submission_id AND job_id=:job_id AND printer_id=:printer_id "
                "AND authority_revision=:revision AND state='running'",
                {**context, "revision": revision, "printer_id": printer_id},
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
        if parse_education_token(row.get("filename") or row.get("job_name")) is not None
    ]
    if len(quarantined) == 1:
        return {
            "education_owned": False,
            "education_reserved": True,
            "authorized": True,
            "print_job_id": quarantined[0]["print_job_id"],
        }
    return {
        "education_owned": False,
        "education_reserved": bool(quarantined),
        "authorized": False,
        "ambiguous": len(quarantined) > 1,
    }
