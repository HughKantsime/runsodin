"""Enterprise controlled-part marking. Existing organization and RBAC fences apply."""
from typing import Literal
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import text
from sqlalchemy.orm import Session
from core.db import get_db
from core.audit import log_audit, audit_download
from core.rbac import require_role, require_action, check_org_access
from core.controlled_parts import assert_access, member, policies, refuse, selected_file_sql
from license_manager import require_feature

router = APIRouter(prefix='/controlled-parts', tags=['Controlled parts'])
RESOURCES = {'files': ('print_files', 'file_id', 'org_id', 'file'),
             'jobs': ('jobs', 'job_id', 'charged_to_org_id', 'job')}


class MarkRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    controlled: bool = True
    acknowledge_siem: bool = False
    label: str = Field(default='CUI', min_length=1, max_length=80)
    audience_id: int = Field(gt=0)
    printer_tag: str = Field(min_length=1, max_length=100)
    retention_days: int = Field(ge=1, le=3650)
    reason: str = Field(min_length=1, max_length=500)

    @field_validator('label', 'printer_tag', 'reason')
    @classmethod
    def clean_text(cls, value):
        value = value.strip()
        if not value or any(ord(c) < 32 for c in value):
            raise ValueError('Use nonempty text without control characters')
        return value


def licensed(db, kind, identifier):
    try:
        require_feature('controlled_parts')
    except HTTPException:
        audit_download(db, 'controlled_part.refused', kind, identifier,
                       {'reason': 'feature_not_licensed'})
        raise


def resource(db, resource_type, identifier, actor, *, lock=False):
    table, key, org, kind = RESOURCES[resource_type]
    if lock:
        # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- table, key and owner column come from closed file/job mappings; marking values and identities are bound
        db.execute(text(f'UPDATE {table} SET id=id WHERE id=:id'), {'id': identifier})
    # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- table, key and owner column come from closed file/job mappings; marking values and identities are bound
    row = db.execute(text(f'SELECT {org} FROM {table} WHERE id=:id'), {'id': identifier}).first()
    if row is None or not check_org_access(actor, row[0]):
        raise HTTPException(404, 'Resource not found')
    assert_access(db, kind, identifier, actor)
    return table, key, kind


@router.get('/{resource_type}/{identifier}')
def get_mark(resource_type: Literal['files', 'jobs'], identifier: int,
             actor: dict = Depends(require_role('viewer')), db: Session = Depends(get_db)):
    licensed(db, resource_type, identifier)
    _, _, kind = resource(db, resource_type, identifier, actor)
    result = policies(db, kind, identifier)
    return {'controlled': bool(result), 'policies': result}


