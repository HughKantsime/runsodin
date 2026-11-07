"""Admin-managed explicit audiences, with audited membership replacement."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text, bindparam
from sqlalchemy.orm import Session
from core.db import get_db
from core.rbac import require_role, require_superadmin, require_interactive_session, require_action
from core.audit import log_audit
from core.controlled_audience_schema import audiences
from license_manager import require_feature

router = APIRouter(prefix='/controlled-parts/audiences', tags=['Controlled parts'], dependencies=[Depends(require_interactive_session())])


class AudienceRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    user_ids: list[int] = Field(max_length=10000)

    @field_validator('name')
    @classmethod
    def clean_name(cls, value):
        value = value.strip()
        if not value or any(ord(c) < 32 for c in value):
            raise ValueError('Use a nonempty audience name without control characters')
        return value


@router.get('')
def list_audiences(actor: dict = Depends(require_superadmin()), db: Session = Depends(get_db)):
    require_feature('controlled_parts')
    query = 'SELECT a.id,a.name FROM controlled_audiences a'
    # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- audience listing selects one of two literal scope queries; user id is bound
    return [dict(r) for r in db.execute(text(query + ' ORDER BY a.id'), {'uid': actor['id']}).mappings()]


@router.get('/eligible')
def eligible_audiences(actor: dict = Depends(require_role('viewer')), db: Session = Depends(get_db)):
    """Selection only: a user sees their active memberships, never the roster."""
    require_feature('controlled_parts')
    return [dict(r) for r in db.execute(text(
        'SELECT a.id,a.name FROM controlled_audiences a JOIN controlled_audience_members m ON m.audience_id=a.id '
        'JOIN users u ON u.id=m.user_id WHERE u.id=:uid AND u.is_active IS TRUE ORDER BY a.id'),
        {'uid': actor['id']}).mappings()]


@router.get('/{audience_id}')
def get_audience(audience_id: int, actor: dict = Depends(require_superadmin()), db: Session = Depends(get_db)):
    require_feature('controlled_parts')
    row = db.execute(text('SELECT id,name FROM controlled_audiences WHERE id=:id'), {'id': audience_id}).mappings().first()
    if not row:
        raise HTTPException(404, 'Audience not found')
    ids = db.execute(text('SELECT user_id FROM controlled_audience_members WHERE audience_id=:id ORDER BY user_id'), {'id': audience_id}).scalars().all()
    return {**row, 'user_ids': ids}


def save(db, body, actor, audience_id=None):
    require_feature('controlled_parts')
    before = []
    if audience_id is not None:
        lock_audience(db, audience_id)
        before = db.execute(text('SELECT user_id FROM controlled_audience_members WHERE audience_id=:id ORDER BY user_id'), {'id': audience_id}).scalars().all()
    ids = sorted(set(body.user_ids))
    query = text('SELECT id FROM users WHERE id IN :ids AND is_active IS TRUE').bindparams(bindparam('ids', expanding=True))
    if set(db.execute(query, {'ids': ids}).scalars()) != set(ids):
        raise HTTPException(422, 'Audience members must be active users')
    if audience_id is None:
        audience_id = db.execute(audiences.insert().values(name=body.name.strip(), created_by=actor['id'])).inserted_primary_key[0]
    else:
        if not db.execute(text('UPDATE controlled_audiences SET name=:name WHERE id=:id'), {'id': audience_id, 'name': body.name.strip()}).rowcount:
            raise HTTPException(404, 'Audience not found')
        db.execute(text('DELETE FROM controlled_audience_members WHERE audience_id=:id'), {'id': audience_id})
    for uid in ids:
        db.execute(text('INSERT INTO controlled_audience_members(audience_id,user_id) VALUES(:id,:uid)'), {'id': audience_id, 'uid': uid})
    log_audit(db, 'controlled_audience.saved', 'controlled_audience', audience_id, {'before': before, 'after': ids, 'self_added': actor['id'] in ids and actor['id'] not in before})
    db.commit()
    return {'id': audience_id, 'name': body.name.strip(), 'user_ids': ids}


@router.post('', dependencies=[Depends(require_action('settings.edit'))])
def create_audience(body: AudienceRequest, actor: dict = Depends(require_superadmin()), db: Session = Depends(get_db)):
    return save(db, body, actor)


@router.put('/{audience_id}', dependencies=[Depends(require_action('settings.edit'))])
def update_audience(audience_id: int, body: AudienceRequest, actor: dict = Depends(require_superadmin()), db: Session = Depends(get_db)):
    return save(db, body, actor, audience_id)


@router.delete('/{audience_id}', dependencies=[Depends(require_action('settings.edit'))])
def delete_audience(audience_id: int, actor: dict = Depends(require_superadmin()), db: Session = Depends(get_db)):
    require_feature('controlled_parts')
    lock_audience(db, audience_id)
    from core.controlled_parts import refuse
    if db.execute(text('SELECT 1 FROM controlled_files WHERE audience_id=:id UNION ALL SELECT 1 FROM controlled_jobs WHERE audience_id=:id UNION ALL SELECT 1 FROM controlled_job_tombstones WHERE audience_id=:id'), {'id': audience_id}).first():
        refuse(db, 'controlled_part_audience_in_use', 'controlled_audience', audience_id, status_code=409)
    before = db.execute(text('SELECT user_id FROM controlled_audience_members WHERE audience_id=:id ORDER BY user_id'), {'id': audience_id}).scalars().all()
    db.execute(text('DELETE FROM controlled_audience_members WHERE audience_id=:id'), {'id': audience_id})
    db.execute(text('DELETE FROM controlled_audiences WHERE id=:id'), {'id': audience_id})
    log_audit(db, 'controlled_audience.deleted', 'controlled_audience', audience_id, {'before': before, 'after': [], 'self_added': False})
    db.commit()
    return {'deleted': True}


def lock_audience(db, audience_id):
    # Take the write lock before reading membership: concurrent replacements serialize.
    if db.get_bind().dialect.name == 'postgresql':
        db.execute(text('SELECT id FROM controlled_audiences WHERE id=:id FOR UPDATE'), {'id': audience_id})
    else:
        db.execute(text('UPDATE controlled_audiences SET id=id WHERE id=:id'), {'id': audience_id})
