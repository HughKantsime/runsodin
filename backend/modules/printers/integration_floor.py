"""Print-floor additions to the Home Assistant status payload.

Spec: .claude/specs/ha-print-floor.md (main section and Addendum; the Addendum wins).

Everything here reads O.D.I.N.'s database. Nothing contacts a printer, an adapter
or a broker, and nothing writes. Every job surfaced passes the same predicates as
``queue_length`` (``visible_clauses``): a job the caller cannot list is never
selected, so it cannot be named, counted or described.

Unlinked prints have no durable resume history: the timeline writer drops events
without a scheduled job. Monitors overwrite print_jobs progress/layer values;
vision detections store no progress/layer snapshot, and printer_telemetry stores
only temperatures/fan speed. The attention lane's pause_episode is also overwritten
on each transition. None can tie an old auto_paused detection to the current pause.
For an unlinked print with such a detection, pause_cause is null and the reason is
"Paused (Vigil flagged this print earlier; cause of this pause unknown)", unless current HMS/print-error evidence supplies a cause.
Even a recent detection cannot prove that no resume has intervened. Scheduled
prints retain the existing timeline-based classification. No history is inferred
from elapsed time or the current layer alone.
"""
import json
import os
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import bindparam, text

from core.display_names import display_names
from core.hms_state import display_entries
from core.rbac import should_filter_org

HIDDEN_ERROR_MESSAGE = "Details hidden (controlled work)"
CONTROLLED_NAME = "Controlled work"
HELD_REASON = "Held, waiting for go"
MAX_UNASSIGNED = 5
# Bambu HMS spaghetti family 0C000300_00030008: module 0x0C, error 0x00030008.
SPAGHETTI_MODULE = 0x0C
SPAGHETTI_CODE = 0x00030008

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
_GRADES = "ABCDE"


def ams_environment(db, printer_ids, now):
    """{printer_id: [unit, ...]} from the monitors' five-minute AMS readings (ams_telemetry).

    Bambu reports a level from 5 (driest) to 1 (wettest); `humidity_index` turns it round
    (1 = dry ... 5 = wet) to match the A-E scale on the printer's screen, and `grade` is that
    letter. Percent humidity and drying state are not stored yet, so they are null rather
    than guessed. A unit with no sensor (AMS lite), or a reading older than a day, has no
    humidity (null, never 0).
    """
    if not printer_ids:
        return {}
    rows = db.execute(text(
        "SELECT t.printer_id, t.ams_unit, t.humidity, t.temperature, t.recorded_at FROM ams_telemetry t "
        "JOIN (SELECT printer_id, ams_unit, MAX(id) AS latest FROM ams_telemetry WHERE printer_id IN :pids "
        "GROUP BY printer_id, ams_unit) m ON m.latest = t.id ORDER BY t.printer_id, t.ams_unit"
    ).bindparams(bindparam("pids", expanding=True)), {"pids": list(printer_ids)}).all()
    result = {}
    for printer_id, unit, humidity, temperature, recorded_at in rows:
        at = to_utc(recorded_at)
        if at is None or now - at > AMS_READING_MAX_AGE:
            continue
        level = int(humidity) if isinstance(humidity, (int, float)) and 1 <= humidity <= 5 else None
        # Rows stored before the no-sensor rule: level 5 at exactly 0 degrees is an AMS lite placeholder.
        if level == 5 and temperature == 0:
            level, temperature = None, None
        index = 6 - level if level is not None else None
        result.setdefault(printer_id, []).append({
            "unit": int(unit),
            "humidity_index": index,
            "grade": _GRADES[index - 1] if index else None,
            "bambu_level": level,
            "humidity_percent": None,
            "temperature_c": round(float(temperature), 1) if isinstance(temperature, (int, float)) else None,
            "drying": None,
            "recorded_at": iso(at),
            "age_seconds": max(0, int((now - at).total_seconds())),
        })
    return result


# ---------------------------------------------------------------------------
# Day totals
# ---------------------------------------------------------------------------

def server_zone():
    """The server's timezone setting: the TZ environment variable, else the OS zone."""
    name = os.environ.get("TZ")
    if name:
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(name)
        except Exception:
            pass
    return None


def day_start(now, zone=None):
    """Local midnight (in the server zone) at or before ``now``, as an aware UTC datetime."""
    local = now.astimezone(zone) if zone is not None else now.astimezone()
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)


def day_totals(db, actor, scope, controlled, printer_ids, now, zone=None):
    start = day_start(now, zone)
    result = {"plates_done": 0, "plates_failed": 0, "since": iso(start)}
    if not printer_ids:
        return result
    # The exact test is on parsed instants below. This text bound only keeps the scan to recent
    # rows (this feed is polled every few seconds): the date prefix sorts the same in every
    # stored form, and two days cover any UTC offset.
    clauses = ["p.printer_id IN :pids", "p.status IN ('completed', 'failed')", "p.ended_at >= :day"]
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
            result["plates_done" if status == "completed" else "plates_failed"] += 1
    return result


# ---------------------------------------------------------------------------
# Job selection (all visible through visible_clauses)
# ---------------------------------------------------------------------------