def change_mark(db, resource_type, identifier, body, actor):
    licensed(db, resource_type, identifier)
    # Serialize first-mark admission with SIEM configuration on both providers.
    from core.controlled_siem import lock_policy
    lock_policy(db)
    _, key, kind = resource(db, resource_type, identifier, actor)
    # Even platform admins must join the audience before marking or releasing.
    if body.controlled and not member(db, actor, body.audience_id):
        refuse(db, 'controlled_part_group_required', kind, identifier)
    if db.get_bind().dialect.name == 'sqlite':
        resource(db, resource_type, identifier, actor, lock=True)
    from core.controlled_marking import lock_related, assert_not_education
    from sqlalchemy.exc import OperationalError
    try:
        lock_related(db, kind, identifier)
    except OperationalError as error:
        db.rollback()
        if getattr(error.orig, 'sqlstate', None) == '55P03' or 'locked' in str(error).lower():
            raise HTTPException(409, {'code': 'controlled_part_busy_retry'}) from error
        raise
    assert_not_education(db, kind, identifier)
    # No reclassification while any linked dispatch/capture can be in flight.
    source = ':id' if kind == 'file' else '(' + selected_file_sql(':id') + ')'
    jobs = ('SELECT j.id FROM jobs j WHERE (' + selected_file_sql('j.id') + ') IN '
            '(SELECT id FROM print_files WHERE id=' + source + ' OR (stored_path IS NOT NULL '
            'AND stored_path=(SELECT stored_path FROM print_files WHERE id=' + source + ')))')
    if kind == 'job':
        jobs += ' UNION SELECT id FROM jobs WHERE id=:id'
    # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- table, key and owner column come from closed file/job mappings; marking values and identities are bound
    busy = db.execute(text("SELECT 1 FROM jobs WHERE id IN (" + jobs + ") AND status IN ('printing','paused') "
                           "UNION SELECT 1 FROM printer_dispatch_claims WHERE job_id IN (" + jobs + ") "
                           "AND state IN ('preparing','adapter_entered','accepted','uncertain') LIMIT 1"), {'id': identifier}).first()
    if busy:
        db.rollback()
        refuse(db, 'controlled_part_work_active', kind, identifier)
    table = 'controlled_' + resource_type
    # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- table, key and owner column come from closed file/job mappings; marking values and identities are bound
    before = db.execute(text(f'SELECT * FROM {table} WHERE {key}=:id'), {'id': identifier}).mappings().first()
    if body.controlled:
        from core.controlled_siem import first_mark
        if first_mark(db, actor, body.acknowledge_siem):
            log_audit(db, 'controlled_part.siem_acknowledged', 'system', None,
                      {'direction': 'first_mark', 'acknowledged': True})
    # Audience switches never grant extra time, including repeated A -> B -> A.
    from core.controlled_policy_clock import update_clock
    marked_at = update_clock(db, resource_type, identifier, before, body)
    previewed_at = (None if not before or body.retention_days < before['retention_days']
                    or before['audience_id'] != body.audience_id else before['purge_previewed_at'])
    if body.controlled:
        # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- table, key and owner column come from closed file/job mappings; marking values and identities are bound
        db.execute(text(f'''INSERT INTO {table}
            ({key},label,audience_id,printer_tag,retention_days,reason,changed_by,marked_at)
            VALUES (:id,:label,:audience_id,:printer_tag,:retention_days,:reason,:actor,:marked_at)
            ON CONFLICT ({key}) DO UPDATE SET label=excluded.label,audience_id=excluded.audience_id,
            printer_tag=excluded.printer_tag,retention_days=excluded.retention_days,
            reason=excluded.reason,changed_by=excluded.changed_by,marked_at=excluded.marked_at,purge_previewed_at=:previewed_at'''),
                   {**body.model_dump(exclude={'controlled', 'acknowledge_siem'}), 'id': identifier, 'actor': actor['id'], 'marked_at': marked_at, 'previewed_at': previewed_at})
    else:
        # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- table, key and owner column come from closed file/job mappings; marking values and identities are bound
        db.execute(text(f'DELETE FROM {table} WHERE {key}=:id'), {'id': identifier})
    log_audit(db, 'controlled_part.changed', kind, identifier,
              {'before': dict(before) if before else None, 'after': body.model_dump(), 'retention_extended': bool(before and body.retention_days > before['retention_days'])})
    db.commit()
    db.info.pop('_controlled_any', None)
    from core.controlled_mqtt import invalidate
    invalidate()
    return {'controlled': body.controlled}


@router.put('/files/{identifier}', dependencies=[Depends(require_role('operator'))])
def mark_file(identifier: int, body: MarkRequest,
              actor: dict = Depends(require_action('models.edit', scope='write:models')),
              db: Session = Depends(get_db)):
    return change_mark(db, 'files', identifier, body, actor)


@router.put('/jobs/{identifier}', dependencies=[Depends(require_role('operator'))])
def mark_job(identifier: int, body: MarkRequest,
             actor: dict = Depends(require_action('jobs.edit', scope='write:jobs')),
             db: Session = Depends(get_db)):
    return change_mark(db, 'jobs', identifier, body, actor)
