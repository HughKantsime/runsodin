"""Private Education work queues and target-serialized, revision-checked ordering."""
import hashlib
import json
import uuid
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from core.errors import ErrorCode, OdinError
from modules.jobs.services import education_order, waiting_rows
from modules.organizations.education_access import is_superadmin, require_tenant_admin
from modules.organizations.education_admin_support import write_audit

TERMINAL = {'completed', 'failed', 'rejected', 'cancelled'}


def conflict(message='Queue changed; reload required'):
    return OdinError(ErrorCode.revision_conflict, message, status=409)


def authority(db, principal, org_id=None, write=False):
    actor = db.execute(text('SELECT id,role,group_id,is_active FROM users WHERE id=:id'),
                       {'id': principal['id']}).mappings().first()
    if not actor or not actor['is_active']:
        raise OdinError(ErrorCode.not_found, 'Education account not found', status=404)
    if is_superadmin(actor):
        if write:
            raise OdinError(ErrorCode.permission_denied, 'Use a school account to change Education work', status=403)
        org_id = require_tenant_admin(dict(actor), org_id)
    else:
        if actor['group_id'] is None or (org_id is not None and org_id != actor['group_id']):
            raise OdinError(ErrorCode.not_found, 'Organization not found', status=404)
        org_id = actor['group_id']
    centers = None if actor['role'] == 'admin' else set(db.execute(text(
        "SELECT g.cost_center_id FROM education_cost_center_grants g "
        "JOIN education_cost_centers c ON c.id=g.cost_center_id AND c.org_id=g.org_id "
        "WHERE g.user_id=:uid AND g.org_id=:org AND g.role='manager' "
        "AND g.state='active' AND c.state='active'"), {'uid': actor['id'], 'org': org_id}).scalars())
    if centers == set():
        raise OdinError(ErrorCode.permission_denied, 'Education manager access required', status=403)
    return int(org_id), centers


