"""Print-floor additions to the Home Assistant status payload.

Spec: .claude/specs/ha-print-floor.md (main section and Addendum; the Addendum wins).

Everything here reads O.D.I.N.'s database. Nothing contacts a printer, an adapter
or a broker, and nothing writes. Every job surfaced passes the same predicates as
``queue_length`` (``visible_clauses``): a job the caller cannot list is never
selected, so it cannot be named, counted or described.

Unlinked prints have no durable resume history: the timeline writer drops events
without a scheduled job. Monitors overwrite print_jobs progress/layer values;
vision detections store no progress/layer snapshot, and printer_telemetry stores
only temperatures/fan speed. The attention lane keeps the current episode's start, but does not keep a
resume history. None can tie an old auto_paused detection to the current pause.
For an unlinked print with such a detection, pause_cause is null and the reason is
"Paused (Vigil flagged this print earlier; cause of this pause unknown)", unless current HMS/print-error evidence supplies a cause.
Even a recent detection cannot prove that no resume has intervened. Scheduled
prints retain the existing timeline-based classification. No history is inferred
from elapsed time or the current layer alone.

Integration 0.3.0 (spec: .claude/specs/ha-integration-next.md, items A-D) adds, per printer,
``last_completed_at``, ``finished_at``, ``plate_clear_confirmed_at`` and ``slots``, and on the
next job ``queued_after``, ``short_spools`` and ``ready``. The times and the count pass
``visible_clauses`` like everything else; a time that belongs to a job the caller cannot
list is null. Spool amounts follow the filament-check route: withheld on a printer the
caller reaches only because it is shared. The filament check runs once per printer that has
a next job (as the AMS comparison does); everything else is a fixed number of queries.
"""
import json
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import bindparam, text

from modules.printers.ams_humidity import humidity_scale, reported_percent
from core.print_outcomes import PRINT_DID_NOT_COMPLETE_SQL, print_outcome
from core.physical_print import pause_identity
from core.display_names import display_names
from core.hms_state import distinct_entries, hms_code_parts, is_notice
from modules.printers.hms_codes import is_spaghetti_code
from core.rbac import should_filter_org

HIDDEN_ERROR_MESSAGE = "Details hidden (controlled work)"
CONTROLLED_NAME = "Controlled work"
HELD_REASON = "Held, waiting for go"
MAX_UNASSIGNED = 5

REFUSAL_WORDS = {
    "AMS_MAPPING_REQUIRED": "No AMS mapping chosen for this job",
    "MATERIAL_MISMATCH": "Loaded filament does not match the job",
    "TRAY_MATERIAL_UNREPORTED": "An AMS tray has no filament type set",
    "TRAY_MISSING": "A required AMS tray is missing",
    "TRAY_SLOT_MISMATCH": "AMS trays do not match the job's mapping",
    "TRAY_COUNT": "AMS trays do not match the job's mapping",
    "TRAY_NOT_MECHANICALLY_READY": "An AMS tray is not ready",
    "AMS_BINDING_MISMATCH": "An AMS slot feeds the wrong nozzle",
    "MAPPING_CHANGED": "AMS selection changed during dispatch",
    "PRINTER_MAINTENANCE_HOLD": "Printer is on a maintenance hold",
    "PRINTER_NOT_IDLE_OR_ERROR": "Printer is not idle or reports an error",
    "PRINTER_MODEL_MISMATCH": "File was sliced for a different printer model",
    "BED_GEOMETRY_MISMATCH": "File does not fit this printer's bed",
    "NOZZLE_MISMATCH": "Installed nozzle does not match the file",
    "SOURCE_MAPPING_UNVERIFIED": "Filament mapping could not be verified",
    "UNSUPPORTED_PRINTER": "Printer is not supported for this dispatch",
    "DEPLOY_WINDOW": "Dispatch is paused during a deployment window",
    "FILAMENT_SHORT": "Not enough filament loaded",
    "CONTROLLED_PART_PRINTER_GROUP_REQUIRED": "Printer is not in the required printer group",
    "CONTROLLED_PART_FEATURE_NOT_LICENSED": "Controlled-part handling is not licensed",
}



