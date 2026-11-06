"""Enterprise traceability, inspection, delivery and signed certificate exports."""
import io
import json
import zipfile
from datetime import datetime, timezone
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from fastapi.responses import Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text
from sqlalchemy.orm import Session
from core.db import get_db
from core.dependencies import log_audit
from core.rbac import require_action
from core import traceability as service
from license_manager import feature_gate
from modules.jobs.traceability_certificate import certificate, signed, signing_identity, certificate_branding

router = APIRouter(prefix='/jobs', tags=['Traceability'], dependencies=[Depends(feature_gate('traceability'))])


class Inspection(BaseModel):
    result: Literal['pass', 'fail']
    notes: str = Field(default='', max_length=4000)

    @field_validator('notes')
    @classmethod
    def printable_notes(cls, value):
        if any((ord(c) < 32 and c not in '\n\t') or 0xD800 <= ord(c) <= 0xDFFF or ord(c) == 127 for c in value):
            raise ValueError('Inspection notes must contain printable text')
        return value


class Policy(BaseModel):
    org_id: int = Field(ge=0)
    target_kind: Literal['printer_tag', 'product']
    target_key: str = Field(min_length=1, max_length=100)
    required: bool


def policy_owner(db, user, org_id):
    if not (user['role'] == 'admin' and user.get('group_id') is None) and org_id != user.get('group_id'):
        raise HTTPException(404, 'Organization not found')
    if not service.lock_policy_scope(db, org_id):
        raise HTTPException(404, 'Organization not found')
    return org_id


def load(db, user, job_id, *, mutate=False, attempt=None):
    job = service.owned_job(db, job_id, user, mutate=mutate, attempt=attempt)
    retained = service.record(db, job, attempt=attempt)
    return job, retained, service.render_record(db, job, retained)


@router.get('/traceability/policies')
def list_policies(org_id: int = Query(ge=0), user=Depends(require_action('jobs.inspection_policy', scope='read:jobs')), db: Session=Depends(get_db)):
    owner = policy_owner(db, user, org_id)
    result = service.rows(db, 'SELECT * FROM traceability_policies WHERE owner_id=:id ORDER BY target_kind,target_key', id=owner)
    log_audit(db, 'traceability.policies_read', 'group', owner)
    db.commit()
    return [{"org_id":r['owner_id'],"target_kind":r['target_kind'],"target_key":r['target_key'],"required":bool(r['required'])} for r in result]


@router.put('/traceability/policies')
def put_policy(body: Policy, user=Depends(require_action('jobs.inspection_policy', scope='write:jobs')), db: Session=Depends(get_db)):
    owner = policy_owner(db, user, body.org_id)
    key = body.target_key.strip()
    if not key:
        raise HTTPException(422, 'A printer tag or product ID is required')
    if body.target_kind == 'product':
        if not key.isascii() or not key.isdecimal() or int(key) <= 0:
            raise HTTPException(422, 'Product ID must be a positive integer')
        key = str(int(key))
        if not service.one(db, 'SELECT id FROM products WHERE id=:id AND (org_id=:owner OR (:owner=0 AND org_id IS NULL))', id=int(key), owner=owner):
            raise HTTPException(404, 'Product not found')
    db.execute(text('''INSERT INTO traceability_policies(owner_id,org_id,target_kind,target_key,required)
        VALUES(:owner,:org,:kind,:key,:required) ON CONFLICT(owner_id,target_kind,target_key)
        DO UPDATE SET required=excluded.required'''), {'owner':owner,'org':owner or None,'kind':body.target_kind,'key':key,'required':int(body.required)})
    log_audit(db, 'traceability.policy_changed', 'group', owner, {'kind':body.target_kind,'key':key,'required':body.required})
    db.commit()
    return {'org_id':owner,'target_kind':body.target_kind,'target_key':key,'required':body.required}


@router.get('/traceability/export')
def batch_export(start: datetime, end: datetime, user=Depends(require_action('jobs.traceability', scope='read:jobs')), db: Session=Depends(get_db)):
    from modules.jobs.traceability_batch import export_batch
    return export_batch(db, user, start, end, load)


