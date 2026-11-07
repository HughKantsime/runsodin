"""Preview and acknowledge the first purge for each controlled policy revision."""
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session
from core.db import get_db
from core.rbac import require_role, require_action, require_interactive_session, check_org_access
from core.audit import log_audit
from core.controlled_parts import allowed, refuse, policies
from core.controlled_retention import expired
from license_manager import require_feature
from core.controlled_csrf import require_controlled_ack

router = APIRouter(prefix='/controlled-parts/retention', tags=['Controlled parts'])


class TombstoneSnapshot(BaseModel):
    job_id: int
    audit_max_id: int


class PurgeAcknowledgement(BaseModel):
    files: list[int] = []
    jobs: list[int] = []
    tombstones: list[TombstoneSnapshot] = []


@router.get('/preview')
def preview(actor: dict = Depends(require_role('operator')), db: Session = Depends(get_db)):
    require_feature('controlled_parts')
    marks = {}
    for resource, key, kind, table, org in (
        ('files', 'file_id', 'file', 'print_files', 'org_id'),
        ('jobs', 'job_id', 'job', 'jobs', 'charged_to_org_id'),
    ):
        query = f'SELECT c.{key},r.{org} FROM controlled_{resource} c JOIN {table} r ON r.id=c.{key} WHERE c.purge_previewed_at IS NULL'
        # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- resource, key, table and owner column come only from the closed files/jobs tuple
        marks[resource] = [r[0] for r in db.execute(text(query))
                           if check_org_access(actor, r[1]) and allowed(db, kind, r[0], actor)]
    from core.controlled_parts import member
    marks['tombstones'] = [dict(job_id=job, audit_max_id=bound) for job, bound in sorted({
        (r.job_id, r.audit_max_id) for r in db.execute(text(
            'SELECT job_id,audit_max_id,audience_id FROM controlled_job_tombstones WHERE purge_previewed_at IS NULL'))
        if member(db, actor, r.audience_id)})]
    from core.timelapse_access import timelapse_visibility
    visible, parameters = timelapse_visibility(actor)
    # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- resource table and key come from a closed files/jobs mapping; audience predicates use bound identities
    due = [r[0] for r in db.execute(text('SELECT id FROM timelapses WHERE ' + visible), parameters) if expired(db, r[0])]
    return {**marks, 'due_timelapse_ids': due, 'due_count': len(due)}


@router.post('/acknowledge', dependencies=[Depends(require_interactive_session()), Depends(require_action('jobs.edit')), Depends(require_controlled_ack)])
def acknowledge(body: PurgeAcknowledgement, actor: dict = Depends(require_role('operator')), db: Session = Depends(get_db)):
    require_feature('controlled_parts')
    from core.controlled_parts import assert_access
    from modules.organizations.routes_controlled_parts import resource
    for name, kind in (('files', 'file'), ('jobs', 'job')):
        for identifier in getattr(body, name):
            resource(db, name, identifier, actor, lock=True)
    from core.controlled_parts import member
    tombstones = []
    for snapshot in body.tombstones:
        values = {'id': snapshot.job_id, 'bound': snapshot.audit_max_id}
        where = ' WHERE job_id=:id AND audit_max_id=:bound'
        # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- resource table and key come from a closed files/jobs mapping; audience predicates use bound identities
        db.execute(text('UPDATE controlled_job_tombstones SET job_id=job_id' + where), values)
        # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- resource table and key come from a closed files/jobs mapping; audience predicates use bound identities
        rows = db.execute(text('SELECT audience_id FROM controlled_job_tombstones' + where), values).scalars().all()
        entitled = [audience for audience in rows if member(db, actor, audience)]
        if not entitled:
            raise HTTPException(404, 'Retention policy not found')
        tombstones.extend((snapshot.job_id, snapshot.audit_max_id, audience) for audience in entitled)
    now = datetime.now(timezone.utc).isoformat()
    for identifier, bound, audience in tombstones:
        db.execute(text('UPDATE controlled_job_tombstones SET purge_previewed_at=:now '
                        'WHERE job_id=:id AND audit_max_id=:bound AND audience_id=:audience AND purge_previewed_at IS NULL'),
                   {'id': identifier, 'bound': bound, 'audience': audience, 'now': now})
    for name, key in (('files', 'file_id'), ('jobs', 'job_id')):
        for identifier in getattr(body, name):
            # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- resource table and key come from a closed files/jobs mapping; audience predicates use bound identities
            db.execute(text(f'UPDATE controlled_{name} SET purge_previewed_at=:now WHERE {key}=:id'),
                       {'id': identifier, 'now': datetime.now(timezone.utc).isoformat()})
    log_audit(db, 'controlled_part.retention_acknowledged', 'system', details=body.model_dump())
    db.commit()
    return {'acknowledged': True}
