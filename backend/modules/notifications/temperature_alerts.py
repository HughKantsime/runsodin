"""Temperature-anomaly alerts: the periodic checker, its per-print state and the alert text.

Every 2 minutes, one query reads the last few minutes of printer_telemetry for every printer with a
running print; the pure rules in temperature_rules judge each printer; a new finding raises a
PRINTER_ERROR through the normal dispatcher (recipients, preferences, quiet hours and Education's phone
boundary all follow the existing alert rules). Alert-only: it never pauses or stops a print.

State ("alerted" keys per printer for its current print) lives in a small system_config row, read at the
start of every pass under the cross-process lock. A restart, or another worker taking over, therefore
cannot re-alert. Stored timestamps are naive UTC.
"""
import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import DateTime, bindparam, text
from sqlalchemy.orm import Session

from core.base import AlertSeverity, AlertType
from core.system_config_values import config_text, write_config_text
from modules.notifications import alert_dispatcher
from modules.notifications.temperature_rules import (
    HEATING_WITHOUT_TARGET, NOT_REACHING_TARGET, OVERSHOOT, SENSOR_FAULT, Finding, Sample,
    evaluate, normalize_settings, thresholds_from, window_minutes,
)

log = logging.getLogger("odin.temperature_alerts")

SETTINGS_KEY = "temperature_alerts"
STATE_KEY = "temperature_alerts_state"
INTERVAL_SECONDS = 120
LOCK_NAME = ".temperature-alerts.lock"

# One statement for every printer with a running print. LEFT JOIN: a printer with no recent telemetry
# still appears (so its alert state survives a telemetry gap) with NULL readings.
_ACTIVE = text(  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- constant SQL, values bound
    "SELECT p.printer_id AS printer_id, p.id AS print_id, p.started_at AS started_at, "
    "COALESCE(pr.nickname, pr.name) AS printer_name, "
    "t.recorded_at AS recorded_at, t.bed_temp AS bed_temp, t.nozzle_temp AS nozzle_temp, "
    "t.bed_target AS bed_target, t.nozzle_target AS nozzle_target "
    "FROM print_jobs p "
    "JOIN printers pr ON pr.id = p.printer_id "
    "LEFT JOIN printer_telemetry t ON t.printer_id = p.printer_id AND t.recorded_at >= :since AND t.recorded_at <= :until "
    "WHERE pr.is_active IS TRUE "
    "AND p.id = (SELECT MAX(q.id) FROM print_jobs q WHERE q.printer_id = p.printer_id AND q.status IN ('running', 'printing')) "
    "ORDER BY p.printer_id, t.recorded_at"
).bindparams(bindparam("since", type_=DateTime), bindparam("until", type_=DateTime))


# ---------------------------------------------------------------- settings and state rows

