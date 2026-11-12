"""Hourly maintenance notifier.

Raises MAINTENANCE_OVERDUE (critical) when a task becomes overdue and a due-soon
notice (info, same alert type, metadata.stage = "due_soon") when it first becomes due
soon. Transitions only: the last stage per (printer, task) is kept in system_config
('maintenance_alert_state'), so a task alerts once per stage. Logging service puts the
task back to ok, which resets it. Recipients, preferences and quiet hours are the
normal stage_alert rules.
"""
import asyncio
import json
import logging
import tempfile
from pathlib import Path

from sqlalchemy import text

from core.base import AlertSeverity, AlertType
from core.system_config_values import config_text, write_config_text
from modules.system.maintenance_status import STAGE_RANK, compute_status, task_key

log = logging.getLogger("odin.maintenance_alerts")

STATE_KEY = "maintenance_alert_state"
CHECK_INTERVAL_SECONDS = 3600
STARTUP_DELAY_SECONDS = 60
# Serialises workers on one host so a transition is never raised twice.
LOCK_PATH = Path(tempfile.gettempdir()) / "odin-maintenance-alerts.lock"


SERVICE_PREFIX = "svc:"  # state entries "svc:<printer>:<task>" hold the last service the stage was judged against


def _load(db):
    row = db.execute(text("SELECT value FROM system_config WHERE key = :k"), {"k": STATE_KEY}).first()
    if row is None:
        return {}
    value = row[0]
    if isinstance(value, dict):
        data = value
    else:
        try:
            data = json.loads(config_text(value))
        except ValueError:
            return {}
    return data if isinstance(data, dict) else {}


def load_state(db):
    """{"<printer>:<task>": stage} as last remembered."""
    return {str(k): v for k, v in _load(db).items()
            if isinstance(v, str) and v in STAGE_RANK and not str(k).startswith(SERVICE_PREFIX)}


def load_service_markers(db):
    """{"<printer>:<task>": last service time the remembered stage belongs to}."""
    return {str(k)[len(SERVICE_PREFIX):]: v for k, v in _load(db).items()
            if isinstance(v, str) and str(k).startswith(SERVICE_PREFIX)}


def service_marker(task):
    """Identifies the service cycle a task is in: the last service and where the next one falls
    (two entries logged in the same second differ by their print hours)."""
    return "|".join(str(task.get(field) or "") for field in ("last_serviced", "next_due_print_hours", "next_due_date"))


def save_state(db, state, markers=None):
    data = dict(state)
    data.update({SERVICE_PREFIX + key: marker for key, marker in (markers or {}).items() if key in state})
    write_config_text(db, STATE_KEY, json.dumps(data, sort_keys=True))


def _num(value):
    value = round(float(value or 0), 1)
    return str(int(value)) if value == int(value) else str(value)


def alert_text(printer, task, stage):
    """(title, message) in plain words."""
    verb = "overdue" if stage == "overdue" else "due soon"
    parts = []
    if task.get("interval_print_hours"):
        parts.append((f"{_num(task['interval_print_hours'])} print hours", _num(task["hours_since_service"]), "print hours"))
    if task.get("interval_days"):
        parts.append((f"{_num(task['interval_days'])} days", _num(task["days_since_service"]), "days"))
    if len(parts) == 1:
        detail = f"every {parts[0][0]}; {parts[0][1]} since last service"
    elif parts:
        detail = ("every " + " or ".join(p[0] for p in parts) + "; "
                  + " and ".join(f"{p[1]} {p[2]}" for p in parts) + " since last service")
    else:
        detail = ""
    message = f"Maintenance {verb} on {printer['printer_name']}: {task['task_name']}"
    if detail:
        message += f" ({detail})"
    return f"Maintenance {verb}: {printer['printer_name']}"[:200], message


def _stage(db, printer, task, stage):
    from modules.notifications import alert_dispatcher
    title, message = alert_text(printer, task, stage)
    metadata = {
        # "kind" tells the dispatcher this alert is de-duplicated by the stage state, not
        # by its per-printer 24 h rule (which would swallow a second task or the overdue stage).
        "kind": "maintenance_task",
        "stage": stage,
        "task_id": task["task_id"],
        "task_name": task["task_name"],
        "default": bool(task.get("is_default")),
        "interval_print_hours": task.get("interval_print_hours"),
        "hours_since_service": task.get("hours_since_service"),
        "interval_days": task.get("interval_days"),
        "days_since_service": task.get("days_since_service"),
    }
    severity = AlertSeverity.CRITICAL if stage == "overdue" else AlertSeverity.INFO
    return alert_dispatcher.stage_alert(db, AlertType.MAINTENANCE_OVERDUE, severity, title, message,
                                        printer_id=printer["printer_id"], metadata=metadata)