def _pick(db, actor, scope, controlled, states, printer_ids):
    """First visible queued job per printer, and the first MAX_UNASSIGNED unassigned ones."""
    clauses, params = visible_clauses(actor, scope, controlled, "j")
    params.update({"states": list(states), "pids": list(printer_ids) or [-1], "cap": MAX_UNASSIGNED})
    where = " AND ".join(["j.status IN :states"] + clauses)
    sql = ("SELECT id, printer_id FROM ("
           "SELECT j.id AS id, j.printer_id AS printer_id, ROW_NUMBER() OVER (PARTITION BY j.printer_id "
           "ORDER BY (j.scheduled_start IS NULL), j.scheduled_start, COALESCE(j.priority, 3), "
           "(j.queue_position IS NULL), j.queue_position, j.id) AS rn "
           "FROM jobs j WHERE " + where + " AND (j.printer_id IN :pids OR j.printer_id IS NULL)"
           ") ranked WHERE ranked.rn <= CASE WHEN ranked.printer_id IS NULL THEN :cap ELSE 1 END "
           "ORDER BY ranked.rn, ranked.id")
    # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- fixed clauses plus job_visibility's source-owned predicate; every value is bound
    query = text(sql).bindparams(bindparam("states", expanding=True), bindparam("pids", expanding=True))
    return [(row[0], row[1]) for row in db.execute(query, params).all()]


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
           "WHERE j.status = 'waiting_plate_clear' AND j.printer_id IN :pids AND " + " AND ".join(clauses) +
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


def _slot_in(error):
    found = re.search(r"\(AMS slot (\d+)\)$", error or "")
    return int(found.group(1)) if found else None


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


def _is_spaghetti(attr, raw):
    return type(attr) is int and type(raw) is int and attr >> 24 == SPAGHETTI_MODULE and raw == SPAGHETTI_CODE


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
        self.today = day_totals(db, actor, scope, controlled, ids, now, zone or server_zone())
        picked = _pick(db, actor, scope, controlled, states, ids)
        rows = _details(db, [job_id for job_id, _ in picked]) if picked else {}
        self.names = display_names(db, "job", list(rows), actor) if rows else {}
        self.next_rows, unassigned = {}, []
        for job_id, printer_id in picked:
            if job_id not in rows:
                continue
            if printer_id is None:
                unassigned.append(rows[job_id])
            else:
                self.next_rows.setdefault(printer_id, rows[job_id])
        self.unassigned = unassigned
        self.refusals = _refusals(db, [row["id"] for row in self.next_rows.values()]) if self.next_rows else {}
        self.loaded = {}
        if self.next_rows:
            query = text("SELECT printer_id, slot_number, color, filament_type FROM filament_slots "
                         "WHERE printer_id IN :ids").bindparams(bindparam("ids", expanding=True))
            for printer_id, slot, color, kind in db.execute(query, {"ids": list(self.next_rows)}).all():
                self.loaded.setdefault(printer_id, {})[slot] = {"color": color, "material": _material(kind)}
        self.clear_plate = _clear_plate(db, actor, scope, controlled, ids) if ids else {}
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
        return self

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
            whole = _mapping_error({"printer_id": printer_id, "filaments_json": row["filaments_json"]}, mapping)
        except Exception:
            whole = "AMS check unavailable"
        failed = _slot_in(whole)
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
                        [-1] * (need["file_slot"] - 1) + [tray - 1])
                except Exception:
                    alone = "AMS check unavailable"
                ok = True if alone is None else (False if _slot_in(alone) is not None else None)
            slots.append({"slot": tray, "need": side(need), "loaded": loaded.get(tray) if tray else None, "ok": ok})
        status = "match" if whole is None else ("mismatch" if failed is not None else "unknown")
        reason = None
        if status == "mismatch":
            bad = next((s for s in slots if s["slot"] == failed), None)
            reason = (f"AMS slot {failed}: needs {_words(bad['need']) or 'a filament'}, has {_words(bad['loaded']) or 'empty'}"
                      if bad else f"AMS slot {failed}: filament does not match")
        return {"status": status, "slots": slots}, reason

    # -- per printer --------------------------------------------------------

    def for_printer(self, printer, status, running, names_allowed, redact, error_unresolved):
        row = self.next_rows.get(printer.id)
        next_job, mismatch = (self._describe(row, names_allowed, printer.api_type) if row is not None else (None, None))
        bench = None
        care = self.maintenance.get(printer.id) or {}
        held_out = care.get("state") == "hold"
        if status == "paused":
            cause, reason = self._pause(printer, running, error_unresolved, redact)
            bench = {"state": "paused", "reason": reason, "since": self.paused_since.get(printer.id), "pause_cause": cause}
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
                "ams": self.ams.get(printer.id, [])}

    def _pause(self, printer, running, error_unresolved, redact):
        stored = _json(printer.hms_errors)
        entries = [_entry(item) for item in display_entries(stored if isinstance(stored, list) else [])]
        stage = printer.print_stage or ""
        vision = running is not None and running["id"] in self.vision_paused
        if vision or any(_is_spaghetti(a, c) for a, c, _ in entries) or _is_spaghetti(*_stage_code(stage)):
            return "spaghetti", "Paused: possible spaghetti"
        recorded = bool(printer.last_error_code or printer.last_error_message) and error_unresolved
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
        return {"today": self.today, "queued_hours": self.queue["hours"],
                "queued_unestimated": self.queue["unestimated"], "next_unassigned": unassigned}
