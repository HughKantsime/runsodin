"""Maintenance status: the one place that decides ok / due_soon / overdue.

Used by GET /maintenance/status (the page) and by the hourly notifier
(maintenance_alerts.py). It also owns the implicit 2000 h "General service" task
and the per-printer failure rate. No rows are written.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import sqlalchemy as sa
from sqlalchemy import or_

from core.physical_print import CLOSED_UNKNOWN
from core.rbac import get_org_scope, should_filter_org
from modules.printers.models import Printer
from modules.system.models import MaintenanceLog, MaintenanceTask

DEFAULT_TASK_NAME = "General service"
DEFAULT_INTERVAL_PRINT_HOURS = 2000.0
STAGE_RANK = {"ok": 0, "due_soon": 1, "overdue": 2}

# Stands in for a task row when no maintenance template applies to a printer.
_DEFAULT_TASK = SimpleNamespace(
    id=None,
    name=DEFAULT_TASK_NAME,
    description="Default service interval, used until a maintenance task is added for this printer",
    interval_print_hours=DEFAULT_INTERVAL_PRINT_HOURS,
    interval_days=None,
)

_PRINT_JOBS = sa.table(
    "print_jobs",
    sa.column("printer_id", sa.Integer),
    sa.column("status", sa.String),
    sa.column("error_code", sa.String),
    sa.column("started_at", sa.DateTime),
    sa.column("ended_at", sa.DateTime),
)


def visible_printer_query(query, user):
    """Printers the user may see. user=None means no scoping (background jobs)."""
    if user is None:
        return query
    org = get_org_scope(user)
    if should_filter_org(user, org):
        query = query.filter(or_(Printer.org_id == org, Printer.org_id.is_(None), Printer.shared.is_(True)))
    return query


def task_key(printer_id, task):
    """Stable key of a (printer, task) pair; the implicit task is 'default'."""
    task_id = task["task_id"]
    return f"{printer_id}:{'default' if task_id is None else task_id}"


def _applies(task, printer):
    model_filter = task.printer_model_filter
    return not model_filter or model_filter.lower() in (printer.model or "").lower()


def _naive_utc(moment):
    if moment.tzinfo is not None:
        moment = moment.astimezone(timezone.utc)
    return moment.replace(tzinfo=None)


def failure_rates(db, printer_ids, now=None):
    """{printer_id: {...}} over the last 30 and 90 days.

    failed / (completed + failed) print rows. Cancelled rows and rows retired by a
    new physical start (CLOSED_UNKNOWN) are not counted.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    cut30 = _naive_utc(now - timedelta(days=30))
    cut90 = _naive_utc(now - timedelta(days=90))
    when = sa.func.coalesce(_PRINT_JOBS.c.ended_at, _PRINT_JOBS.c.started_at)

    def count(status, cutoff=None):
        condition = _PRINT_JOBS.c.status == status
        if cutoff is not None:
            condition = sa.and_(condition, when >= cutoff)
        return sa.func.sum(sa.case((condition, 1), else_=0))

    result = {}
    if printer_ids:
        rows = db.execute(
            sa.select(_PRINT_JOBS.c.printer_id, count("completed", cut30), count("failed", cut30),
                      count("completed"), count("failed"))
            .where(_PRINT_JOBS.c.printer_id.in_(list(printer_ids)),
                   _PRINT_JOBS.c.status.in_(("completed", "failed")),
                   sa.or_(_PRINT_JOBS.c.error_code.is_(None), _PRINT_JOBS.c.error_code != CLOSED_UNKNOWN),
                   when >= cut90)
            .group_by(_PRINT_JOBS.c.printer_id)
        ).all()
        for printer_id, completed30, failed30, completed90, failed90 in rows:
            result[printer_id] = {
                "last_30_days": _window(completed30, failed30),
                "last_90_days": _window(completed90, failed90),
            }
    return result


def _window(completed, failed):
    completed, failed = int(completed or 0), int(failed or 0)
    total = completed + failed
    return {"completed": completed, "failed": failed,
            "rate_percent": round(failed / total * 100, 1) if total else None}


def _empty_failure_rate():
    return {"last_30_days": _window(0, 0), "last_90_days": _window(0, 0)}


