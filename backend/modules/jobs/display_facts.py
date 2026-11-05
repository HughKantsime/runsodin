"""Derive display-only nozzle/slot selections for already-visible jobs."""
import json
from core.rbac import check_org_access
from core.printer_display_facts import read_facts, may_read


def enrich_job_facts(db, jobs, actor):
    from modules.printers.models import Printer
    from modules.jobs.pinning import job_pin
    from modules.printers.printer_models import bambu_nozzle_count
    ids = {j.printer_id or job_pin(j) for j in jobs} - {None}
    printers = db.query(Printer).filter(Printer.id.in_(ids)).all() if ids and may_read(actor, 'printers') else []
    printers = [p for p in printers if check_org_access(actor, p.org_id) or p.shared]
    facts = read_facts(db, printers)
    visible = {p.id: p for p in printers}
    for job in jobs:
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
    return jobs
