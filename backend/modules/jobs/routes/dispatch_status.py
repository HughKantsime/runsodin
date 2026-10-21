"""Read-only dispatch evidence. Never grants retry or hardware authority."""
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session
from core.db import get_db
from core.rbac import check_org_access, require_role
from core.dispatch_claim_schema import PrinterDispatchClaim
from core.dispatch_receipt_schema import DispatchAttemptReceipt
from core.dispatch_receipts import observed_filename_matches
from modules.jobs.models import Job
from modules.printers.models import Printer

router = APIRouter(prefix='/jobs', tags=['Jobs'])


@router.get('/{job_id}/dispatch-status')
def dispatch_status(job_id: int,
                    current_user=Depends(require_role('viewer', scope='read:jobs')),
                    db: Session = Depends(get_db)):
    job = db.get(Job, job_id)
    if job is None or not check_org_access(current_user, job.charged_to_org_id):
        raise HTTPException(404, 'Job not found')
    result = {'job_id': job_id, 'phase': 'unconfirmed', 'retry_blocked': True,
              'observation': None}
    printer = db.get(Printer, job.printer_id) if job.printer_id else None
    if printer is None:
        return result
    if not check_org_access(current_user, printer.org_id) and not printer.shared:
        raise HTTPException(404, 'Printer not found')
    claim = db.get(PrinterDispatchClaim, printer.id)
    if claim is None:
        # Staging may precede the durable claim. Absence proves no safe retry.
        return result
    if claim.job_id != job.id or claim.owner_org_id != printer.org_id:
        result['phase'] = 'target_busy'
        return result
    result['phase'] = {'preparing':'preparing', 'adapter_entered':'sending',
                       'accepted':'start_accepted', 'uncertain':'needs_reconciliation'}[claim.state]
    receipt = db.get(DispatchAttemptReceipt, claim.token)
    if (receipt is None or receipt.job_id != job.id or receipt.printer_id != printer.id
            or receipt.configuration_key != claim.configuration_key
            or receipt.observed_print_job_id is None):
        return result
    observed = db.execute(text('SELECT printer_id,scheduled_job_id,filename,job_name,'
                              'current_layer,total_layers,progress_percent,status,observed_received_at '
                              'FROM print_jobs WHERE id=:id'),
                          {'id':receipt.observed_print_job_id}).mappings().one_or_none()
    if observed is None or observed['printer_id'] != printer.id or observed['scheduled_job_id'] != job.id:
        return result
    from core.auth import SECRET_KEY
    key = SECRET_KEY.encode() if isinstance(SECRET_KEY, str) else None
    name = observed['job_name'] if printer.api_type == 'bambu' else observed['filename']
    if key is None or not observed_filename_matches(key, claim.token, printer.api_type,
                                                   name, receipt.observed_filename_key):
        return result
    stamp = observed['observed_received_at']
    try:
        stamp = datetime.fromisoformat(str(stamp).replace('Z', '+00:00'))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        age = max(0, int((datetime.now(timezone.utc) - stamp).total_seconds()))
    except (ValueError, TypeError):
        return result
    # Layer evidence is historical. This endpoint does not assert current motion.
    result['observation'] = {'current_layer':observed['current_layer'],
                             'total_layers':observed['total_layers'],
                             'progress_percent':observed['progress_percent'],
                             'status':observed['status'], 'start_observation_age_seconds':age}
    if (observed['current_layer'] or 0) > 0:
        result['phase'] = 'layer_observed'
    return result
