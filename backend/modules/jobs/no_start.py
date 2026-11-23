"""Record an operator's physical check against retained start attempts."""
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, field_validator
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from core.audit import log_audit
from core.controlled_parts import allowed
from core.db import get_db
from core.db_compat import as_utc_datetime
from core.display_names import filename_stem
from core.dispatch_claim_schema import PrinterDispatchClaim
from core.dispatch_payload_selection import SELECTED_PRINT_FILE_ID_SQL
from core.dispatch_receipt_schema import DispatchAttemptReceipt as Receipt
from core.dispatch_receipts import observed_filename_matches
from core.dispatch_recovery import telemetry
from core.models import AuditLog
from core.rbac import check_org_access, require_action
from modules.jobs.education_guard import assert_generic_job_mutation_allowed
from modules.jobs.models import Job

router = APIRouter(prefix='/jobs', tags=['Jobs'])
ACTION = 'operator.dispatch_no_start_reconciled'


class NoStartAttestation(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    version: Literal[1]
    printer_checked: Literal[True]
    job_did_not_start: Literal[True]

    @field_validator('version', 'printer_checked', 'job_did_not_start', mode='before')
    @classmethod
    def confirm(cls, value, info):
        if info.field_name == 'version':
            if type(value) is not int or value != 1:
                raise ValueError('Use version 1.')
        elif value is not True:
            raise ValueError('Confirm that you checked the printer and this job did not start.')
        return value


class NoStartRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    attestation: NoStartAttestation


def _recorded_attempts(db, ids):
    stamps = {}
    for jid, stamp, details in db.query(AuditLog.entity_id, AuditLog.timestamp, AuditLog.details).filter(
        AuditLog.entity_id.in_(ids), AuditLog.entity_type == 'job', AuditLog.action == ACTION
    ):
        if not isinstance(details, dict) or details.get('job_left_held') is not True or details.get('observed_print_job_id') is not None:
            continue
        token = details.get('claim_token')
        if not stamp or not isinstance(token, str) or details.get('attempt_id', token) != token:
            continue
        key = (jid, token)
        stamp = as_utc_datetime(stamp)
        stamps[key] = max(stamp, stamps.get(key, stamp))
    return stamps


def no_start_states(db, jobs):
    ids = [job.id for job in jobs if job.hold and job.status in ('pending', 'scheduled')]
    if not ids:
        return {}
    receipts = db.query(Receipt.job_id, Receipt.claim_token, Receipt.created_at, Receipt.observed_print_job_id).filter(
        Receipt.job_id.in_(ids), ~Receipt.job_id.in_(db.query(PrinterDispatchClaim.job_id)),
        text('NOT EXISTS (SELECT 1 FROM print_jobs p WHERE p.scheduled_job_id=dispatch_attempt_receipts.job_id)'),
        text('NOT EXISTS (SELECT 1 FROM dispatch_sender_registrations s WHERE s.job_id=dispatch_attempt_receipts.job_id)'),
        text('NOT EXISTS (SELECT 1 FROM education_submissions e WHERE e.job_id=dispatch_attempt_receipts.job_id)'),
    ).all()
    states = {}
    recorded = _recorded_attempts(db, {r.job_id for r in receipts}) if receipts else {}
    observed = set()
    for r in receipts:
        if r.observed_print_job_id is not None:
            observed.add(r.job_id)
        stamp = recorded.get((r.job_id, r.claim_token))
        if stamp is None or stamp < as_utc_datetime(r.created_at):
            states[r.job_id] = 'needs_check'
        else:
            states.setdefault(r.job_id, 'recorded')
    return {jid: state for jid, state in states.items() if jid not in observed}


def _check_printers(db, job, receipts, actor):
    from core.auth import SECRET_KEY
    targets = defaultdict(list)
    for receipt in receipts:
        targets[receipt.printer_id].append(receipt)
    if job.printer_id:
        targets.setdefault(job.printer_id, [])
    source = db.execute(text(
        'SELECT pf.original_filename, pf.filename, pf.project_name, m.name FROM jobs j '
        'LEFT JOIN models m ON m.id=j.model_id LEFT JOIN education_submissions s ON s.job_id=j.id '
        f'LEFT JOIN print_files pf ON pf.id=({SELECTED_PRINT_FILE_ID_SQL}) WHERE j.id=:id'
    ), {'id': job.id}).first()
    names = {filename_stem(name).casefold() for name in (job.item_name, *(source or ())) if name}
    checks = {}
    for pid in sorted(targets):
        printer = db.execute(text('SELECT org_id,shared,api_type,last_seen,gcode_state FROM printers WHERE id=:id'),
                             {'id': pid}).mappings().first()
        if printer and not (check_org_access(actor, printer['org_id']) or printer['shared']):
            raise HTTPException(404, 'Printer not found.')
        state = telemetry(db, {'printer': printer, 'target_jobs': [], 'observations': []}) if printer else 'status_unavailable'
        active = db.execute(text("SELECT filename,job_name,observed_received_at FROM print_jobs WHERE printer_id=:pid AND ended_at IS NULL "
                                 "AND lower(status) IN ('printing','running','paused')"), {'pid': pid}).all()
        # A recent print observation can arrive before the printer heartbeat.
        if state == 'status_unavailable':
            now = datetime.now(timezone.utc)
            active = [row for row in active if row.observed_received_at and
                      timedelta(0) <= now - as_utc_datetime(row.observed_received_at) <= timedelta(seconds=10)]
        observed = [name for row in active for name in (row.filename, row.job_name) if name]
        for name in observed:
            if filename_stem(name).casefold() in names or any(observed_filename_matches(
                SECRET_KEY.encode(), r.claim_token, printer['api_type'] if printer else None, name, r.observed_filename_key
            ) for r in targets[pid]):
                raise HTTPException(409, 'The printer is reporting a print that matches this job.')
        if observed:
            checks[str(pid)] = 'different_print'
        elif state is None:
            checks[str(pid)] = 'idle'
        else:
            checks[str(pid)] = 'operator_check_only'
    return checks


@router.post('/{job_id}/dispatch-attempts/no-start')
def record_no_start(job_id: int, body: NoStartRequest,
                    actor=Depends(require_action('jobs.cancel', scope='write:jobs')), db=Depends(get_db)):
    try:
        # Cover empty evidence sets as well as existing rows. Serialize retries before reading audits.
        if db.get_bind().dialect.name == 'postgresql':
            db.execute(text('LOCK TABLE dispatch_attempt_receipts, dispatch_sender_registrations, education_submissions, '
                            'print_jobs, printer_dispatch_claims, printers IN SHARE MODE NOWAIT'))
        else:
            db.execute(text('BEGIN IMMEDIATE'))
        job = db.query(Job).filter_by(id=job_id).with_for_update(nowait=True).populate_existing().one_or_none()
        if not job or not check_org_access(actor, job.charged_to_org_id) or not allowed(db, 'job', job_id, actor):
            raise HTTPException(404, 'Job not found.')
        assert_generic_job_mutation_allowed(db, job_id)
        if not job.hold or job.status not in ('pending', 'scheduled'):
            raise HTTPException(409, 'The job must be pending or scheduled and on hold.')
        for query, reason in (
            ('SELECT 1 FROM printer_dispatch_claims WHERE job_id=:id', "Ask an administrator to use the printer’s Dispatch recovery panel."),
            ('SELECT 1 FROM print_jobs WHERE scheduled_job_id=:id', 'A print is already linked to this job.'),
            ('SELECT 1 FROM dispatch_sender_registrations WHERE job_id=:id', 'A sender is still recorded for this job. Ask an administrator to check its start request.'),
        ):
            if db.execute(text(query), {'id': job_id}).first():
                raise HTTPException(409, reason)
        receipts = db.query(Receipt).filter_by(job_id=job_id).all()
        if not receipts:
            raise HTTPException(409, 'This job has no recorded start request.')
        if any(r.observed_print_job_id is not None for r in receipts):
            raise HTTPException(409, 'ODIN observed a print for a start request on this job.')
        newest = max(as_utc_datetime(r.created_at) for r in receipts)
        clock = 'SELECT clock_timestamp()' if db.get_bind().dialect.name == 'postgresql' else 'SELECT CURRENT_TIMESTAMP'
        if as_utc_datetime(db.execute(text(clock)).scalar_one()) - newest < timedelta(minutes=10):
            raise HTTPException(409, 'Wait at least 10 minutes after the latest start request and check the printer again.')
        printer_checks = _check_printers(db, job, receipts, actor)
        recorded = _recorded_attempts(db, [job_id])
        count = 0
        for r in receipts:
            stamp = recorded.get((job_id, r.claim_token))
            if stamp is not None and stamp >= as_utc_datetime(r.created_at):
                continue
            log_audit(db, ACTION, 'job', job_id, {
                'claim_token': r.claim_token, 'attempt_id': r.claim_token, 'job_left_held': True,
                'observed_print_job_id': None, 'actor_user_id': actor['id'], 'actor_token_id': actor.get('_token_id'),
                'attestation': body.attestation.model_dump(), 'printer_checks': printer_checks,
                'job_status': job.status.value, 'claim_present': False, 'linked_print_present': False,
                'sender_present': False, 'latest_start_request': newest.isoformat(),
            })
            count += 1
        db.commit()
        return {'recorded': count}
    except OperationalError as exc:
        db.rollback()
        if getattr(exc.orig, 'sqlstate', None) in ('55P03', '40P01', '40001') or 'locked' in str(exc.orig).lower():
            raise HTTPException(409, 'The job or printer is being updated. Check again in a moment.') from None
        raise
    except Exception:
        db.rollback()
        raise
