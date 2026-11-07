"""Education lifecycle policy extracted from the public policy module."""
from __future__ import annotations
import hashlib
import json
import re
import secrets
import uuid
from contextlib import contextmanager
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError as SQLAlchemyIntegrityError
from sqlalchemy.orm import Session
from core.interfaces.education_policy import is_education_reserved_filename, parse_education_token
from modules.jobs.services import transition_job, transition_result, Expr



def resolve_active_monitor_observation(executor, *, printer_id: int) -> dict:
    """Resolve the unique durable Education observation after monitor restart."""
    adapter = owner._ExecutionAdapter(executor)
    rows = adapter.fetchall(
        "SELECT p.id AS print_job_id,mc.submission_id,mc.job_id,mc.authority_revision "
        "FROM education_monitor_claims mc JOIN print_jobs p "
        "ON p.scheduled_job_id=mc.job_id AND p.printer_id=mc.printer_id "
        "WHERE mc.printer_id=:printer_id AND mc.state='running' AND p.status='running' "
        "ORDER BY p.id LIMIT 2",
        {"printer_id": printer_id},
    )
    if len(rows) > 1:
        return {
            "education_owned": True,
            "education_reserved": True,
            "authorized": False,
            "ambiguous": True,
        }
    if len(rows) == 1:
        return {
            "education_owned": True,
            "education_reserved": True,
            "authorized": True,
            **rows[0],
        }
    quarantine_rows = adapter.fetchall(
        "SELECT id AS print_job_id,filename,job_name FROM print_jobs "
        "WHERE printer_id=:printer_id AND scheduled_job_id IS NULL AND status='running' "
        "ORDER BY id DESC LIMIT 20",
        {"printer_id": printer_id},
    )
    quarantined = [
        row
        for row in quarantine_rows
        if owner._resolve_monitor_tokens(row.get("filename"), row.get("job_name"))[1]
    ]
    if len(quarantined) == 1:
        return {
            "education_owned": False,
            "education_reserved": True,
            "authorized": not owner._resolve_monitor_tokens(
                quarantined[0].get("filename"), quarantined[0].get("job_name"))[2],
            "print_job_id": quarantined[0]["print_job_id"],
        }
    return {
        "education_owned": False,
        "education_reserved": bool(quarantined),
        "authorized": False,
        "ambiguous": len(quarantined) > 1,
    }



def recovery_authority(db, *, evidence, principal, lock=False):
    """Recovery authority works for retained running claims, never dispatches.

    School administrators retain authority over every school center. Platform
    admins must use a school account; printer ownership alone grants no school
    authority. No lifecycle, reservation, quota or accounting changes occur.
    """
    from modules.organizations.education_access import _education_token_allowed
    if not _education_token_allowed(principal,write=True):
        return False
    actor = db.execute(text('SELECT id,role,group_id,is_active FROM users WHERE id=:id'),{'id':principal['id']}).mappings().first()
    if not actor or not actor['is_active'] or actor['role']!='admin' or actor['group_id'] is None:
        return False
    pg = db.get_bind().dialect.name=='postgresql'
    for submission in evidence['submissions']:
        if submission['org_id']!=actor['group_id'] or submission['approved_printer_id']!=evidence['printer']['id'] or submission['org_id']!=evidence['job']['charged_to_org_id']:
            return False
        args = {'org':submission['org_id'],'center':submission['cost_center_id'],'pid':evidence['printer']['id'],'sid':submission['id'],'jid':submission['job_id']}
        for query in (
            'SELECT id FROM education_cost_centers WHERE id=:center AND org_id=:org AND state=\'active\'',
            'SELECT printer_id FROM education_cost_center_printers WHERE cost_center_id=:center AND org_id=:org AND printer_id=:pid AND state=\'active\'',
            'SELECT id FROM education_submissions WHERE id=:sid AND job_id=:jid AND org_id=:org',
            'SELECT id FROM jobs WHERE id=:jid',
            'SELECT claim_id FROM education_monitor_claims WHERE submission_id=:sid AND org_id=:org AND job_id=:jid AND printer_id=:pid ORDER BY claim_id',
        ):
            if not db.execute(text(query+(' FOR UPDATE NOWAIT' if lock and pg else '')),args).first():  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- verified safe — constant query plus a fixed dialect suffix; values are bound
                # Terminal submissions may legitimately have no monitor claim.
                if not query.startswith('SELECT claim_id'): return False
    return bool(evidence['submissions'])



