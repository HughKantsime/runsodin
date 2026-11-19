"""Display facts for already-visible jobs: nozzle/slot selections and what each job is waiting for."""
import json
from sqlalchemy.orm import selectinload
from sqlalchemy.orm.attributes import set_committed_value
from core.rbac import check_org_access
from core.printer_display_facts import read_facts, may_read


def _recovery_printers(db, printers, actor):
    """Only point an admin to a retained claim within their recovery authority."""
    from core.controlled_parts import allowed
    from core.dispatch_claim_schema import PrinterDispatchClaim
    from core.dispatch_recovery import capture, authorized
    from core.rbac import is_scoped_token, token_scope_granted
    from core.rbac_policy import action_roles
    if not printers or actor.get('role') != 'admin':
        return set()
    if is_scoped_token(actor) and not all(token_scope_granted(actor.get('_token_scopes', []), scope)
                                          for scope in ('read:printers', 'write:printers')):
        return set()
    # A failed hint query must leave the caller's transaction usable on PostgreSQL.
    try:
        with db.begin_nested():
            if actor.get('group_id') is not None and 'admin' not in action_roles(db, 'printers.dispatch_recovery'):
                return set()
            claimed = db.query(PrinterDispatchClaim.printer_id).filter(PrinterDispatchClaim.printer_id.in_(printers)).all()
    except Exception:
        return set()
    result = set()
    for (printer_id,) in claimed:
        try:
            with db.begin_nested():
                evidence = capture(db, printer_id, actor, authority_only=True)
                if not evidence or not evidence['claim'] or not evidence['job']:
                    continue
                authorized(db, evidence, actor)
                permitted = allowed(db, 'job', evidence['job']['id'], actor)
            if permitted:
                result.add(printer_id)
        except Exception:
            continue
    return result


def enrich_job_facts(db, jobs, actor):
    """Display facts for already-visible jobs: nozzle/slot selections and what each job waits for."""
    from sqlalchemy import or_
    from modules.printers.models import Printer
    from modules.jobs.pinning import job_pin, preload_pin_printer_states
    from modules.printers.printer_models import bambu_nozzle_count
    from modules.jobs.waiting_reason import add_waiting_reasons, may_lack_a_printer
    # Pins may differ from the current assignment. Load both before filtering
    # display visibility so pin issues retain inactive/inaccessible/missing states.
    ids = {pid for j in jobs for pid in (j.printer_id, job_pin(j))} - {None}
    # A pending job may be waiting for a printer that can run it: the same single
    # printer read then also brings every active printer (still one read per list).
    fleet = any(may_lack_a_printer(job) for job in jobs)
    wanted = or_(Printer.id.in_(ids), Printer.is_active.is_(True)) if fleet else Printer.id.in_(ids)
    loaded = db.query(Printer).options(selectinload(Printer.filament_slots)).filter(wanted).all() if ids or fleet else []
    printers = [p for p in loaded if p.id in ids]
    by_id = {p.id: p for p in printers}
    # JobResponse also expands the assigned printer. Seed that relationship
    # without marking it dirty or issuing another lazy read during serialization.
    for job in jobs:
        set_committed_value(job, 'printer', by_id.get(job.printer_id))
    printers = [p for p in printers if may_read(actor, 'printers')
                and (check_org_access(actor, p.org_id) or p.shared)]
    facts = read_facts(db, printers)
    visible = {p.id: p for p in printers}
    recovery = _recovery_printers(db, list(visible), actor)
    # The recovery savepoints invalidate the session pin cache. Seed it from the
    # unfiltered batch after those reads, before waiting reasons and serialization.
    preload_pin_printer_states(db, ids, by_id.values())
    for job in jobs:
        job.printer_has_dispatch_claim = job.printer_id in recovery
        job.ams_slot_nozzles = []
        printer_id = job.printer_id or job_pin(job)
        printer = visible.get(printer_id)
        if printer is None or bambu_nozzle_count(printer.machine_type or printer.model) != 2:
            continue
        mapping = job.ams_mapping
        if isinstance(mapping, str):
            try:
                mapping = json.loads(mapping)
            except ValueError:
                continue
        if not isinstance(mapping, list):
            continue
        bindings = facts.get(printer_id, {}).get('slot_extruders', {})
        for source, tray in enumerate(mapping):
            if type(tray) is int and 0 <= tray < 16:
                extruder = bindings.get(str(tray + 1))
                job.ams_slot_nozzles.append({'source_index': source, 'slot_number': tray + 1,
                    'nozzle': ('right' if extruder == 0 else 'left') if type(extruder) is int and extruder in (0, 1) else None})
    return add_waiting_reasons(db, jobs, actor, active_printers=[p for p in loaded if p.is_active is True] if fleet else None)