def check_maintenance(*, session_factory=None, now=None):
    """One pass over every active printer and task. Returns a small summary."""
    from modules.notifications import alert_dispatcher
    from modules.system.backup_service import restore_lock
    if session_factory is None:
        from core.db import SessionLocal
        session_factory = SessionLocal
    plans = []
    with session_factory() as db:
        with restore_lock(LOCK_PATH):
            # First run after an upgrade: tasks already due soon are noted quietly, so a large
            # farm gets alerts only for what is actually overdue instead of a burst.
            first_run = db.execute(text("SELECT 1 FROM system_config WHERE key = :k"), {"k": STATE_KEY}).first() is None
            state = load_state(db)
            markers = load_service_markers(db)
            seen, seen_markers = {}, {}
            for printer in compute_status(db, None, include_failure_rate=False, now=now):
                for task in printer["tasks"]:
                    key = task_key(printer["printer_id"], task)
                    stage = task["status"]
                    previous = state.get(key)
                    # A new service entry (or a changed interval) starts a new cycle: a task
                    # serviced and due again between two checks (downtime, a back-dated entry)
                    # alerts again. A state row with no marker yet is taken as this cycle.
                    marker = service_marker(task)
                    seen_markers[key] = marker
                    if key in markers and markers[key] != marker:
                        previous = None
                    if STAGE_RANK[stage] <= STAGE_RANK.get(previous, 0):
                        seen[key] = stage  # unchanged, improved, or logged: remember it
                        continue
                    # ...and the implicit default task is new with this version, so it never
                    # alerts on the first run either (a farm that never logged service would
                    # otherwise get one critical alert per printer).
                    if first_run and (stage == "due_soon" or task.get("is_default")):
                        seen[key] = stage
                        continue
                    try:
                        plan = _stage(db, printer, task, stage)
                        seen[key] = stage
                        # The alert rows and the remembered stage commit together.
                        save_state(db, {**state, **seen}, {**markers, **seen_markers})
                        db.commit()
                        if plan is not None:
                            plans.append(plan)
                    except Exception:
                        db.rollback()
                        log.warning("Maintenance alert could not be raised for printer %s", printer["printer_id"], exc_info=True)
                        # Retry next hour: forget the stage that was not raised.
                        if previous is not None:
                            seen[key] = previous
                        else:
                            seen.pop(key, None)
            # Keep the remembered stage of a task or printer that is only switched off, so
            # switching it back on does not alert again; forget what no longer exists.
            printer_ids = {str(i) for i in db.execute(text("SELECT id FROM printers")).scalars()}
            task_ids = {str(i) for i in db.execute(text("SELECT id FROM maintenance_tasks")).scalars()}
            for key, value in state.items():
                printer_part, _, task_part = key.partition(":")
                if key not in seen and printer_part in printer_ids and (task_part in task_ids or not task_part.isdigit()):
                    seen[key] = value
                    if key in markers:
                        seen_markers[key] = markers[key]
            kept_markers = {key: marker for key, marker in seen_markers.items() if key in seen}
            if seen != state or kept_markers != markers or first_run:
                save_state(db, seen, kept_markers)  # also drops tasks and printers that are gone
                db.commit()
        # After the lock: delivery can wait on the network and the rows are already safe.
        for plan in plans:
            try:
                alert_dispatcher.deliver_staged_alert(db, plan)
            except Exception:
                log.warning("Maintenance alert delivery failed", exc_info=True)
    return {"raised": len(plans)}


async def run(interval=CHECK_INTERVAL_SECONDS, startup_delay=STARTUP_DELAY_SECONDS):
    """Background task: check once after start-up, then hourly."""
    await asyncio.sleep(startup_delay)
    while True:
        try:
            await asyncio.to_thread(check_maintenance)
        except Exception:
            log.warning("Maintenance check failed", exc_info=True)
        await asyncio.sleep(interval)
