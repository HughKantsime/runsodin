"""Printer deletion and owner notifications, shared by the CRUD route."""
import json
from fastapi import HTTPException
from fastapi.responses import Response
from sqlalchemy import text, bindparam
from core.rbac import check_org_access
from core.dependencies import log_audit
from modules.printers.models import Printer


def delete_printer(db, printer_id, current_user):
    printer = db.query(Printer).filter(Printer.id == printer_id).first()
    if not printer:
        raise HTTPException(status_code=404, detail="Printer not found")
    if not check_org_access(current_user, printer.org_id):
        raise HTTPException(status_code=404, detail="Printer not found")

    from core.registry import registry
    education_policy = registry.get_provider("EducationPolicyProvider")
    if education_policy is None:
        raise HTTPException(status_code=503, detail="Education policy unavailable")
    education_policy.assert_printer_tenant_change_or_delete_allowed(db, printer_id)
    from modules.organizations.services import scope_loss_warnings
    training_warnings = scope_loss_warnings(db, org_id=printer.org_id, removed_printer_ids={printer_id})
    from modules.printers.slot_feed_policy import refuse_delete_while_flagged
    refuse_delete_while_flagged(db, printer_id)
    printer_name = printer.name
    # Jobs pinned to this printer must not silently follow a reused id: mark
    # their pin deleted (they wait for a new choice) and tell their owners.
    from modules.jobs.services import clear_pins_for_deleted_printer
    from core.base import AlertType, AlertSeverity
    from modules.notifications.alert_dispatcher import stage_alert, deliver_staged_alert
    pins = clear_pins_for_deleted_printer(db, printer_id)
    live_jobs = pins.live
    # One alert per owner of a still-schedulable job (not per job), in bounded chunks.
    owners = {}
    for start in range(0, len(live_jobs), 500):
        for row in db.execute(text("SELECT id,charged_to_user_id,submitted_by FROM jobs WHERE id IN :ids")
                              .bindparams(bindparam("ids", expanding=True)), {"ids": live_jobs[start:start + 500]}).all():
            owner = row[1] or row[2]
            if owner is not None:
                owners.setdefault(owner, []).append(row[0])
    plans = []
    for owner, job_ids in sorted(owners.items()):
        count = len(job_ids)
        plan = stage_alert(db, AlertType.QUEUE_SKIPPED,
            AlertSeverity.WARNING, f"Pinned printer deleted: {printer_name}",
            f"{printer_name} was deleted. {count} queued job{'s' if count != 1 else ''} pinned to it "
            f"wait for a new printer choice.",
            job_id=job_ids[0],  # scopes the alert to this owner's job audience
            metadata={"reason": "pinned_printer_deleted", "job_ids": job_ids[:50], "job_count": count},
            target_user_ids=[owner])
        if plan is not None:
            plans.append(plan)
    db.delete(printer)
    log_audit(db, "printer.deleted", "printer", printer_id,
              {"name": printer_name, "pinned_job_count": len(pins.marked), "queued_pinned_jobs": live_jobs[:200]})
    db.commit()
    for plan in plans:
        deliver_staged_alert(db, plan)
    headers = {'X-ODIN-Training-Warnings': json.dumps(training_warnings)} if training_warnings else {}
    return Response(status_code=204, headers=headers)