@router.get('/{job_id}/traceability')
def get_record(job_id: int, response: Response, attempt: int | None=Query(default=None, ge=0), user=Depends(require_action('jobs.traceability', scope='read:jobs')), db: Session=Depends(get_db)):
    _, retained, result = load(db, user, job_id, attempt=attempt)
    response.headers['Cache-Control'] = 'no-store'
    log_audit(db, 'traceability.read', 'job', job_id, {'attempt':retained['attempt']})
    db.commit()
    return result


@router.get('/{job_id}/certificate')
def get_certificate(job_id: int, bundle: bool=False, attempt: int | None=Query(default=None, ge=0), user=Depends(require_action('jobs.traceability', scope='read:jobs')), db: Session=Depends(get_db)):
    return certificate_response(db, user, job_id, bundle=bundle, attempt=attempt)


@router.get('/{job_id}/certificate.sig')
def get_signature(job_id: int, attempt: int | None=Query(default=None, ge=0), user=Depends(require_action('jobs.traceability', scope='read:jobs')), db: Session=Depends(get_db)):
    return certificate_response(db, user, job_id, attempt=attempt, signature_only=True)


def certificate_response(db, user, job_id, *, bundle=False, attempt=None, signature_only=False):
    identity = signing_identity()
    job, retained, record = load(db, user, job_id, attempt=attempt)
    branding = certificate_branding(db, job['charged_to_org_id'])
    db.commit()
    pdf = certificate(None, record, job['charged_to_org_id'], branding=branding, identity=identity)
    signature = signed(pdf, identity)
    data, media, extension = pdf, 'application/pdf', 'pdf'
    if signature_only:
        data, media, extension = json.dumps(signature).encode(), 'application/json', 'pdf.sig'
    elif bundle:
        output = io.BytesIO()
        with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('certificate.pdf', pdf)
            archive.writestr('signature.json', json.dumps(signature))
            archive.writestr('traceability.json', json.dumps(jsonable_encoder(record), ensure_ascii=False))
        data, media, extension = output.getvalue(), 'application/zip', 'zip'
    log_audit(db, 'traceability.certificate_exported', 'job', job_id, {'attempt':retained['attempt'],'sha256':signature['sha256']})
    db.commit()
    return Response(data, media_type=media, headers={
        'Content-Disposition':f'attachment; filename="job-{job_id}-certificate.{extension}"',
        'Cache-Control':'no-store', 'X-ODIN-Signature':signature['signature'], 'X-ODIN-Public-Key':signature['public_key'],
        'X-ODIN-SHA256':signature['sha256'],
    })


@router.put('/{job_id}/inspection')
def inspect_job(job_id: int, body: Inspection, user=Depends(require_action('jobs.inspect', scope='write:jobs')), db: Session=Depends(get_db)):
    job, retained, _ = load(db, user, job_id, mutate=True)
    if retained['delivered_at']:
        raise HTTPException(409, 'Delivered inspection is locked')
    db.execute(text('''UPDATE traceability_records SET inspection_result=:result,inspection_notes=:notes,
        inspected_by=:user,inspected_at=:at WHERE job_id=:id AND attempt=:seq'''),
        {'id':job_id,'seq':retained['attempt'],'result':body.result,'notes':body.notes,'user':user['id'],'at':service.now()})
    log_audit(db, 'traceability.inspected', 'job', job_id, {'attempt':retained['attempt'],'result':body.result})
    result = service.render_record(db, job, service.record(db, job))
    db.commit()
    return result


@router.post('/{job_id}/deliver')
def deliver_job(job_id: int, user=Depends(require_action('jobs.deliver', scope='write:jobs')), db: Session=Depends(get_db)):
    job, retained, result = load(db, user, job_id, mutate=True)
    from core.traceability_delivery import assert_inspected_for_delivery
    try:
        assert_inspected_for_delivery(db, job, retained)
    except HTTPException as exc:
        log_audit(db, 'traceability.delivery_refused', 'job', job_id, {'reason':exc.detail})
        db.commit()
        raise
    if not retained['delivered_at']:
        db.execute(text('''UPDATE traceability_records SET delivered_by=:user,delivered_at=:at
            WHERE job_id=:id AND attempt=:seq'''), {'id':job_id,'seq':retained['attempt'],'user':user['id'],'at':service.now()})
        log_audit(db, 'traceability.delivered', 'job', job_id, {'attempt':retained['attempt']})
        result = service.render_record(db, job, service.record(db, job))
    db.commit()
    return result
