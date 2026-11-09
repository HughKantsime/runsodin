"""Read-only fleet status for home automation integrations (Home Assistant).

Spec: .claude/specs/home-assistant-integration.md (Part A).

Every value comes from ODIN's database. This route never contacts a printer,
an adapter or an MQTT broker, so an integration may poll it every few seconds
without taking printer connection slots (unlike /printers/live-status, which
opens a session to each printer per call).
"""
import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from core.db import get_db
from core.display_names import name_printers
from core.hms_state import display_entries
from core.rbac import get_org_scope, require_role, require_scope, should_filter_org
from modules.printers.models import Printer
from modules.printers.route_utils import get_camera_url

router = APIRouter()

SCHEMA_VERSION = 1
# Matches the web UI (frontend/src/utils/shared.ts ONLINE_THRESHOLD_MS).
ONLINE_SECONDS = 90
PRINTING_STATES = frozenset({"RUNNING", "PRINTING"})
PAUSED_STATES = frozenset({"PAUSE", "PAUSED"})
ERROR_STATES = frozenset({"FAILED", "ERROR"})
QUEUED_JOB_STATES = ("pending", "submitted", "scheduled")
STATUSES = ("printing", "paused", "error", "offline", "idle")


def _utc(value):
    """Database timestamps without a zone are UTC."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _iso(value):
    value = _utc(value)
    return value.isoformat().replace("+00:00", "Z") if value else None


def active_hms_count(stored) -> int:
    try:
        entries = json.loads(stored or "[]")
    except (TypeError, ValueError):
        return 0
    return len(display_entries(entries)) if isinstance(entries, list) else 0


def is_online(gcode_state, last_seen, now) -> bool:
    if (gcode_state or "").strip().upper() == "OFFLINE":
        return False
    seen = _utc(last_seen)
    if seen is None:
        return False
    age = (now - seen).total_seconds()
    return 0 <= age < ONLINE_SECONDS


def derive_status(gcode_state, online: bool, hms_count: int) -> str:
    """One status per printer, in this order: offline, printing, paused, error, idle.

    A printing or paused printer keeps that status even with an HMS warning
    (the integration exposes the error separately); a stopped printer with an
    active HMS entry, or a FAILED/ERROR state, is 'error'.
    """
    state = (gcode_state or "").strip().upper()
    if not online:
        return "offline"
    if state in PRINTING_STATES:
        return "printing"
    if state in PAUSED_STATES:
        return "paused"
    if state in ERROR_STATES or hms_count > 0:
        return "error"
    return "idle"


def _number(value):
    if value is None:
        return None
    try:
        return round(float(value), 1)
    except (TypeError, ValueError):
        return None


def _integer(value):
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@router.get("/integrations/home-assistant/status", tags=["Integrations"])
def home_assistant_status(
    current_user: dict = Depends(require_role("viewer", scope="read:printers")),
    _jobs_scope: dict = Depends(require_scope("read:jobs")),
    db: Session = Depends(get_db),
):
    """Fleet and per-printer status for Home Assistant. Read-only, database only."""
    now = datetime.now(timezone.utc)
    query = db.query(Printer).filter(Printer.is_active.isnot(False))
    scope = get_org_scope(current_user)
    if should_filter_org(current_user, scope):
        query = query.filter((Printer.org_id == scope) | (Printer.org_id.is_(None)) | (Printer.shared.is_(True)))
    printers = query.order_by(Printer.display_order, Printer.id).all()
    # Display names with controlled-work redaction, exactly as the printer cards.
    name_printers(db, printers, current_user)

    running = {}
    if printers:
        rows = db.execute(
            text("SELECT printer_id, progress_percent, current_layer, total_layers, remaining_minutes "
                 "FROM print_jobs WHERE status = 'running' AND printer_id IN :ids "
                 "ORDER BY started_at DESC, id DESC").bindparams(bindparam("ids", expanding=True)),
            {"ids": [p.id for p in printers]},
        ).mappings().all()
        for row in rows:
            running.setdefault(row["printer_id"], row)

    items = []
    counts = {name: 0 for name in STATUSES}
    for printer in printers:
        online = is_online(printer.gcode_state, printer.last_seen, now)
        hms_count = active_hms_count(printer.hms_errors)
        status = derive_status(printer.gcode_state, online, hms_count)
        counts[status] += 1
        job = running.get(printer.id) if status in ("printing", "paused") else None
        remaining = _number(job["remaining_minutes"]) if job else None
        last_error = None
        if printer.last_error_code or printer.last_error_message:
            last_error = {"code": printer.last_error_code, "message": printer.last_error_message,
                          "at": _iso(printer.last_error_at)}
        items.append({
            "id": printer.id,
            "name": printer.nickname or printer.name,
            "model": printer.model,
            "api_type": printer.api_type,
            "status": status,
            "online": online,
            "raw_state": printer.gcode_state,
            "progress": _number(job["progress_percent"]) if job else None,
            "current_layer": _integer(job["current_layer"]) if job else None,
            "total_layers": _integer(job["total_layers"]) if job else None,
            "remaining_minutes": remaining,
            "eta": _iso(now + timedelta(minutes=remaining)) if remaining is not None and remaining >= 0 else None,
            "job_name": printer.current_print_display_name if job else None,
            "active_errors": hms_count,
            "last_error": last_error,
            "last_seen": _iso(printer.last_seen),
            "camera_available": bool(printer.camera_enabled and get_camera_url(printer)),
        })

    queue_query = "SELECT count(*) FROM jobs WHERE status IN :states"
    params = {"states": list(QUEUED_JOB_STATES)}
    if should_filter_org(current_user, scope):
        queue_query += " AND (charged_to_org_id = :org OR charged_to_org_id IS NULL)"
        params["org"] = scope
    queue_length = db.execute(text(queue_query).bindparams(bindparam("states", expanding=True)), params).scalar_one()

    from modules.system.health_version import backend_version
    return {
        "schema_version": SCHEMA_VERSION,
        "odin_version": backend_version(),
        "generated_at": _iso(now),
        "fleet": {**counts, "total": len(items), "queue_length": int(queue_length or 0)},
        "printers": items,
    }
