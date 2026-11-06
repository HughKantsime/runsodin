"""Explicit, tenant-scoped term policy and cleanup planning (no content deletion)."""
import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import text
from core.dependencies import log_audit
from modules.organizations.education_access import require_tenant_admin

KINDS = ('files', 'timelapses', 'thumbnails', 'portfolio')
TERMINAL = {'completed', 'failed', 'rejected', 'cancelled'}
MAX_ITEMS = 500


def now():
    return datetime.now(timezone.utc)


def stamp(value=None):
    return (value or now()).astimezone(timezone.utc).isoformat(timespec='seconds')


def parsed(value):
    value = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


class TermPolicy(BaseModel):
    model_config = ConfigDict(extra='forbid')
    cost_center_id: int = Field(gt=0)
    name: str = Field(min_length=1, max_length=120)
    starts_at: datetime
    ends_at: datetime
    files_days: int | None = Field(default=None, ge=0, le=36500)
    timelapses_days: int | None = Field(default=None, ge=0, le=36500)
    thumbnails_days: int | None = Field(default=None, ge=0, le=36500)
    portfolio_days: int | None = Field(default=None, ge=0, le=36500)

    @model_validator(mode='after')
    def dates(self):
        if not self.name.strip() or self.starts_at.tzinfo is None or self.ends_at.tzinfo is None:
            raise ValueError('Name and timezone-qualified dates required')
        if self.ends_at <= self.starts_at:
            raise ValueError('Term end must follow start')
        return self


def authority(db, principal, org_id=None, *, lock=False):
    if lock:
        if db.get_bind().dialect.name == 'sqlite':
            connection = db.connection()
            if not connection.connection.driver_connection.in_transaction:
                connection.exec_driver_sql('BEGIN IMMEDIATE')
        else:
            db.execute(text("SET LOCAL lock_timeout = '1s'"))
            db.execute(text('SELECT id FROM users WHERE id=:id FOR UPDATE'), {'id': principal['id']})
    actor = db.execute(text('SELECT id,role,group_id,is_active FROM users WHERE id=:id'),
                       {'id': principal['id']}).mappings().first()
    if not actor or not actor['is_active']:
        raise HTTPException(403, 'Active school administrator required')
    return require_tenant_admin(dict(actor), org_id)


def policy(db, term_id, org_id, *, lock=False):
    # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- suffix is empty or literal FOR UPDATE selected by lock and dialect; term and org are bound
    row = db.execute(text('SELECT * FROM education_term_policies WHERE id=:id AND org_id=:org' +
                     (' FOR UPDATE' if lock and db.get_bind().dialect.name == 'postgresql' else '')),
                     {'id': term_id, 'org': org_id}).mappings().first()
    if not row:
        raise HTTPException(404, 'Term policy not found')
    return dict(row)


def audit(db, action, term_id, details):
    # IDs and controlled result codes only; never filenames, student names or paths.
    log_audit(db, 'term_retention_' + action, 'education_term', None,
              {'term_id': term_id, **details})


def create_policy(db, body, principal, org_id=None):
    org = authority(db, principal, org_id, lock=True)
    if not db.execute(text('SELECT 1 FROM education_cost_centers WHERE id=:id AND org_id=:org'),
                      {'id': body.cost_center_id, 'org': org}).first():
        raise HTTPException(404, 'Cost center not found')
    if db.get_bind().dialect.name == 'postgresql':
        db.execute(text('SELECT id FROM education_cost_centers WHERE id=:id FOR UPDATE'), {'id': body.cost_center_id})
    # Disallow overlapping windows so one policy cannot override another's keep choice.
    existing = db.execute(text('SELECT starts_at,ends_at FROM education_term_policies '
        'WHERE org_id=:org AND cost_center_id=:center'), {'org': org, 'center': body.cost_center_id})
    if any(parsed(row.starts_at) < body.ends_at and parsed(row.ends_at) > body.starts_at for row in existing):
        raise HTTPException(409, 'This cost center already has an overlapping term policy')
    values = {**body.model_dump(), 'id': str(uuid.uuid4()), 'org_id': org,
              'created_by': principal['id'], 'created_at': stamp(),
              'starts_at': stamp(body.starts_at), 'ends_at': stamp(body.ends_at)}
    db.execute(text('INSERT INTO education_term_policies '
        '(id,org_id,cost_center_id,name,starts_at,ends_at,files_days,timelapses_days,thumbnails_days,portfolio_days,created_by,created_at) '
        'VALUES (:id,:org_id,:cost_center_id,:name,:starts_at,:ends_at,:files_days,:timelapses_days,:thumbnails_days,:portfolio_days,:created_by,:created_at)'), values)
    audit(db, 'policy_created', values['id'], {'org_id': org})
    db.commit()
    return values


def submissions(db, term, *, after_id=0, limit=None):
    # Compare timestamps in Python: historical SQLite uses space-separated dates,
    # PostgreSQL uses native timestamps. Never compare mixed date strings lexically.
    rows = db.execute(text('SELECT s.*,j.status AS job_status,f.stored_path,f.blob_state,'
        'f.storage_bytes,o.expected_hash,o.final_path,o.state AS upload_state '
        'FROM education_submissions s JOIN jobs j ON j.id=s.job_id '
        'JOIN print_files f ON f.id=s.print_file_id JOIN education_upload_operations o ON o.operation_id=s.operation_id '
        'WHERE s.org_id=:org AND s.cost_center_id=:center AND s.id>:after ORDER BY s.id'),
        {'org': term['org_id'], 'center': term['cost_center_id'], 'after': after_id}).mappings()
    result = []
    for row in rows:
        if parsed(term['starts_at']) <= parsed(row['created_at']) < parsed(term['ends_at']):
            result.append(dict(row))
            if limit is not None and len(result) >= limit:
                break

    return result


