"""Interactive school administration: explicit dry run, content purge and graduation."""
from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from core.db import get_db
from core.rbac import require_action, require_interactive_session
from license_manager import require_feature
from modules.organizations.education_access import require_education_principal
from modules.organizations.education_term_retention import (
    TermPolicy, authority, create_policy, delete_policy, policy, preview, audit,
)
from modules.organizations.education_term_cleanup import purge
from modules.organizations.education_term_privacy import (
    anonymize, eligible_student, term_student_ids, pending_erasures, resume_erasure,
)


def licensed():
    require_feature('term_retention')


router = APIRouter(prefix='/education/term-retention', tags=['Education retention'], dependencies=[
    Depends(licensed), Depends(require_interactive_session()), Depends(require_action('settings.edit')),
])


class Confirm(BaseModel):
    model_config = ConfigDict(extra='forbid')
    after_id: int = Field(default=0, ge=0)
    fingerprint: str = Field(pattern=r'^[0-9a-f]{64}$')


class Graduate(Confirm):
    user_ids: list[int] = Field(min_length=1, max_length=100)


@router.get('')
def list_policies(response: Response, org_id: int | None = Query(None, gt=0),
                  principal=Depends(require_education_principal()), db=Depends(get_db)):
    org = authority(db, principal, org_id)
    rows = [dict(r) for r in db.execute(text('SELECT * FROM education_term_policies WHERE org_id=:org ORDER BY ends_at DESC'),
                                      {'org': org}).mappings()]
    pending = pending_erasures(db, org)
    audit(db, 'policies_read', None, {'org_id': org})
    db.commit()
    response.headers['Cache-Control'] = 'private, no-store'
    return {'items': rows, 'pending_erasures': pending}


@router.post('/erasures/{user_id}/resume')
def resume_student_erasure(user_id: int, org_id: int | None = Query(None, gt=0),
                          principal=Depends(require_education_principal(write=True)), db=Depends(get_db)):
    return resume_erasure(db, user_id, principal, org_id)


@router.post('')
def add_policy(body: TermPolicy, org_id: int | None = Query(None, gt=0),
               principal=Depends(require_education_principal(write=True)), db=Depends(get_db)):
    return create_policy(db, body, principal, org_id)


@router.delete('/{term_id}')
def remove_policy(term_id: str, org_id: int | None = Query(None, gt=0),
                  principal=Depends(require_education_principal(write=True)), db=Depends(get_db)):
    return delete_policy(db, term_id, principal, org_id)


@router.get('/{term_id}/dry-run')
def dry_run(term_id: str, response: Response, after_id: int = Query(0, ge=0), org_id: int | None = Query(None, gt=0),
            principal=Depends(require_education_principal()), db=Depends(get_db)):
    org = authority(db, principal, org_id)
    term = policy(db, term_id, org)
    result = preview(db, term, after_id=after_id)
    result['eligible_student_ids'] = sorted(uid for uid in term_student_ids(db, term) if eligible_student(db, uid, org))
    result['eligible_students'] = [dict(r) for uid in result['eligible_student_ids'] for r in db.execute(
        text('SELECT id,username FROM users WHERE id=:uid'), {'uid': uid}).mappings()]
    audit(db, 'dry_run', term_id, {'org_id': org, 'fingerprint': result['fingerprint']})
    db.commit()
    response.headers['Cache-Control'] = 'private, no-store'
    return result


@router.post('/{term_id}/purge')
def execute_purge(term_id: str, body: Confirm, request: Request, org_id: int | None = Query(None, gt=0),
                  principal=Depends(require_education_principal(write=True)), db=Depends(get_db)):
    org = authority(db, principal, org_id)
    planned = preview(db, policy(db, term_id, org), after_id=body.after_id)
    retire = None
    if any(i['timelapses'] and 'timelapses' in i['kinds'] and not i['blocked_reason']
           and not i['blocked_kinds'].get('timelapses') for i in planned['items']):
        from modules.organizations.education_term_media import media_retirement
        retire = media_retirement(request, db, principal)
    return purge(db, term_id, body.fingerprint, principal, org, retire_media=retire, after_id=body.after_id)


@router.post('/{term_id}/anonymize')
def graduate(term_id: str, body: Graduate, org_id: int | None = Query(None, gt=0),
                   principal=Depends(require_education_principal(write=True)), db=Depends(get_db)):
    return anonymize(db, term_id, body.user_ids, body.fingerprint, principal, org_id, after_id=body.after_id)