def to_utc(value):
    """Database timestamps without a zone are UTC."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def iso(value):
    value = to_utc(value)
    return value.isoformat().replace("+00:00", "Z") if value else None


def _json(value):
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value) if value else None
    except (TypeError, ValueError):
        return None


def _grams(value):
    try:
        return None if value is None else round(float(value), 1)
    except (TypeError, ValueError):
        return None


def _material(value):
    value = getattr(value, "value", value)
    return None if value is None or str(value).strip().lower() in ("", "empty") else str(value)


def _words(side):
    return " ".join(part for part in ((side or {}).get("material"), (side or {}).get("color")) if part)


def refusal_words(code):
    code = str(code or "").upper()
    if code in REFUSAL_WORDS:
        return REFUSAL_WORDS[code]
    return "Dispatch refused (" + code.replace("_", " ").replace(":", ": ").lower() + ")"


# ---------------------------------------------------------------------------
# Visibility: one definition shared by queue totals, next jobs and day totals.
# ---------------------------------------------------------------------------

def visible_clauses(actor, scope, controlled, alias="jobs"):
    """The predicates GET /api/jobs applies (archived Education, controlled parts, org)."""
    clauses = [f"NOT EXISTS (SELECT 1 FROM education_submissions es WHERE es.job_id={alias}.id AND es.archived_at IS NOT NULL)"]
    params = {}
    if controlled:
        from core.controlled_parts import job_visibility
        predicate, visibility_params = job_visibility(actor, alias)
        clauses.append("(" + predicate + ")")
        params.update(visibility_params)
    if should_filter_org(actor, scope):
        clauses.append(f"({alias}.charged_to_org_id = :org OR {alias}.charged_to_org_id IS NULL)")
        params["org"] = scope
    return clauses, params


def queue_totals(db, actor, scope, controlled, states):
    """Count, estimated hours and un-estimated jobs of the caller-visible queue."""
    clauses, params = visible_clauses(actor, scope, controlled)
    params["states"] = list(states)
    sql = ("SELECT count(*), "
           "COALESCE(SUM(CASE WHEN jobs.duration_hours > 0 THEN jobs.duration_hours ELSE 0 END), 0), "
           "COALESCE(SUM(CASE WHEN jobs.duration_hours IS NULL OR jobs.duration_hours <= 0 THEN 1 ELSE 0 END), 0) "
           "FROM jobs WHERE jobs.status IN :states AND " + " AND ".join(clauses))
    # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- fixed clauses plus job_visibility's source-owned predicate; every value is bound
    count, hours, unestimated = db.execute(text(sql).bindparams(bindparam("states", expanding=True)), params).one()
    return {"count": int(count or 0), "hours": round(float(hours or 0), 2), "unestimated": int(unestimated or 0)}


# ---------------------------------------------------------------------------
# AMS humidity (latest stored reading per unit)
# ---------------------------------------------------------------------------

AMS_READING_MAX_AGE = timedelta(hours=24)


def ams_environment(db, printer_ids, now):
    """{printer_id: [unit, ...]} from the monitors' five-minute AMS readings (ams_telemetry).

    Bambu reports a level from 5 (driest) to 1 (wettest); `humidity_index` turns it round
    (1 = dry ... 5 = wet) to match the A-E scale on the printer's screen, and `grade` is that
    letter. Humidity percentage comes from the same reading when reported. Drying state
    is unknown. Readings older than a day are excluded.
    """
    if not printer_ids:
        return {}
    rows = db.execute(text(
        "SELECT t.printer_id, t.ams_unit, t.humidity, t.temperature, t.recorded_at, t.humidity_percent FROM ams_telemetry t "
        "JOIN (SELECT printer_id, ams_unit, MAX(id) AS latest FROM ams_telemetry WHERE printer_id IN :pids "
        "GROUP BY printer_id, ams_unit) m ON m.latest = t.id ORDER BY t.printer_id, t.ams_unit"
    ).bindparams(bindparam("pids", expanding=True)), {"pids": list(printer_ids)}).all()
    result = {}
    from modules.printers.ams_humidity import zone_labels
    labels = zone_labels(db, {r[0] for r in rows}) if rows else {}
    for printer_id, unit, humidity, temperature, recorded_at, humidity_percent in rows:
        at = to_utc(recorded_at)
        if at is None or now - at > AMS_READING_MAX_AGE:
            continue
        level, index, grade, temperature = humidity_scale(humidity, temperature)
        result.setdefault(printer_id, []).append({
            "unit": int(unit),
            **({"label": label} if (label := labels.get(printer_id, {}).get(str(unit))) else {}),
            "humidity_index": index,
            "grade": grade,
            "bambu_level": level,
            "humidity_percent": reported_percent(humidity_percent),
            "temperature_c": round(float(temperature), 1) if isinstance(temperature, (int, float)) else None,
            "drying": None,
            "recorded_at": iso(at),
            "age_seconds": max(0, int((now - at).total_seconds())),
        })
    return result


# ---------------------------------------------------------------------------
# Day totals
# ---------------------------------------------------------------------------

def day_start(now, zone=None):
    """Local midnight in ``zone`` at or before ``now``, as an aware UTC datetime (no zone: the OS zone).
    Built from the date so a daylight-saving change day gets midnight's own UTC offset."""
    local = now.astimezone(zone) if zone is not None else now.astimezone()
    midnight = datetime(local.year, local.month, local.day, tzinfo=zone) if zone is not None else local.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(timezone.utc)


