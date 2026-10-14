"""Scoped read-only retained-attempt inspection. No device I/O or release."""
from fastapi import APIRouter,Depends,HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session
from core.db import get_db
from core.rbac import require_role,check_org_access

router=APIRouter()

@router.get('/printers/{printer_id}/dispatch-recovery',tags=['Printers'])
def inspect_dispatch_recovery(printer_id:int,current_user:dict=Depends(require_role('viewer',scope='read:printers')),db:Session=Depends(get_db)):
    # Single source snapshot; whitelisted fields only. Do not select secrets,
    # filenames or configuration fingerprints into this diagnostic response.
    row=db.execute(text('''SELECT p.org_id AS printer_org,c.owner_org_id,c.state,c.job_id,
        j.status AS job_status,j.hold,j.charged_to_org_id AS job_org,j.printer_id AS job_printer,
        r.claim_token IS NOT NULL AS receipt_present,
        o.id AS observed_id,o.status AS observed_status,
        s.id AS submission_id,s.status AS submission_status,s.org_id AS submission_org,
        mc.state AS education_claim_state
        FROM printers p LEFT JOIN printer_dispatch_claims c ON c.printer_id=p.id
        LEFT JOIN jobs j ON j.id=c.job_id
        LEFT JOIN dispatch_attempt_receipts r ON r.claim_token=c.token AND r.printer_id=p.id AND r.job_id=c.job_id
        LEFT JOIN print_jobs o ON o.id=r.observed_print_job_id AND o.printer_id=p.id AND o.scheduled_job_id=c.job_id
        LEFT JOIN education_submissions s ON s.job_id=c.job_id AND s.org_id=j.charged_to_org_id
        LEFT JOIN education_monitor_claims mc ON mc.submission_id=s.id AND mc.org_id=s.org_id AND mc.job_id=c.job_id AND mc.printer_id=p.id
        WHERE p.id=:id'''),{'id':printer_id}).mappings().first()
    if row is None or not check_org_access(current_user,row['printer_org']):
        raise HTTPException(404,'Printer not found')
    if row['state'] is None:
        return {'state':'no_retained_claim','release_available':False}
    if (row['owner_org_id'] is None or row['owner_org_id']!=row['printer_org']
            or not check_org_access(current_user,row['owner_org_id'])):
        raise HTTPException(404,'Printer not found')
    job_visible=(row['job_status'] is not None and row['job_org'] is not None
                 and check_org_access(current_user,row['job_org']) and row['job_printer']==printer_id)
    education_visible=(job_visible and row['submission_id'] is not None
                       and row['submission_org']==row['job_org'])
    return {'state':row['state'],'job_id':row['job_id'] if job_visible else None,
        'job':{'status':row['job_status'],'held':bool(row['hold'])} if job_visible else None,
        'receipt_present':bool(row['receipt_present']),
        'observed_print':{'id':row['observed_id'],'status':row['observed_status']} if job_visible and row['observed_id'] is not None else None,
        'education':{'submission_id':row['submission_id'],'status':row['submission_status'],
                     'reservation_state':row['education_claim_state']} if education_visible else None,
        'release_available':False,
        'guidance':'Retained evidence blocks repeat dispatch. Inspect the printer and sender before reconciliation. Idle status alone does not prove that no command can still arrive.'}
