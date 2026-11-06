"""D4 Settings/import routes: interactive tenant admin, matrix and license gates."""
from typing import Literal
from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session
from core.db import get_db
from core.rbac import require_action, require_interactive_session
from license_manager import feature_gate
from modules.organizations.education_access import require_education_principal
from modules.organizations import education_roster_store as store, education_roster_sync as sync
from modules.organizations.education_roster_providers import refuse


def private(response: Response):
    response.headers['Cache-Control'] = 'private, no-store'


router = APIRouter(prefix='/education/roster', tags=['Education roster'], dependencies=[
    Depends(feature_gate('roster_sync')), Depends(feature_gate('classes')), Depends(private),
    Depends(require_action('education.roster_manage')), Depends(require_interactive_session())])
principal_dep = require_education_principal(write=True)


class Configuration(BaseModel):
    provider: Literal['google', 'canvas']
    enabled: bool = False
    revision: int = Field(default=0, ge=0)
    config: dict = Field(default_factory=dict)


class Preview(BaseModel):
    course_id: str = Field(min_length=1, max_length=255)


class AccountConfirmation(BaseModel):
    subject: str = Field(min_length=1, max_length=255)
    user_id: int
    mode: Literal['identity', 'grant_only']
    decision: Literal['confirm', 'decline'] = 'confirm'


class Confirmation(BaseModel):
    preview_token: str = Field(min_length=32, max_length=64)
    account_confirmations: list[AccountConfirmation] = Field(default_factory=list, max_length=10000)


class TombstoneClear(BaseModel):
    preview_token: str = Field(min_length=32, max_length=64)
    clear_handle: str = Field(min_length=32, max_length=64)


@router.get('')
def settings(principal=Depends(principal_dep), db: Session = Depends(get_db)):
    org = sync.admin_org(db, principal)
    return {'items': [store.status(r) for r in db.execute(text(
        'SELECT * FROM education_roster_connections WHERE org_id=:o ORDER BY provider'), {'o': org}).mappings()]}


@router.put('')
def configure(body: Configuration, principal=Depends(principal_dep), db: Session = Depends(get_db)):
    return store.save(db, principal, body)


@router.post('/{connection_id}/tombstones/clear')
def clear_tombstone(connection_id: str, body: TombstoneClear, principal=Depends(principal_dep), db: Session = Depends(get_db)):
    org = sync.admin_org(db, principal)
    connection = store.load(db, connection_id, org, lock=True)
    plan = store.consume(db, body.preview_token, connection_id, 'preview', principal['id'])
    if plan['revision'] != connection['revision'] or plan['local'] != sync.local_state(db, connection):
        refuse('roster_preview_stale')
    subject_hash = plan.get('clear_handles', {}).get(body.clear_handle)
    if not subject_hash:
        refuse('roster_tombstone_not_found', 404)
    removed = db.execute(text('DELETE FROM education_roster_tombstones WHERE connection_id=:c AND subject_hash=:s'),
                         {'c': connection_id, 's': subject_hash})
    if removed.rowcount != 1:
        refuse('roster_tombstone_not_found', 404)
    store.audit(db, 'tombstone_cleared', org, resource=connection_id, user_id=principal['id'], subject_hash=subject_hash)
    db.commit()
    return {'cleared': True}


@router.post('/{connection_id}/connect')
def connect(connection_id: str, request: Request, principal=Depends(principal_dep), db: Session = Depends(get_db)):
    from modules.organizations.routes_roster_auth import begin
    org = sync.admin_org(db, principal)
    connection = store.load(db, connection_id, org)
    if connection['provider'] != 'google':
        refuse('roster_canvas_launch_required')
    return begin(db, connection, request, actor_id=principal['id'])


@router.get('/{connection_id}/courses')
async def courses(connection_id: str, principal=Depends(principal_dep), db: Session = Depends(get_db)):
    connection = store.load(db, connection_id, sync.admin_org(db, principal))
    return {'items': await sync.courses(db, connection)}


@router.post('/{connection_id}/preview')
async def preview(connection_id: str, body: Preview, principal=Depends(principal_dep), db: Session = Depends(get_db)):
    connection = store.load(db, connection_id, sync.admin_org(db, principal))
    roster = await sync.source(db, connection, body.course_id)
    return sync.preview(db, principal, connection, roster)


@router.post('/{connection_id}/import')
def import_roster(connection_id: str, body: Confirmation, principal=Depends(principal_dep), db: Session = Depends(get_db)):
    try:
        return sync.apply(db, principal, connection_id, body.preview_token, [c.model_dump() for c in body.account_confirmations])
    except Exception:
        db.rollback()
        store.audit(db, 'import_refused', principal.get('group_id'), resource=connection_id, reason='import_refused')
        db.commit()
        raise
