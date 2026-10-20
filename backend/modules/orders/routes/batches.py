"""Fixed-yield recipes, physical inspection and prepared Square additions.

There is deliberately no route that sends an addition to Square.
"""
import json
from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from core.db import get_db
from core.rbac import require_role, require_interactive_session
from modules.orders import batch_production as service
from modules.orders import square_store
from modules.orders.batch_schemas import RecipeInput, InspectionInput

router = APIRouter(prefix='/products/production', tags=['Production inspection'])
viewer = require_role('viewer',scope='read')
admin = require_role('admin',scope='write')
operator = require_role('operator',scope='write')
interactive = Depends(require_interactive_session())


@router.get('')
def overview(offset: int = Query(default=0,ge=0), user=Depends(viewer), db: Session=Depends(get_db)):
    owner = square_store.tenant(user)
    recipes = [ {'id':r.id,**json.loads(r.definition)} for r in db.execute(select(service.recipes).where(service.recipes.c.owner==owner).order_by(service.recipes.c.id).offset(offset).limit(100)) ]
    receipts = [service.receipt_public(r) for r in db.execute(select(service.receipts).where(service.receipts.c.owner==owner).order_by(service.receipts.c.id.desc()).limit(100)).mappings()]
    additions = [{'id':r.id,'receipt_id':r.receipt_id,'state':r.state,'attempts':r.attempts,
                  'destination':json.loads(r.destination),'request':json.loads(r.request_body)}
        for r in db.execute(select(service.additions).where(service.additions.c.owner==owner).order_by(service.additions.c.id).limit(100))]
    return {'recipes':recipes,'receipts':receipts,'additions':additions,'square_writes_enabled':False,
            'next_offset':offset+100 if len(recipes)==100 else None}


@router.get('/jobs')
def eligible_jobs(offset: int=Query(default=0,ge=0), user=Depends(operator), db: Session=Depends(get_db)):
    from fastapi import HTTPException
    owner=square_store.tenant(user)
    from modules.jobs.models import Job
    clause=Job.charged_to_org_id.is_(None) if owner==0 else Job.charged_to_org_id==owner
    candidates=db.execute(select(Job.id).where(clause,Job.status=='completed').order_by(Job.id.desc()).limit(100).offset(offset)).scalars().all()
    result=[]
    for job_id in candidates:
        try:
            evidence=service.job_evidence(db,owner,job_id)
        except HTTPException as exc:
            if exc.status_code in (403,404,409):continue
            raise
        result.append({'id':job_id,'name':evidence['item_name'],'payload':service.recipe_payload(evidence)})
    return {'jobs':result,'next_offset':offset+100 if len(candidates)==100 else None}


@router.post('/recipes',dependencies=[interactive])
def create_recipe(data:RecipeInput,user=Depends(admin),db:Session=Depends(get_db)):
    return service.create_recipe(db,user,data)


@router.post('/inspections',dependencies=[interactive])
def inspect_batch(data:InspectionInput,user=Depends(operator),db:Session=Depends(get_db)):
    return service.inspect_batch(db,user,data)