def _load_json(value):
    if isinstance(value, dict):
        return value
    try:
        loaded = json.loads(config_text(value))
    except (TypeError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def read_settings(db) -> dict:
    row = db.execute(text("SELECT value FROM system_config WHERE key = :k"), {"k": SETTINGS_KEY}).first()
    return normalize_settings(_load_json(row[0]) if row else None)


def store_settings(db, settings: dict) -> None:
    """Write in the caller's transaction; the caller audits and commits."""
    write_config_text(db, SETTINGS_KEY, json.dumps(settings, sort_keys=True))


def load_state(db) -> dict:
    row = db.execute(text("SELECT value FROM system_config WHERE key = :k"), {"k": STATE_KEY}).first()
    state = {}
    for key, entry in (_load_json(row[0]) if row else None or {}).items():
        if (isinstance(key, str) and key.isdigit() and isinstance(entry, dict)
                and isinstance(entry.get("print"), int) and isinstance(entry.get("alerted"), list)):
            state[key] = {"print": entry["print"], "alerted": [a for a in entry["alerted"] if isinstance(a, str)]}
    return state


def _dump(state: dict) -> str:
    return json.dumps(state, sort_keys=True)


# ---------------------------------------------------------------- alert text

def _c(value: float) -> str:
    return f"{round(value):d} C"


def describe(name: str, finding: Finding) -> str:
    """Plain words, e.g. 'Xena nozzle is 22 C below its 220 C target for 6 minutes'."""
    who = f"{name} {finding.sensor}"
    span = f"{finding.minutes} minute{'' if finding.minutes == 1 else 's'}"
    if finding.rule == NOT_REACHING_TARGET:
        return f"{who} is {_c(finding.target - finding.actual)} below its {_c(finding.target)} target for {span}"
    if finding.rule == OVERSHOOT:
        return f"{who} is {_c(finding.actual - finding.target)} above its {_c(finding.target)} target for {span}"
    if finding.rule == HEATING_WITHOUT_TARGET:
        return f"{who} is heating up ({_c(finding.actual)}) with no target set, rising for {span}"
    if finding.detail == "frozen":
        return f"{who} sensor has read exactly {_c(finding.actual)} for {span} while its target changed"
    return f"{who} sensor reads {_c(finding.actual)}, outside its possible range"


def alert_metadata(finding: Finding) -> dict:
    return {
        "kind": "temperature_anomaly", "rule": finding.rule, "sensor": finding.sensor,
        "actual": round(finding.actual, 1),
        "target": None if finding.target is None else round(finding.target, 1),
    }


def _stage(db, printer_id: int, name: str, finding: Finding):
    severity = AlertSeverity.CRITICAL if finding.critical else AlertSeverity.WARNING
    return alert_dispatcher.stage_alert(
        db, AlertType.PRINTER_ERROR, severity, f"Temperature warning: {name} {finding.sensor}",
        describe(name, finding), printer_id=printer_id, metadata=alert_metadata(finding))


# ---------------------------------------------------------------- the pass

def _naive_utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if value.tzinfo is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _groups(rows) -> dict:
    groups = {}
    for row in rows:
        group = groups.setdefault(row.printer_id, {
            "print_id": row.print_id, "name": row.printer_name or f"Printer {row.printer_id}",
            "started_at": _naive_utc(row.started_at), "samples": []})
        if row.recorded_at is not None:
            group["samples"].append(Sample(_naive_utc(row.recorded_at), row.bed_temp, row.nozzle_temp,
                                           row.bed_target, row.nozzle_target))
    return groups


def _prune(state: dict, groups: dict) -> None:
    for key in [k for k in state if int(k) not in groups]:
        del state[key]  # the print ended: its alerts are cleared


def run_pass(session_factory, *, now=None) -> list:
    """One checker pass. Returns the (printer_id, sensor, rule) of every alert raised."""
    current = _naive_utc(now or datetime.now(timezone.utc))
    raised = []
    with session_factory() as db:
        settings = read_settings(db)
        if not settings["enabled"]:
            return raised
        th = thresholds_from(settings)
        rows = db.execute(_ACTIVE, {"since": current - timedelta(minutes=window_minutes(th)), "until": current}).all()
        groups = _groups(rows)
        state = load_state(db)
        saved = _dump(state)
        _prune(state, groups)
        for printer_id, group in groups.items():
            if printer_id in settings["disabled_printer_ids"] or not group["samples"]:
                continue
            previous = state.get(str(printer_id))
            carried = set(previous["alerted"]) if previous and previous["print"] == group["print_id"] else set()
            result = evaluate(group["samples"], group["started_at"], current, th)
            alerted = carried - {f"{sensor}:{rule}" for sensor, rule in result.clear}
            plans, fresh = [], []
            try:
                for finding in result.findings:
                    key = f"{finding.sensor}:{finding.rule}"
                    if key in alerted:
                        continue
                    plans.append(_stage(db, printer_id, group["name"], finding))
                    alerted.add(key)
                    fresh.append((printer_id, finding.sensor, finding.rule))
                if alerted:
                    state[str(printer_id)] = {"print": group["print_id"], "alerted": sorted(alerted)}
                else:
                    state.pop(str(printer_id), None)
                if _dump(state) != saved:
                    write_config_text(db, STATE_KEY, _dump(state))
                    saved = _dump(state)
                db.commit()
            except Exception as error:
                db.rollback()
                log.warning("Temperature check for printer %s failed (%s)", printer_id, type(error).__name__)
                state = load_state(db)
                saved = _dump(state)
                _prune(state, groups)
                continue
            raised.extend(fresh)
            for plan in plans:  # after the commit: delivery can wait on the network
                try:
                    alert_dispatcher.deliver_staged_alert(db, plan)
                except Exception as error:
                    log.warning("Temperature alert delivery failed (%s)", type(error).__name__)
        if _dump(state) != saved:
            write_config_text(db, STATE_KEY, _dump(state))
            db.commit()
    return raised


def check(engine, *, now=None):
    """One locked pass. The lock is the installation-wide file lock the audit job uses, so a second
    API worker skips the pass instead of repeating it."""
    from core.audit_keys import file_lock
    with file_lock(LOCK_NAME, blocking=False) as acquired:
        if not acquired:
            return None
        return run_pass(lambda: Session(engine), now=now)


async def run():
    from core.db import current_engine
    while True:
        try:
            await asyncio.to_thread(check, current_engine())
        except Exception as error:
            log.error("Temperature alert pass unavailable (%s)", type(error).__name__)
        await asyncio.sleep(INTERVAL_SECONDS)
