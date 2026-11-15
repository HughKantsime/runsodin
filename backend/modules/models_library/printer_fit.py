"""Which of the caller's printers a stored print file runs on, and what the file says.

This module holds no rule of its own. A printer fits a file when every check that a
schedule request and the scheduler already make for that pair passes:

* the printer is active and takes jobs from ODIN at all
  (``dispatch_unsupported_reason``, ``experimental_refusal``, the license selection);
* ``queue_payload.printer_payload_refusal``: printer type, file type, the printer
  types the file allows, and the bed (the schedule and queue request check);
* the same first-plate payload validation used by scheduling;
* the scheduler's same-machine gate on ``print_files.printer_model``
  (``jobs.scheduler._same_printer_model``);
* the upload's printer binding (``core.print_file_binding``);
* tenant ownership (``core.dispatch_identity.ownership_refusal``).

States that pass on their own (a maintenance hold, a running job) are not counted
against a printer. The answer is information: nothing is refused on the strength of it.

Printers are read once for the whole request, with the visibility of ``GET /printers``
(the caller's organization, unowned printers and shared ones). Callers pass file rows
they have already authorized (``core.model_preview_access.accessible_preview_file``).
"""
import json
from pathlib import Path
from types import SimpleNamespace

from fastapi import HTTPException
from sqlalchemy import bindparam, text

_PRINTER_COLUMNS = ("id, name, org_id, shared, is_active, api_type, model, machine_type, "
                    "bed_x_mm, bed_y_mm, display_order")


def visible_printers(db, current_user):
    """The caller's printers, each with the reason it takes no jobs at all (or None).

    One printer query; the Experimental opt-in is read only for printers of an
    Experimental type, once per printer and never per file.
    """
    from core import license_lock
    from core.rbac import get_org_scope, should_filter_org
    from modules.printers.services import dispatch_unsupported_reason
    from modules.printers.services import experimental_refusal
    statement = "SELECT " + _PRINTER_COLUMNS + " FROM printers"
    params = {}
    org = get_org_scope(current_user)
    if should_filter_org(current_user, org):
        # The same predicate as the printer list.
        if org is None:
            statement += " WHERE (org_id IS NULL OR shared IS TRUE)"
        else:
            statement += " WHERE (org_id = :org OR org_id IS NULL OR shared IS TRUE)"
            params["org"] = org
    statement += " ORDER BY display_order, id"
    licensed = license_lock.stored_snapshot(db) if license_lock.enabled() else None
    printers = []
    # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- the statement is built only from this module's constant column list and fixed clauses; the organization is bound
    for row in db.execute(text(statement), params).mappings():
        printer = SimpleNamespace(**row)
        printer.unavailable = (
            (None if printer.is_active else "inactive")
            or dispatch_unsupported_reason(printer)
            or experimental_refusal(db, printer.id, printer.api_type)
            or (None if licensed is None or license_lock.printer_allowed(licensed, printer.id, "N")
                else "license_printer_locked"))
        printers.append(printer)
    return printers


def fits(file, printer, *, bound=None, tenant=None):
    """True when this file can be scheduled on this printer today."""
    from core.dispatch_identity import ownership_refusal
    from modules.jobs.services import printer_matches_sliced_model
    from modules.models_library.queue_payload import printer_payload_refusal
    if printer.unavailable or (bound is not None and bound != printer.id):
        return False
    if not file.stored_path or printer_payload_refusal(file, printer):
        return False
    required = (file.printer_model or "").strip()
    if required and not printer_matches_sliced_model(printer, required):
        return False
    return ownership_refusal(
        {"charged_to_org_id": tenant, "print_file_org_id": file.org_id},
        {"org_id": printer.org_id, "shared": bool(printer.shared)}) is None


def _unstated(db, files):
    """Ids of files for which no count is given: Education submissions follow their
    approved printer, and controlled parts follow their printer group."""
    from core.controlled_parts import any_marks, policies
    from core.model_preview_access import education_owned_file_ids
    hidden = set(education_owned_file_ids(db, [file.id for file in files]))
    if any_marks(db):
        hidden.update(file.id for file in files if policies(db, "file", file.id))
    return hidden