def revision(rows):
    return hashlib.sha256(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()


def list_queue(db, *, principal, kind, org_id=None, archived='all', cost_center_id=None):
    org, centers = authority(db, principal, org_id)
    rows = [dict(row) for row in db.execute(text(
        "SELECT s.id,s.org_id,s.job_id,s.cost_center_id,s.submitted_by,s.status,s.lifecycle_revision,"
        "s.archived_at,s.print_file_id,s.approved_printer_id,j.item_name,j.status AS job_status,j.printer_id,"
        "j.priority,j.queue_position,j.created_at,j.hold,u.username AS submitter_username,"
        "c.display_name AS center_name,p.name AS printer_name,p.gcode_state,p.hms_errors,p.is_active AS printer_active,"
        "EXISTS(SELECT 1 FROM printer_dispatch_claims d WHERE d.job_id=j.id) AS committed,"
        "EXISTS(SELECT 1 FROM education_monitor_claims m WHERE m.job_id=j.id) AS monitor_claim "
        "FROM education_submissions s JOIN jobs j ON j.id=s.job_id AND j.charged_to_org_id=s.org_id "
        "JOIN education_cost_centers c ON c.id=s.cost_center_id AND c.org_id=s.org_id "
        "JOIN users u ON u.id=s.submitted_by LEFT JOIN printers p ON p.id=j.printer_id AND p.org_id=s.org_id "
        "WHERE s.org_id=:org ORDER BY j.priority,(j.queue_position IS NULL),"
        "j.queue_position,j.created_at,j.id"), {'org': org}).mappings()]
    visible = [row for row in rows if (centers is None or row['cost_center_id'] in centers)
               and (cost_center_id is None or row['cost_center_id'] == cost_center_id)]
    if kind != 'jobs':
        items = [row for row in visible if row['status'] == 'submitted' and not row['archived_at']] if kind == 'review' else [row for row in visible
                 if row['status'] in TERMINAL and (archived == 'all' or
                    (row['archived_at'] is not None) == (archived == 'archived'))]
        for row in items:
            row.pop('hms_errors', None)
            row['archived'] = row['archived_at'] is not None
            row['can_archive'] = (not is_superadmin(principal) and not row['archived']
                and row['status'] in TERMINAL and row['job_status'] in TERMINAL
                and not row['committed'] and not row['monitor_claim'])
        return {'items': items}
    groups = []
    for pid in sorted({row['printer_id'] for row in visible if row['job_status'] in {'pending','scheduled','printing'} and row['printer_id'] is not None}):
        # The displayed rows and ETag come from the same SELECT snapshot.
        waiting = [{
            'id': row['job_id'], 'printer_id': row['printer_id'],
            'queue_position': row['queue_position'], 'priority': row['priority'],
            'created_at': row['created_at'], 'status': row['job_status'], 'hold': row['hold'],
            'org_id': row['org_id'], 'cost_center_id': row['cost_center_id'],
            'lifecycle_revision': row['lifecycle_revision'], 'submission_id': row['id'],
            'committed': row['committed'], 'monitor_claim': row['monitor_claim'],
        } for row in rows if row['printer_id'] == pid and row['job_status'] in ('pending', 'scheduled')]
        eligible = [r for r in waiting if not r['committed'] and not r['monitor_claim']]
        ordered = education_order(eligible, {r['id'] for r in eligible}, lambda r,k:r[k])
        ranks = {r['id']: index for index,r in enumerate(ordered)}
        items = [r for r in visible if r['printer_id'] == pid and r['job_status'] in {'pending','scheduled','printing'}]
        for row in items:
            row['section'] = 'printing' if row['job_status'] == 'printing' else 'committed' if row['committed'] or row['monitor_claim'] else 'waiting'
            row['can_reorder'] = row['section'] == 'waiting' and not is_superadmin(principal)
            row['blocked_reason'] = None
            if ranks.get(row['job_id']) == 0:
                row['blocked_reason'] = head_block_reason(db, row, org)
            # Do not expose unparsed device diagnostic text in this projection.
            row.pop('hms_errors', None)
        items.sort(key=lambda r: ({'printing':0,'committed':1,'waiting':2}[r['section']], ranks.get(r['job_id'],0)))
        groups.append({'printer_id': pid, 'printer_name': items[0]['printer_name'],
                       'revision': revision(waiting), 'items': items})
    return {'printers': groups}


def head_block_reason(db, row, org):
    if row['hold']:
        return 'On hold'
    if not row['printer_active']:
        return 'Printer is inactive'
    if db.execute(text('SELECT 1 FROM printer_dispatch_claims WHERE printer_id=:pid'),
                  {'pid': row['printer_id']}).first():
        return 'Printer has an unresolved dispatch claim; reconciliation required'
    if row['job_status'] == 'pending':
        return 'Awaiting scheduling'
    from modules.printers.services import evaluate_submission_compatibility
    compatibility = evaluate_submission_compatibility(db, org_id=org,
        print_file_id=row['print_file_id'], printer_id=row['printer_id'])
    if compatibility is None:
        return 'Printer compatibility is unavailable'
    if not compatibility['compatible']:
        return '; '.join(reason['detail'] for reason in compatibility['reasons'])
    if row['gcode_state'] not in ('IDLE', 'FINISH', 'idle', 'ready'):
        return 'Waiting for an idle printer and fresh readiness confirmation'
    if row.get('hms_errors') and row['hms_errors'] != '[]':
        return 'Printer has recorded HMS diagnostics; inspect and reconcile before printing'
    return 'Live readiness, AMS, bed clearance and sender checks run before print start'


def reorder_queue(db, *, principal, printer_id, body):
    from core.auth import SECRET_KEY
    from core.dispatch_identity import build_identity
    from core.dispatch_target_lock import acquire_target_lock
    from modules.printers.dispatch import _get_printer_info
    # Release the authentication read snapshot before target serialization.
    db.rollback()
    creds = _get_printer_info(printer_id)
    key = SECRET_KEY.encode() if isinstance(SECRET_KEY, str) else None
    identity = build_identity(creds, key) if creds else None
    if identity is None:
        raise conflict('Printer configuration cannot be verified')
    acquired = acquire_target_lock(db.get_bind(), identity.target_key)
    if acquired.status != 'acquired':
        raise conflict('Printer queue is busy or target locking is unavailable')
    handle = acquired.handle
    try:
        if db.get_bind().dialect.name == 'sqlite':
            db.connection().exec_driver_sql('BEGIN IMMEDIATE')
        else:
            db.execute(text('SELECT id FROM users WHERE id=:id FOR UPDATE NOWAIT'), {'id': principal['id']})
            db.execute(text('SELECT id FROM printers WHERE id=:pid FOR UPDATE NOWAIT'), {'pid': printer_id})
            db.execute(text('SELECT id FROM education_cost_centers WHERE org_id=:org FOR SHARE NOWAIT'), {'org': principal.get('group_id')})
            db.execute(text('SELECT id FROM jobs WHERE printer_id=:pid FOR UPDATE NOWAIT'), {'pid': printer_id})
            db.execute(text('SELECT user_id FROM education_cost_center_grants WHERE user_id=:id FOR UPDATE NOWAIT'), {'id': principal['id']})
        org, centers = authority(db, principal, write=True)
        current = _get_printer_info(printer_id, connection=db.connection())
        if not handle.held() or build_identity(current, key) != identity or current.get('org_id') != org:
            raise conflict('Printer authority changed')
        rows = waiting_rows(db, printer_id)
        if revision(rows) != body.revision:
            raise conflict()
        eligible = [r for r in rows if r['org_id'] == org and not r['committed'] and not r['monitor_claim']]
        ordered = education_order(eligible, {r['id'] for r in eligible}, lambda r,k:r[k])
        permitted = [r['id'] for r in ordered if centers is None or r['cost_center_id'] in centers]
        if not permitted:
            raise OdinError(ErrorCode.permission_denied, 'No authorized waiting jobs on this printer', status=403)
        ids = body.job_ids
        if len(ids) != len(set(ids)) or set(ids) != set(permitted):
            raise OdinError(ErrorCode.validation_failed, 'Provide every authorized waiting job exactly once', status=422)
        replacements = iter(ids)
        final = [next(replacements) if r['id'] in permitted else r['id'] for r in ordered]
        for rank, job_id in enumerate(final):
            db.execute(text('UPDATE jobs SET queue_position=:rank WHERE id=:id'), {'rank': rank, 'id': job_id})
        result = {'revision': revision(waiting_rows(db, printer_id)), 'job_ids': ids}
        write_audit(db, org_id=org, actor_id=principal['id'], action='queue.reorder',
            command_id=str(uuid.uuid4()), request_digest=hashlib.sha256(json.dumps(ids).encode()).hexdigest(),
            resource_type='printer', resource_id=printer_id, cost_center_id=None, lifecycle_revision=None,
            details={'before': [r['id'] for r in ordered], 'after': final}, result=result)
        if not handle.held():
            raise conflict('Printer target ownership changed')
        db.commit()
        return result
    except OperationalError:
        db.rollback()
        raise conflict('Queue is busy; reload and retry') from None
    except Exception:
        db.rollback()
        raise
    finally:
        handle.close()