def _entry(task, last_log, printer, total_hours, now):
    if last_log and last_log.performed_at:
        hours_since = max(0.0, total_hours - (last_log.print_hours_at_service or 0))
        performed_at = last_log.performed_at
        if performed_at.tzinfo is None:
            performed_at = performed_at.replace(tzinfo=timezone.utc)
        service_anchor = performed_at
        days_since = (now - performed_at).days
        last_serviced = last_log.performed_at.isoformat()
        last_by = last_log.performed_by
    else:
        hours_since = total_hours
        created = printer.created_at
        if created and created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        days_since = (now - created).days if created else 0
        service_anchor = created
        last_serviced = None
        last_by = None

    # Next due point: the interval measured from the last service (or from the
    # printer's start when never serviced).
    next_due_hours = hours_until_due = next_due_date = None
    if task.interval_print_hours and task.interval_print_hours > 0:
        next_due_hours = round(total_hours - hours_since + task.interval_print_hours, 1)
        hours_until_due = round(task.interval_print_hours - hours_since, 1)
    if task.interval_days and task.interval_days > 0 and service_anchor:
        next_due_date = (service_anchor + timedelta(days=task.interval_days)).isoformat()

    task_status = "ok"
    progress = 0.0

    if task.interval_print_hours and task.interval_print_hours > 0:
        pct = (hours_since / task.interval_print_hours) * 100
        progress = max(progress, pct)
        if hours_since >= task.interval_print_hours:
            task_status = "overdue"
        elif hours_since >= task.interval_print_hours * 0.8:
            task_status = "due_soon"

    if task.interval_days and task.interval_days > 0:
        pct = (days_since / task.interval_days) * 100
        progress = max(progress, pct)
        if days_since >= task.interval_days:
            task_status = "overdue"
        elif days_since >= task.interval_days * 0.8:
            if task_status != "overdue":
                task_status = "due_soon"

    return {
        "task_id": task.id,
        "task_name": task.name,
        "description": task.description,
        "interval_print_hours": task.interval_print_hours,
        "interval_days": task.interval_days,
        "hours_since_service": round(hours_since, 1),
        "days_since_service": days_since,
        "last_serviced": last_serviced,
        "last_by": last_by,
        "status": task_status,
        "progress_percent": round(min(progress, 150), 1),
        "next_due_print_hours": next_due_hours,
        "hours_until_due": hours_until_due,
        "next_due_date": next_due_date,
        "is_default": task is _DEFAULT_TASK,
    }


def compute_status(db, user=None, *, include_failure_rate=True, now=None, service_cycle=False):
    """Per-printer maintenance health for the printers `user` may see (all when None).

    ``service_cycle`` (the hourly notifier only) adds ``service_cycle`` to each task: the id of
    the service entry the status is measured from plus the task's intervals, all stored values.
    """
    printers = (visible_printer_query(db.query(Printer), user)
                .filter(Printer.is_active.is_(True)).order_by(Printer.name).all())
    if not printers:
        return []
    all_tasks = db.query(MaintenanceTask).all()
    tasks = [t for t in all_tasks if t.is_active]

    hours_map = {p.id: float(p.total_print_hours or 0) for p in printers}

    all_logs = db.query(MaintenanceLog).filter(
        MaintenanceLog.printer_id.in_([p.id for p in printers])
    ).order_by(MaintenanceLog.performed_at.desc(), MaintenanceLog.id.desc()).all()
    log_map = {}
    default_log_map = {}
    for mlog in all_logs:
        key = (mlog.printer_id, mlog.task_id)
        if key not in log_map or (mlog.performed_at and log_map[key].performed_at and mlog.performed_at > log_map[key].performed_at):
            log_map[key] = mlog
        # The implicit task has no row; a service logged under its name (no task) resets it.
        if mlog.task_id is None and (mlog.task_name or "").strip().lower() == DEFAULT_TASK_NAME.lower():
            default_log_map.setdefault(mlog.printer_id, mlog)

    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    rates = failure_rates(db, [p.id for p in printers], now) if include_failure_rate else {}
    result = []

    for printer in printers:
        total_hours = hours_map.get(printer.id, 0)
        applicable = [t for t in tasks if _applies(t, printer)]
        if not applicable and not any(_applies(t, printer) for t in all_tasks):
            # No template of any kind applies (active or not): the default stands in.
            entries = [(_DEFAULT_TASK, default_log_map.get(printer.id))]
        else:
            entries = [(t, log_map.get((printer.id, t.id))) for t in applicable]

        printer_tasks = []
        worst_status = "ok"
        for task, last_log in entries:
            entry = _entry(task, last_log, printer, total_hours, now)
            if service_cycle:
                entry["service_cycle"] = "|".join(str(v if v is not None else "") for v in (
                    getattr(last_log, "id", None), task.interval_print_hours, task.interval_days))
            if entry["status"] == "overdue":
                worst_status = "overdue"
            elif entry["status"] == "due_soon" and worst_status == "ok":
                worst_status = "due_soon"
            printer_tasks.append(entry)

        row = {
            "printer_id": printer.id,
            "printer_name": printer.name,
            "printer_model": printer.model,
            "total_print_hours": round(total_hours, 1),
            # Care counters: lifetime prints plus prints/hours since the last
            # logged maintenance (reset by POST /maintenance/logs).
            "total_print_count": printer.total_print_count or 0,
            "prints_since_maintenance": printer.prints_since_maintenance or 0,
            "hours_since_maintenance": round(float(printer.hours_since_maintenance or 0), 1),
            "tasks": sorted(printer_tasks, key=lambda t: {"overdue": 0, "due_soon": 1, "ok": 2}.get(t["status"], 3)),
            "overall_status": worst_status,
        }
        if include_failure_rate:
            row["failure_rate"] = rates.get(printer.id) or _empty_failure_rate()
        result.append(row)

    result.sort(key=lambda p: {"overdue": 0, "due_soon": 1, "ok": 2}.get(p["overall_status"], 3))
    return result