def day_totals(db, actor, scope, controlled, printer_ids, now, zone=None):
    start = day_start(now, zone)
    result = {"plates_done": 0, "plates_failed": 0, "plates_stopped": 0, "plates_did_not_complete": 0, "since": iso(start)}
    if not printer_ids:
        return result
    # The exact test is on parsed instants below. This text bound only keeps the scan to recent
    # rows (this feed is polled every few seconds): the date prefix sorts the same in every
    # stored form, and two days cover any UTC offset.
    clauses = ["p.printer_id IN :pids", "(p.status = 'completed' OR " + PRINT_DID_NOT_COMPLETE_SQL.format(prefix="p.") + ")", "p.ended_at >= :day"]
    params = {"pids": list(printer_ids), "day": (start.astimezone(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%d")}
    # A print that belongs to a job counts only when the caller could list that job (the queue's
    # own predicates: org, archived Education, controlled parts). A print with no job is the
    # printer's own and counts with the printer.
    job_clauses, job_params = visible_clauses(actor, scope, controlled)
    clauses.append("(p.scheduled_job_id IS NULL OR p.scheduled_job_id IN (SELECT jobs.id FROM jobs WHERE "
                   + " AND ".join(job_clauses) + "))")
    params.update(job_params)
    sql = "SELECT p.status, p.ended_at FROM print_jobs p WHERE " + " AND ".join(clauses)
    # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- fixed clauses plus job_visibility's source-owned predicate; every value is bound
    for status, ended_at in db.execute(text(sql).bindparams(bindparam("pids", expanding=True)), params):
        # Monitors store both naive UTC and ISO timestamps with offsets; text order is not time order.
        ended = _instant(ended_at)
        if ended is not None and ended >= start:
            outcome = print_outcome(status, ended_at)
            result["plates_" + outcome if outcome else "plates_done"] += 1
    result["plates_did_not_complete"] = result["plates_failed"] + result["plates_stopped"]
    return result


# ---------------------------------------------------------------------------
# Job selection (all visible through visible_clauses)
# ---------------------------------------------------------------------------

def _pick(db, actor, scope, controlled, states, printer_ids):
    """First visible queued job per printer, and the first MAX_UNASSIGNED unassigned ones.

    Each row also carries how many visible queued jobs its printer has in all (the window
    counts only rows that passed ``visible_clauses``), which is where ``queued_after`` comes from.
    """
    clauses, params = visible_clauses(actor, scope, controlled, "j")
    params.update({"states": list(states), "pids": list(printer_ids) or [-1], "cap": MAX_UNASSIGNED})
    where = " AND ".join(["j.status IN :states"] + clauses)
    sql = ("SELECT id, printer_id, total FROM ("
           "SELECT j.id AS id, j.printer_id AS printer_id, ROW_NUMBER() OVER (PARTITION BY j.printer_id "
           "ORDER BY (j.scheduled_start IS NULL), j.scheduled_start, COALESCE(j.priority, 3), "
           "(j.queue_position IS NULL), j.queue_position, j.id) AS rn, "
           "COUNT(*) OVER (PARTITION BY j.printer_id) AS total "
           "FROM jobs j WHERE " + where + " AND (j.printer_id IN :pids OR j.printer_id IS NULL)"
           ") ranked WHERE ranked.rn <= CASE WHEN ranked.printer_id IS NULL THEN :cap ELSE 1 END "
           "ORDER BY ranked.rn, ranked.id")
    # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- fixed clauses plus job_visibility's source-owned predicate; every value is bound
    query = text(sql).bindparams(bindparam("states", expanding=True), bindparam("pids", expanding=True))
    return [(row[0], row[1], int(row[2] or 0)) for row in db.execute(query, params).all()]


def _last_prints(db, actor, scope, controlled, printer_ids):
    """{printer_id: {"completed": instant, "ended": instant, "resolved": instant}} from print_jobs.

    A printer runs one print at a time, so its highest-numbered ended row is its last print
    (stored end times mix naive UTC and ISO text and do not sort as text). ``completed`` is the
    end of the last print that completed, ``ended`` the end of the last print whatever its
    outcome. Both are None when that print belongs to a job the caller cannot list (the
    ``day_totals`` rule: a print with no job is the printer's own). ``resolved`` is the same
    completion time for the status rule only and is never sent.
    """
    if not printer_ids:
        return {}
    latest = db.execute(text(
        "SELECT p.printer_id, MAX(CASE WHEN p.status = 'completed' THEN p.id END), MAX(p.id) FROM print_jobs p "
        "WHERE p.printer_id IN :pids AND p.status IN ('completed', 'failed', 'cancelled') AND p.ended_at IS NOT NULL "
        "GROUP BY p.printer_id").bindparams(bindparam("pids", expanding=True)), {"pids": list(printer_ids)}).all()
    wanted = {value for _, done, ended in latest for value in (done, ended) if value is not None}
    if not wanted:
        return {}
    clauses, params = visible_clauses(actor, scope, controlled)
    params["ids"] = list(wanted)
    sql = ("SELECT p.id, p.ended_at, CASE WHEN p.scheduled_job_id IS NULL OR p.scheduled_job_id IN "
           "(SELECT jobs.id FROM jobs WHERE " + " AND ".join(clauses) + ") THEN 1 ELSE 0 END "
           "FROM print_jobs p WHERE p.id IN :ids")
    # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- fixed clauses plus job_visibility's source-owned predicate; every value is bound
    rows = {row[0]: (_instant(row[1]), bool(row[2]))
            for row in db.execute(text(sql).bindparams(bindparam("ids", expanding=True)), params).all()}
    found = {}
    for printer_id, done, ended in latest:
        done_at, done_visible = rows.get(done, (None, False))
        ended_at, ended_visible = rows.get(ended, (None, False))
        found[printer_id] = {"completed": done_at if done_visible else None,
                             "ended": ended_at if ended_visible else None, "resolved": done_at}
    return found


# ---------------------------------------------------------------------------
# Spool slots (what is loaded) and the pre-dispatch filament check
# ---------------------------------------------------------------------------

# The names the web page shows on a slot (frontend/src/utils/shared.ts: materialLabel,
# slotColorName). Kept word for word so Home Assistant and the Printers page agree.
_MATERIAL_NAMES = {
    "PLA_SUPPORT": "PLA Support", "PETG_SUPPORT": "PETG Support", "PA_SUPPORT": "PA Support", "SUPPORT": "Support",
    "PLA_CF": "PLA-CF", "PETG_CF": "PETG-CF", "PA_CF": "PA-CF", "PA_GF": "PA-GF", "PET_CF": "PET-CF",
    "ABS_GF": "ABS-GF", "ASA_CF": "ASA-CF", "PLA_AERO": "PLA Aero", "ASA_AERO": "ASA Aero", "PLA_SILK": "PLA Silk",
    "PLA_MATTE": "PLA Matte", "TPU_95A": "TPU 95A", "OTHER": "Other",
}
_FILAMENT_BRANDS = ("Bambu Lab", "Bambu", "Polymaker", "Hatchbox", "eSun", "Prusament", "Overture", "Elegoo",
                    "Sunlu", "Anycubic", "Creality", "Generic")


def material_label(value):
    """A material code as people write it ("PLA_SUPPORT" is "PLA Support"); None for an empty slot."""
    code = _material(value)
    code = code.strip() if code else ""
    if not code:
        return None
    if code.upper() in _MATERIAL_NAMES:
        return _MATERIAL_NAMES[code.upper()]
    return re.sub(r"_(CF|GF)$", lambda found: "-" + found.group(1).upper(), code, flags=re.IGNORECASE).replace("_", " ")


def slot_color_name(value):
    """The colour or product name on a slot without "preset:", the brand or a bare hex code; None when none."""
    name = re.sub(r"^preset:\s*", "", str(value or "").strip(), flags=re.IGNORECASE)
    if not name or re.fullmatch(r"#?[0-9a-fA-F]{6,8}", name):
        return None
    for brand in _FILAMENT_BRANDS:
        if name.lower().startswith(brand.lower() + " "):
            name = name[len(brand) + 1:].strip()
            break
    return name or None


def _color_hex(value):
    found = re.fullmatch(r"#?([0-9a-fA-F]{6})(?:[0-9a-fA-F]{2})?", str(value or "").strip())
    return "#" + found.group(1).upper() if found else None


def _amount(value):
    """A stored weight or percentage as a number; None (never 0) when it is not a finite number."""
    if type(value) not in (int, float) or value != value or value in (float("inf"), float("-inf")) or value < 0:
        return None
    return round(float(value), 1)


def printer_slots(db, printers, actor):
    """{printer_id: [slot, ...]}: what each printer has loaded, as the Printers page shows it.

    Values come from the Printers page's own slot schema (FilamentSlotResponse), so the
    colour name, the remaining percentage and the remaining grams follow its rules. On a
    printer the caller reaches only because it is shared, the owner's remaining amounts are
    withheld: the rule of the filament-check route (``hide_spool_details``).
    """
    if not printers:
        return {}
    from sqlalchemy.orm import joinedload
    from core.rbac import check_org_access
    from modules.printers.models import FilamentSlot
    from modules.printers.schemas import FilamentSlotResponse
    spool = FilamentSlot.assigned_spool.property.mapper.class_
    owners = {printer.id: check_org_access(actor, printer.org_id) for printer in printers}
    rows = (db.query(FilamentSlot)
            .options(joinedload(FilamentSlot.assigned_spool).joinedload(spool.filament))
            .filter(FilamentSlot.printer_id.in_(list(owners)))
            .order_by(FilamentSlot.printer_id, FilamentSlot.slot_number).all())
    found = {}
    for row in rows:
        try:
            shown = FilamentSlotResponse.model_validate(row)
        except Exception:
            continue  # a slot row the Printers page cannot show either
        owner = owners.get(row.printer_id, False)
        found.setdefault(row.printer_id, []).append({
            "slot": shown.slot_number,
            "color_hex": _color_hex(shown.color_hex),
            "name": slot_color_name(shown.color),
            "material": material_label(shown.filament_type),
            **({"preset_id": shown.preset_id} if shown.preset_id else {}),
            "remaining_percent": _amount(shown.remaining) if owner else None,
            "remaining_g": _amount(shown.remaining_weight) if owner else None,
        })
    return found


def short_spools(db, job_id, printer, actor):
    """[{slot, needed_g, remaining_g}] for the spools the pre-dispatch filament check finds short.

    The check is the one the Jobs page and dispatch use (``filament_check.check_job``), with
    that route's rule for a printer reached only because it is shared (no remaining weight).
    An empty list means every filament was judged and has enough. None means unknown: the
    check is off, could not run, or could not judge a filament and found none short.
    """
    from core.rbac import check_org_access
    from modules.printers import filament_check
    try:
        with db.begin_nested():  # a failed read must not poison the request's transaction
            result = filament_check.check_job(db.connection(), job_id, printer.id)
        if not check_org_access(actor, printer.org_id):
            result = filament_check.hide_spool_details(result)
        if result.get("overall") not in ("short", "enough"):
            return None
        return [{"slot": item.get("slot"), "needed_g": _amount(item.get("needed_g")),
                 "remaining_g": _amount(item.get("remaining_g"))}
                for item in result.get("slots", []) if item.get("status") == "short"]
    except Exception:
        return None


def _details(db, job_ids):
    """Detail rows for ids already chosen by _pick; never used to select jobs."""
    from core.dispatch_payload_selection import SELECTED_PRINT_FILE_ID_SQL
    sql = ("SELECT j.id, j.item_name, j.printer_id, j.hold, j.scheduled_start, j.duration_hours, "
           "j.quantity_on_bed, j.plate_index, j.ams_mapping, pf.plate_count, pf.filaments_json, "
           "m.color_requirements FROM jobs j LEFT JOIN models m ON m.id=j.model_id "
           "LEFT JOIN education_submissions s ON s.job_id=j.id "
           "LEFT JOIN print_files pf ON pf.id=(" + SELECTED_PRINT_FILE_ID_SQL + ") WHERE j.id IN :ids")
    # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- constant fragment: the SELECTED_PRINT_FILE_ID_SQL module constant; ids are bound
    rows = db.execute(text(sql).bindparams(bindparam("ids", expanding=True)), {"ids": list(job_ids)}).mappings().all()
    return {row["id"]: dict(row) for row in rows}


def _instant(value):
    """A stored time (naive UTC, or ISO text with or without an offset) as an aware UTC instant."""
    from datetime import datetime, timezone
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _clear_plate(db, actor, scope, controlled, printer_ids):
    clauses, params = visible_clauses(actor, scope, controlled, "j")
    params["pids"] = list(printer_ids)
    sql = ("SELECT j.printer_id, COALESCE(j.actual_end, j.updated_at) FROM jobs j "
           "WHERE CAST(j.status AS TEXT) = 'waiting_plate_clear' AND j.printer_id IN :pids AND " + " AND ".join(clauses) +
           " ORDER BY j.printer_id, j.id")
    found = {}
    # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- fixed clauses plus job_visibility's source-owned predicate; every value is bound
    for printer_id, since in db.execute(text(sql).bindparams(bindparam("pids", expanding=True)), params).all():
        found.setdefault(printer_id, since)
    return found


def _refusals(db, job_ids):
    """Jobs whose latest dispatch event (on their current printer) is a gate refusal."""
    sql = ("SELECT t.job_id, t.kind, t.code, t.occurred_at FROM job_timeline_events t "
           "JOIN jobs j ON j.id = t.job_id AND (t.printer_id IS NULL OR t.printer_id = j.printer_id) "
           "WHERE t.job_id IN :ids AND t.kind IN ('gate_refused','dispatch_uploading','dispatch_starting','dispatch_dispatched','dispatch_failed') "
           "AND t.occurred_at = (SELECT MAX(u.occurred_at) FROM job_timeline_events u "
           "WHERE u.job_id = t.job_id AND u.kind IN ('gate_refused','dispatch_uploading','dispatch_starting','dispatch_dispatched','dispatch_failed'))")
    found = {}
    for job_id, kind, code, at in db.execute(text(sql).bindparams(bindparam("ids", expanding=True)), {"ids": list(job_ids)}).all():
        if kind == "gate_refused":
            found[job_id] = (code, at)
    return found


# ---------------------------------------------------------------------------
# Filaments and the AMS check
# ---------------------------------------------------------------------------

def _needs(row):
    """The job's filaments in file order, and whether they came from the file's own metadata."""
    from fastapi import HTTPException
    from core.print_payload import source_filaments
    try:
        found = source_filaments(row["filaments_json"])
        return [{"file_slot": f["slot"], "color": f.get("color"), "material": f.get("type"),
                 "grams": _grams(f.get("used_grams"))} for f in found], True
    except HTTPException:
        pass
    requirements = _json(row["color_requirements"])
    if isinstance(requirements, dict):
        return [{"file_slot": index + 1, "color": value.get("color"),
                 "material": value.get("type") or value.get("material") or value.get("filament_type"),
                 "grams": _grams(value.get("grams"))}
                for index, value in enumerate(requirements.values()) if isinstance(value, dict)], False
    return [], False


def _tray(mapping, file_slot):
    """One-based AMS tray for a file filament, from the stored vector (index = file slot - 1)."""
    if isinstance(mapping, list) and 1 <= file_slot <= len(mapping):
        tray = mapping[file_slot - 1]
        if type(tray) is int and tray >= 0:
            return tray + 1
    return None


def _mismatched_slot(error):
    from core.print_payload import MappingMaterialMismatch
    return error.target_slot if isinstance(error, MappingMaterialMismatch) and error.confirmed else None


# ---------------------------------------------------------------------------
# HMS / pause classification
# ---------------------------------------------------------------------------

def _entry(item):
    """(attr, raw code, message) from a stored HMS entry; Nones when unreadable."""
    if not isinstance(item, dict):
        return None, None, None
    attr = item.get("attr") if type(item.get("attr")) is int else None
    raw = item.get("raw_code") if type(item.get("raw_code")) is int else (
        item.get("code") if type(item.get("code")) is int else None)
    if isinstance(item.get("code"), str) and "_" in item["code"]:
        try:
            left, right = item["code"].split("_", 1)
            attr = int(left, 16) if attr is None else attr
            raw = int(right, 16) if raw is None else raw
        except ValueError:
            pass
    message = item.get("message") if isinstance(item.get("message"), str) and item.get("message") else None
    return attr, raw, message


def _stage_code(stage):
    """'Paused: hms=0C000300_00030008' -> (attr, raw)."""
    if isinstance(stage, str) and stage.startswith("Paused: hms=") and "_" in stage:
        try:
            left, right = stage.removeprefix("Paused: hms=").split("_", 1)
            return int(left, 16), int(right, 16)
        except ValueError:
            pass
    return None, None


class Floor:
    """All floor data for one request, loaded in a fixed number of queries."""

    @classmethod
    def load(cls, db, actor, scope, controlled, printers, running, now, states, zone=None):
        self = cls()
        ids = [printer.id for printer in printers]
        self.queue = queue_totals(db, actor, scope, controlled, states)
        self.ams = ams_environment(db, ids, now)
        # Maintenance hold (Printer detail > Set maintenance state): dispatch is blocked, the
        # monitor and camera keep running. One read for all printers.
        from core.printer_maintenance import enrich_maintenance
        self.maintenance = {p.id: getattr(p, "maintenance", None) or {}
                            for p in enrich_maintenance(db, list(printers))}
        from core.farm_time import farm_zone  # the farm's day: saved setting, quiet-hours zone, TZ, UTC
        self.today = day_totals(db, actor, scope, controlled, ids, now, zone or farm_zone(db))
        picked = _pick(db, actor, scope, controlled, states, ids)
        rows = _details(db, [job_id for job_id, _, _ in picked]) if picked else {}
        self.names = display_names(db, "job", list(rows), actor) if rows else {}
        self.next_rows, self.queued, unassigned = {}, {}, []
        for job_id, printer_id, total in picked:
            if job_id not in rows:
                continue
            if printer_id is None:
                unassigned.append(rows[job_id])
            elif printer_id not in self.next_rows:
                self.next_rows[printer_id] = rows[job_id]
                self.queued[printer_id] = total  # visible queued jobs on this printer, the next one included
        self.unassigned = unassigned
        # Last print per printer, the plate-clear confirmation (the Printers page's own rule plus
        # this feed's job visibility) and the loaded spools.
        self.last_prints = _last_prints(db, actor, scope, controlled, ids)
        self.running = set(running)
        from core.printer_display_facts import plate_clear_times
        job_clauses, job_params = visible_clauses(actor, scope, controlled, "j")
        self.plate_cleared = plate_clear_times(db, ids, actor, job_clauses, job_params) if ids else {}
        from modules.printers.plate_clear import plate_clear_states
        self.plate_clear = plate_clear_states(db, printers, actor)
        self.slots = printer_slots(db, printers, actor)
        by_id = {printer.id: printer for printer in printers}
        self.short = {row["id"]: short_spools(db, row["id"], by_id[printer_id], actor)
                      for printer_id, row in self.next_rows.items()}
        self.refusals = _refusals(db, [row["id"] for row in self.next_rows.values()]) if self.next_rows else {}
        self.loaded = {}
        if self.next_rows:
            query = text("SELECT printer_id, slot_number, color, filament_type, color_hex FROM filament_slots "
                         "WHERE printer_id IN :ids").bindparams(bindparam("ids", expanding=True))
            from modules.printers.printer_models import identified_h3c
            for printer_id, slot, color, kind, color_hex in db.execute(query, {"ids": list(self.next_rows)}).all():
                loaded = {"color": color, "material": _material(kind)}
                if identified_h3c(by_id[printer_id]):
                    loaded['color_hex'] = color_hex
                self.loaded.setdefault(printer_id, {})[slot] = loaded
        self.clear_plate = _clear_plate(db, actor, scope, controlled, ids) if ids else {}
        self.load_pauses(db, printers, running, now)
        self.hidden_pause_times = set()
        if running:
            clauses, params = visible_clauses(actor, scope, controlled)
            params['ids'] = [row['id'] for row in running.values()]
            sql = ("SELECT p.printer_id FROM print_jobs p WHERE p.id IN :ids AND p.scheduled_job_id IS NOT NULL "
                   "AND p.scheduled_job_id NOT IN (SELECT jobs.id FROM jobs WHERE " + " AND ".join(clauses) + ")")
            # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- source-owned visibility predicates, values bound
            self.hidden_pause_times = set(db.execute(text(sql).bindparams(bindparam('ids', expanding=True)), params).scalars())
        return self

    def load_pauses(self, db, printers, running, now):
        """Shared read-only pause facts for the feed and reminder worker."""
        from modules.notifications.services import read_pause_reminder_settings
        self.pause_settings = read_pause_reminder_settings(db)
        self.now = now
        self.paused_long_count = 0
        self.episodes = {}
        keys = ['pause_episode:' + str(p.id) for p in printers]
        if keys:
            query = text('SELECT key,value FROM system_config WHERE key IN :keys').bindparams(bindparam('keys', expanding=True))
            for key, value in db.execute(query, {'keys': keys}):
                while isinstance(value, str):
                    value = _json(value)
                if isinstance(value, dict) and value.get('paused'):
                    pid = int(key.split(':')[1])
                    current = running.get(pid)
                    if current is None or not value.get('attempt') or value['attempt'] == pause_identity(current.get('economic_uid'), current['id']):
                        self.episodes[pid] = value
        self.paused_since, self.vision_paused, self.vision_unattributed = {}, set(), set()
        paused = [p.id for p in printers if (p.gcode_state or "").strip().upper() in ("PAUSE", "PAUSED")]
        if paused:
            query = text("SELECT printer_id, MAX(occurred_at) FROM job_timeline_events WHERE kind = 'paused' "
                         "AND printer_id IN :ids GROUP BY printer_id").bindparams(bindparam("ids", expanding=True))
            self.paused_since = {pid: iso(at) for pid, at in db.execute(query, {"ids": paused}).all()}
            prints = [running[pid]["id"] for pid in paused if pid in running]
            if prints:
                # Later training/pending rows are not pause evidence and must not hide it.
                # Apply the same eligibility and printer identity inside the latest lookup.
                query = text("SELECT d.print_job_id, d.printer_id, d.created_at, p.scheduled_job_id "
                             "FROM vision_detections d JOIN print_jobs p ON p.id = d.print_job_id AND p.printer_id = d.printer_id "
                             "WHERE d.print_job_id IN :ids "
                             "AND d.status = 'auto_paused' AND d.id = (SELECT MAX(x.id) FROM vision_detections x "
                             "WHERE x.print_job_id = d.print_job_id AND x.printer_id = d.printer_id "
                             "AND x.status = 'auto_paused')").bindparams(bindparam("ids", expanding=True))
                detections = db.execute(query, {"ids": prints}).all()
                # The detection must belong to THIS pause: one from before the print was last
                # resumed explains an earlier pause, not a planned pause reached afterwards.
                query = text("SELECT printer_id, occurred_at FROM job_timeline_events WHERE kind = 'resumed' "
                             "AND printer_id IN :ids").bindparams(bindparam("ids", expanding=True))
                resumed = {}
                for pid, at in db.execute(query, {"ids": paused}).all():
                    moment = _instant(at)
                    if moment is not None and (pid not in resumed or moment > resumed[pid]):
                        resumed[pid] = moment
                for print_id, pid, created, scheduled_job_id in detections:
                    if scheduled_job_id is None:
                        self.vision_unattributed.add(print_id)
                        continue
                    seen, last_resume = _instant(created), resumed.get(pid)
                    if last_resume is None or (seen is not None and seen > last_resume):
                        self.vision_paused.add(print_id)
        for pid, episode in self.episodes.items():
            if episode.get('since'):
                self.paused_since[pid] = iso(episode['since'])

    def pause_timing(self, printer, *, redact=False):
        from modules.printers.routes_integrations import is_online
        since = to_utc(self.episodes.get(printer.id, {}).get('since'))
        seconds = max(0, int((self.now - since).total_seconds())) if since and not redact else None
        long = None
        if seconds is not None and self.pause_settings['enabled'] and is_online(printer.gcode_state, printer.last_seen, self.now):
            long = seconds >= self.pause_settings['first_hours'] * 3600
        return {'paused_seconds': seconds, 'paused_long': long}

    # -- jobs ---------------------------------------------------------------

    def _describe(self, row, named, api_type):
        """(payload, mismatch reason) for one visible job; details withheld when not named."""
        hours = row["duration_hours"]
        # The display-name rules have the last word: a job they give no name to is not named
        # here from its raw item_name, and its details are withheld with it.
        named = bool(named and self.names.get(row["id"]))
        payload = {
            "id": row["id"],
            "name": self.names.get(row["id"]) if named else CONTROLLED_NAME,
            "held": bool(row["hold"]),
            "est_minutes": round(hours * 60) if hours and hours > 0 else None,
            "copies_on_plate": row["quantity_on_bed"],
            "filaments": None,
            "plate": None,
            "ams_check": None,
            "scheduled_start": iso(row["scheduled_start"]),
        }
        if not named:
            return payload, None
        needs, exact = _needs(row)
        mapping = _json(row["ams_mapping"])
        mapping = mapping if isinstance(mapping, list) else None
        payload["filaments"] = [
            {"slot": _tray(mapping, n["file_slot"]) if mapping is not None else n["file_slot"],
             "color": n["color"], "material": n["material"], "grams": n["grams"]} for n in needs]
        if row["plate_count"]:
            index = row["plate_index"] if type(row["plate_index"]) is int and row["plate_index"] >= 0 else 0
            # A stale plate number beyond the file's plates is not reported ("plate 6 of 3").
            if index < int(row["plate_count"]):
                payload["plate"] = {"index": index + 1, "count": int(row["plate_count"])}
        payload["ams_check"], reason = self._ams_check(row, needs, exact, mapping, row["printer_id"], api_type)
        return payload, reason

    def _ams_check(self, row, needs, exact, mapping, printer_id, api_type):
        """Same comparison the dispatch AMS gate uses (dispatch_options._mapping_error), read-only."""
        def side(need):
            return {"color": need["color"], "material": need["material"]}
        if mapping is None or not exact or printer_id is None or api_type != "bambu":
            return {"status": "unknown", "slots": [
                {"slot": n["file_slot"], "need": side(n), "loaded": None, "ok": None} for n in needs]}, None
        from modules.printers.dispatch_options import _mapping_error
        try:
            whole = _mapping_error({"printer_id": printer_id, "filaments_json": row["filaments_json"]}, mapping, slot_details=True)
        except Exception:
            whole = "AMS check unavailable"
        failed = _mismatched_slot(whole)
        loaded = self.loaded.get(printer_id, {})
        slots = []
        for need in needs:
            tray = _tray(mapping, need["file_slot"])
            if whole is None:
                ok = True if tray else None
            elif failed is None or tray is None:
                ok = None
            elif tray == failed:
                ok = False
            else:
                # The gate stops at the first bad tray; ask it about this filament alone.
                try:
                    alone = _mapping_error({"printer_id": printer_id, "filaments_json": json.dumps(
                        [{"slot": need["file_slot"], "type": need["material"]}])},
                        [-1] * (need["file_slot"] - 1) + [tray - 1], slot_details=True)
                except Exception:
                    alone = "AMS check unavailable"
                ok = True if alone is None else (False if _mismatched_slot(alone) is not None else None)
            slots.append({"slot": tray, "need": side(need), "loaded": loaded.get(tray) if tray else None, "ok": ok})
        status = "match" if whole is None else ("mismatch" if failed is not None else "unknown")
        from core.filament_color import hex_color, same_color
        for entry in slots:
            loaded_tray = entry['loaded'] or {}
            required_color = entry['need']['color']
            if 'color_hex' in loaded_tray and hex_color(required_color) and hex_color(loaded_tray['color_hex']):
                if not same_color(required_color, {'color_hex': loaded_tray['color_hex']}):
                    entry['color_warning'] = 'Colour differs'
                    if entry['ok'] is True:
                        entry['ok'] = False
                    if status == 'match':
                        status = 'colour_differs'
        reason = None
        if status == "mismatch":
            bad = next((s for s in slots if s["slot"] == failed), None)
            reason = (f"AMS slot {failed}: needs {_words(bad['need']) or 'a filament'}, has {_words(bad['loaded']) or 'empty'}"
                      if bad else f"AMS slot {failed}: filament does not match")
        return {"status": status, "slots": slots}, reason

    # -- per printer --------------------------------------------------------

    def completed_at(self, printer_id):
        """End of the printer's last completed print, for the unresolved-error rule only."""
        return (self.last_prints.get(printer_id) or {}).get("resolved")

    def for_printer(self, printer, status, running, names_allowed, redact, error_unresolved):
        row = self.next_rows.get(printer.id)
        next_job, mismatch = (self._describe(row, names_allowed, printer.api_type) if row is not None else (None, None))
        bench = None
        care = self.maintenance.get(printer.id) or {}
        held_out = care.get("state") == "hold"
        if next_job is not None:
            next_job["queued_after"] = max(0, self.queued.get(printer.id, 1) - 1)
            # Spool weights are details of the job's filaments: withheld with them.
            short = self.short.get(row["id"]) if next_job["filaments"] is not None else None
            next_job["short_spools"] = short
            colours = (next_job["ams_check"] or {}).get("status")
            if next_job["held"] or held_out or mismatch or short or row["id"] in self.refusals:
                next_job["ready"] = False
            else:
                # Unknown spool weights never hold a dispatch, so they do not make a job unready;
                # colours that cannot be compared leave the answer unknown.
                next_job["ready"] = True if colours == "match" else None
        last = self.last_prints.get(printer.id) or {}
        if status == "paused" or (status == "offline" and (printer.gcode_state or "").strip().upper() in ("PAUSE", "PAUSED")):
            cause, reason = self._pause(printer, running, error_unresolved, redact)
            hide_time = redact or printer.id in self.hidden_pause_times
            bench = {"state": "paused", "reason": reason,
                     "since": None if hide_time else self.paused_since.get(printer.id), "pause_cause": cause,
                     **self.pause_timing(printer, redact=hide_time)}
            self.paused_long_count += bench["paused_long"] is True
        elif held_out:
            # A free-text note can name the work on the bed: withheld like every other reason.
            bench = {"state": "out_of_service",
                     "reason": "Maintenance hold" if redact else (care.get("note") or "Maintenance hold")[:200],
                     "since": care.get("set_at"), "pause_cause": None}
        elif printer.id in self.clear_plate:
            bench = {"state": "clear_plate", "reason": "Plate not cleared", "since": iso(self.clear_plate[printer.id]),
                     "pause_cause": None}
        elif row is not None:
            refused = self.refusals.get(row["id"])
            if mismatch:
                bench = {"state": "blocked", "reason": HIDDEN_ERROR_MESSAGE if redact else mismatch,
                         "since": None, "pause_cause": None}
            elif next_job["held"]:
                bench = {"state": "held", "reason": HELD_REASON, "since": None, "pause_cause": None}
            elif refused:
                bench = {"state": "blocked", "reason": HIDDEN_ERROR_MESSAGE if redact else refusal_words(refused[0]),
                         "since": iso(refused[1]), "pause_cause": None}
        return {"next_job": next_job, "bench": bench, "out_of_service": held_out,
                "ams": self.ams.get(printer.id, []),
                "last_completed_at": iso(last.get("completed")),
                # The print on the bed now has not finished: no end time while one is running.
                "finished_at": None if printer.id in self.running else iso(last.get("ended")),
                "plate_clear": self.plate_clear[printer.id],
                "plate_clear_confirmed_at": iso(_instant(self.plate_cleared.get(printer.id))),
                # What is loaded on a printer carrying controlled work the caller cannot name
                # describes that work's filaments: withheld (null) with the job's own details.
                "slots": self.slots.get(printer.id, []) if names_allowed else None}

    def _pause(self, printer, running, error_unresolved, redact):
        stored = _json(printer.hms_errors)
        displayed = distinct_entries(stored) if isinstance(stored, list) else []
        faults = [_entry(item) for item in displayed if not is_notice(item)]
        entries = [_entry(item) for item in displayed if isinstance(item, dict) and not item.get('informational_only')
                   and (item.get('code') is not None or item.get('raw_code') is not None)]
        stage = printer.print_stage or ""
        vision = running is not None and running["id"] in self.vision_paused
        episode = self.episodes.get(printer.id, {})
        recorded = bool(printer.last_error_code or printer.last_error_message) and error_unresolved
        codes = [(a, c) for a, c, _ in entries]
        codes.extend([_stage_code(stage), hms_code_parts(episode.get('code'))])
        if recorded and printer.api_type == 'bambu':
            codes.append(hms_code_parts(printer.last_error_code))
        if vision or any(is_spaghetti_code(a, c) for a, c in codes):
            return "spaghetti", "Paused: possible spaghetti"
        runout, since = to_utc(episode.get('runout_at')), to_utc(episode.get('since'))
        if runout is not None and since is not None and runout <= since:
            return "filament", "Waiting for filament"
        entries = faults or entries
        if entries or stage.startswith("Paused: print_error=") or recorded:
            if redact:
                return "error", HIDDEN_ERROR_MESSAGE
            message = printer.last_error_message if recorded else None
            message = message or next((m for _, _, m in entries if m), None)
            if not message:
                code = next((f"{a:08X}_{c:08X}" for a, c, _ in entries if a is not None and c is not None), None)
                message = f"Printer error {code}" if code else "Printer error"
            return "error", message
        if running is not None and running["id"] in self.vision_unattributed:
            return None, "Paused (Vigil flagged this print earlier; cause of this pause unknown)"
        layer = running["current_layer"] if running is not None else None
        return "planned", f"Planned pause at layer {layer}" if layer else "Planned pause"

    # -- fleet --------------------------------------------------------------

    def fleet(self):
        unassigned = []
        for row in self.unassigned:
            payload, _ = self._describe(row, True, None)
            payload["candidate_printer_id"] = None
            unassigned.append(payload)
        return {"today": self.today, "paused_long": self.paused_long_count, "queued_hours": self.queue["hours"],
                "queued_unestimated": self.queue["unestimated"], "next_unassigned": unassigned}


def paused_prints(db, now):
    """Current physical prints only; the reminder caller applies Education suppression."""
    from modules.printers.models import Printer
    from modules.printers.routes_integrations import is_online, unresolved_error
    printers = [p for p in db.query(Printer).filter(Printer.is_active.isnot(False)).all()
                if (p.gcode_state or '').strip().upper() in ('PAUSE', 'PAUSED')
                and is_online(p.gcode_state, p.last_seen, now)]
    if not printers:
        return []
    query = text("SELECT id,printer_id,scheduled_job_id,economic_uid,filename,job_name,current_layer,started_at,ended_at,status "
                 "FROM print_jobs p WHERE printer_id IN :ids AND (status IN ('running','printing','paused') "
                 "OR id=(SELECT MAX(q.id) FROM print_jobs q WHERE q.printer_id=p.printer_id)) "
                 "ORDER BY started_at DESC,id DESC").bindparams(bindparam('ids', expanding=True))
    running, latest, ambiguous = {}, {}, set()
    for row in db.execute(query, {'ids': [p.id for p in printers]}).mappings():
        latest.setdefault(row['printer_id'], row)
        if row['status'] not in ('running', 'printing', 'paused'):
            continue
        if row['printer_id'] in running:
            ambiguous.add(row['printer_id'])
        running.setdefault(row['printer_id'], row)
    floor = Floor()
    floor.load_pauses(db, printers, running, now)
    result = []
    for printer in printers:
        row = running.get(printer.id)
        episode = floor.episodes.get(printer.id, {})
        if printer.id in ambiguous or not episode.get('since'):
            continue
        # A printer can pause before a physical print record exists. A known
        # attempt with no active record has ended and must not be reminded.
        if row is None:
            last = latest.get(printer.id)
            ended = to_utc(last['ended_at']) if last else None
            if episode.get('attempt') or (last and (ended is None or ended >= to_utc(episode['since']))):
                continue
        timing = floor.pause_timing(printer)
        if timing['paused_long'] is not True:
            continue
        unresolved = unresolved_error(printer.last_error_at, row['started_at'] if row else None)
        cause, reason = floor._pause(printer, row, unresolved, False)
        if row is None:
            context = episode.get('context', {})
            row = {'scheduled_job_id': None, 'filename': context.get('filename'), 'job_name': context.get('job_name')}
        result.append({'printer': printer.id, 'printer_name': printer.nickname or printer.name,
                       'job': row['scheduled_job_id'], 'filename': row['filename'], 'job_name': row['job_name'],
                       'episode': episode, 'cause': cause, 'reason': reason, **timing})
    return result