def blocked_reason(db, row):
    if row['status'] not in TERMINAL or row['job_status'] not in TERMINAL:
        return 'work_not_terminal'
    p = {'job': row['job_id'], 'file': row['print_file_id'], 'model': row['model_id'],
         'sid': row['id'], 'org': row['org_id'], 'path': row['stored_path'], 'op': row['operation_id']}
    if db.execute(text('SELECT 1 FROM education_monitor_claims WHERE job_id=:job OR submission_id=:sid'), p).first() or db.execute(
            text('SELECT 1 FROM printer_dispatch_claims WHERE job_id=:job'), p).first():
        return 'unresolved_printer_claim'
    if row['upload_state'] not in ('committed', 'purged') or row['final_path'] != row['stored_path']:
        return 'upload_ownership_unconfirmed'
    queries = (
        'SELECT 1 FROM product_components WHERE model_id=:model',
        'SELECT 1 FROM print_presets WHERE model_id=:model',
        'SELECT 1 FROM education_submissions WHERE id<>:sid AND (print_file_id=:file OR model_id=:model OR operation_id=:op)',
        'SELECT 1 FROM jobs WHERE (model_id=:model OR print_file_id=:file) AND id<>:job',
        'SELECT 1 FROM models WHERE print_file_id=:file AND id<>:model',
        'SELECT 1 FROM print_files WHERE stored_path=:path AND id<>:file',
        'SELECT 1 FROM education_upload_operations WHERE final_path=:path AND operation_id<>:op',
        'SELECT 1 FROM print_archives WHERE (print_file_id=:file OR file_path=:path OR job_id=:job) '
        'AND (education_submission_id IS NULL OR education_submission_id<>:sid OR org_id IS NULL OR org_id<>:org)',
        'SELECT 1 FROM print_archives WHERE education_submission_id=:sid AND file_path IS NOT NULL AND file_path<>:path',
    )
    if any(db.execute(text(query), p).first() for query in queries):
        return 'shared_or_unconfirmed_content'
    return None


def media_rows(db, row):
    # Include unresolved legacy videos linked by their physical attempt. They must
    # be reported as blocked, never silently omitted from a successful cleanup.
    return [dict(r) for r in db.execute(text('SELECT DISTINCT t.id,t.status,t.ownership_operation_id,'
        'own.owner_state,own.owner_org_id,own.education_submission_id '
        'FROM timelapses t LEFT JOIN timelapse_ownership_anchors own ON own.operation_id=t.ownership_operation_id '
        'LEFT JOIN print_jobs p ON p.id=t.print_job_id '
        'WHERE own.education_submission_id=:sid OR own.scheduled_job_id=:job OR p.scheduled_job_id=:job ORDER BY t.id'),
        {'sid': row['id'], 'job': row['job_id']}).mappings()]


def content_revision(row):
    fields = ('id', 'org_id', 'job_id', 'print_file_id', 'model_id', 'submitted_by',
              'operation_id', 'status', 'job_status', 'lifecycle_revision',
              'stored_path', 'expected_hash', 'final_path', 'upload_state')
    return hashlib.sha256(json.dumps({k: row[k] for k in fields}, sort_keys=True, default=str).encode()).hexdigest()


def preview(db, term, *, at=None, after_id=0):
    at = at or now()
    due = {kind: term[kind + '_days'] is not None and
           at >= parsed(term['ends_at']) + timedelta(days=term[kind + '_days']) for kind in KINDS}
    items = []
    selected = submissions(db, term, after_id=after_id, limit=MAX_ITEMS + 1)
    for row in selected[:MAX_ITEMS]:
        blocked = blocked_reason(db, row)
        completed = set(db.execute(text("SELECT kind FROM education_term_cleanup WHERE term_id=:term AND submission_id=:sid AND state='done'"),
                                   {'term': term['id'], 'sid': row['id']}).scalars())
        media = media_rows(db, row) if due['timelapses'] else []
        category_blocks = {}
        if any(m['owner_state'] != 'known' or m['owner_org_id'] != row['org_id'] or m['education_submission_id'] != row['id'] for m in media):
            category_blocks['timelapses'] = 'timelapse_ownership_unconfirmed'
        item = {'submission_id': row['id'], 'user_id': row['submitted_by'],
                'revision': row['lifecycle_revision'], 'blocked_reason': blocked,
                'blocked_kinds': category_blocks,
                'kinds': [k for k in KINDS if due[k] and k not in completed],
                'timelapses': media,
                'content_revision': content_revision(row)}
        items.append(item)
    payload = {'policy': term, 'due': due, 'items': items, 'after_id': after_id,
               'next_after_id': items[-1]['submission_id'] if len(selected) > MAX_ITEMS else None}
    payload['fingerprint'] = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
    return payload


def delete_policy(db, term_id, principal, org_id=None):
    org = authority(db, principal, org_id, lock=True)
    term = policy(db, term_id, org, lock=True)
    if db.execute(text('SELECT 1 FROM education_term_cleanup WHERE term_id=:term'), {'term': term_id}).first():
        raise HTTPException(409, 'Policies with cleanup receipts cannot be deleted')
    db.execute(text('DELETE FROM education_term_policies WHERE id=:term'), {'term': term_id})
    audit(db, 'policy_deleted', term_id, {'org_id': org, 'cost_center_id': term['cost_center_id']})
    db.commit()
    return {'status': 'deleted'}