def printer_fit_for_files(db, current_user, files):
    """{file id: fit} for authorized print file rows, from one set of queries.

    fit is ``{"count", "printers": [{"id", "name"}], "sliced_for"}``, or None when
    no count is stated for that file.
    """
    from core.print_file_binding import target_printers
    from core.print_payload import validate_plate
    from modules.printers.printer_models import bambu_model_name
    files = [file for file in files if file is not None]
    if not files:
        return {}
    printers = visible_printers(db, current_user)
    bound = target_printers(db, [file.id for file in files])
    unstated = _unstated(db, files)
    result = {}
    for file in files:
        if file.id in unstated:
            result[file.id] = None
            continue
        # Scheduling charges the caller, including a platform admin with no group.
        tenant = (current_user or {}).get("group_id")
        ready = True
        if Path(file.stored_path or "").suffix.lower() == ".3mf":
            try:
                # Same first-plate check as scheduling, once per file, not printer.
                validate_plate(file.stored_path, 0, "bambu")
            except HTTPException:
                ready = False
        matching = [printer for printer in printers
                    if ready and fits(file, printer, bound=bound.get(file.id), tenant=tenant)]
        # The model as the printer list names it ("N2S" and "Bambu Lab A1" are both "A1").
        sliced_for = bambu_model_name(file.printer_model)
        result[file.id] = {
            "count": len(matching),
            "printers": [{"id": printer.id, "name": printer.name} for printer in matching],
            "sliced_for": sliced_for if sliced_for and sliced_for.lower() != "unknown" else None,
        }
    return result


def schedule_file(model, variants):
    """The file a schedule request for this model uses, from its authorized variants."""
    if not variants:
        return None
    wanted = model.print_file_id if model.print_file_id is not None else max(row.id for row in variants)
    return next((row for row in variants if row.id == wanted), None)


def _positive(value, digits):
    try:
        number = round(float(value), digits)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def file_facts(nozzle_diameter=None, layer_height=None, bed_type=None, supports_used=None, filaments=None):
    """What the file says about how it was sliced; a fact it does not state is None."""
    if isinstance(filaments, str):
        try:
            filaments = json.loads(filaments)
        except ValueError:
            filaments = None
    colors = []
    for filament in filaments if isinstance(filaments, list) else []:
        color = filament.get("color") if isinstance(filament, dict) else getattr(filament, "color", None)
        # Legacy rows cannot distinguish explicit grey from the parser's fallback.
        # Omit that ambiguous value unless the parser recorded its provenance.
        legacy_stated = color != "#888888"
        stated = (filament.get("color_stated", legacy_stated) if isinstance(filament, dict)
                  else getattr(filament, "color_stated", legacy_stated))
        if not stated:
            continue
        if isinstance(color, str) and color.strip() and color not in colors:
            colors.append(color)
    plate = str(bed_type or "").strip()
    return {
        "nozzle_diameter": _positive(nozzle_diameter, 2),
        "layer_height": _positive(layer_height, 3),
        "plate_type": plate if plate and plate.lower() != "unknown" else None,
        "supports": supports_used if supports_used is None else bool(supports_used),
        "filament_colors": colors[:16],
    }


def row_file_facts(row):
    # Only a .3mf states whether supports are on; the column's default (false) on a
    # G-code row is not something the file said.
    is_3mf = str(row.stored_path or row.filename or "").lower().endswith(".3mf")
    return file_facts(row.nozzle_diameter, row.layer_height, row.bed_type,
                      row.supports_used if is_3mf else None, row.filaments_json)


def uploaded_file_fit(db, current_user, file_id):
    """The same two answers for a file that was just stored, for the upload result.

    Never raises: the upload has already been committed when this is read.
    """
    from core.model_preview_access import accessible_preview_file
    try:
        row = accessible_preview_file(db, file_id, current_user)
        return {"printer_fit": printer_fit_for_files(db, current_user, [row]).get(row.id),
                "file_facts": row_file_facts(row)}
    except Exception:
        return {"printer_fit": None, "file_facts": None}