def recovery_terminal_resolved(db, *, evidence):
    """Require genuine monitor lifecycle history, not physical-only terminal."""
    if evidence['reservations'] or len(evidence['submissions'])!=1 or not evidence['claim']:
        return False
    submission = evidence['submissions'][0]
    job = evidence['job']
    claim = evidence['claim']
    if submission['status'] not in ('completed','failed','cancelled') or submission['status']!=job['status']:
        return False
    receipt = next((r for r in evidence['receipts'] if r['claim_token']==claim['token']),None)
    observation = next((o for o in evidence['observations'] if receipt and o['id']==receipt['observed_print_job_id'] and o['scheduled_job_id']==job['id']),None)
    if not observation or observation['status']!=job['status'] or not observation['ended_at'] or not job['actual_end']:
        return False
    events = db.execute(text("SELECT details_json,result_json FROM education_audit_events WHERE org_id=:org AND resource_type='submission' AND resource_id=:sid AND actor_kind='system' AND actor_id='monitor' AND action=:action AND lifecycle_revision=:rev"),
        {'org':submission['org_id'],'sid':str(submission['id']),'action':'submission.print_'+job['status'],'rev':submission['lifecycle_revision']}).mappings().all()
    if len(events)!=1:
        return False
    try:
        details=json.loads(events[0]['details_json']); result=json.loads(events[0]['result_json'])
        if details.get('print_job_id')!=observation['id'] or details.get('from_status')!='printing' or details.get('to_status')!=job['status'] or result.get('authorized') is not True or result.get('transitioned') is not True or result.get('job_id')!=job['id']:
            return False
        # Receipt filename binding is a keyed proof of the Education token
        # observed by the monitor; the original token remains reserved forever.
        from core.dispatch_receipts import observed_filename_matches
        from core.auth import SECRET_KEY
        names=db.execute(text('SELECT filename,job_name FROM print_jobs WHERE id=:id'),{'id':observation['id']}).mappings().one()
        protocol=evidence['printer']['api_type']
        return observed_filename_matches(SECRET_KEY.encode(),claim['token'],protocol,names['job_name'] if protocol=='bambu' else names['filename'],receipt['observed_filename_key'])
    except (ValueError,TypeError,KeyError):
        return False



def recovery_visibility(db, *, evidence, principal):
    """Use the established submission audience for minimal inspection fields.

    Read visibility never implies the admin and Education-write authority
    required to mutate a retained reservation.
    """
    from core.education_archive_audience import _SUBMISSION_AUDIENCE
    actor=db.execute(text('SELECT id,role,group_id,is_active FROM users WHERE id=:id'),{'id':principal.get('id')}).mappings().first()
    if not actor or not actor['is_active'] or actor['group_id'] is None:
        return False
    for submission in evidence['submissions']:
        if submission['org_id']!=actor['group_id'] or submission['org_id']!=evidence['job']['charged_to_org_id']:
            return False
        query='SELECT 1 FROM education_submissions s JOIN education_cost_centers c ON c.id=s.cost_center_id AND c.org_id=s.org_id WHERE s.id=:sid AND s.org_id=:org AND '+_SUBMISSION_AUDIENCE
        if not db.execute(text(query),{'sid':submission['id'],'org':submission['org_id'],'user_id':actor['id'],'is_admin':int(actor['role']=='admin')}).first():  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- verified safe — query is a module constant; values are bound
            return False
    return bool(evidence['submissions'])

# Import after definitions so both the public facade and this module can load first.
import modules.organizations.education_policy as owner
