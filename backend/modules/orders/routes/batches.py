"""Fixed-yield recipes, inspected acceptance and read-only addition checks."""
import json
from core.rbac import require_action
from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from core.db import get_db
from core.rbac import require_role, require_interactive_session
from modules.orders import batch_production as service
from modules.orders.production_auth import require_production_operator
from modules.orders import square_store
from modules.orders.batch_schemas import RecipeInput, InspectionInput

from modules.orders.routes.show_orders import PrivateRoute

class InspectionRoute(PrivateRoute):
    invalid_detail='Invalid production evidence. Check required fields, whole quantities and physical confirmations.'

router = APIRouter(prefix='/products/production', tags=['Production inspection'],route_class=InspectionRoute)
viewer = require_role('viewer',scope='read')
admin = require_role('admin',scope='write')
operator = require_role('operator',scope='write')
interactive = Depends(require_interactive_session())


@router.get('')
def overview(offset: int = Query(default=0,ge=0), user=Depends(viewer), db: Session=Depends(get_db)):
    owner = square_store.tenant(user)
    recipes = [ {'id':r.id,**json.loads(r.definition)} for r in db.execute(select(service.recipes).where(service.recipes.c.owner==owner).order_by(service.recipes.c.id).offset(offset).limit(100)) ]
    receipts = [service.receipt_public(r) for r in db.execute(select(service.receipts).where(service.receipts.c.owner==owner).order_by(service.receipts.c.id.desc()).limit(100)).mappings()]
    # Expose receipt metadata, never transport bodies or credential documents.
    additions = [{'id':r.id,'receipt_id':r.receipt_id,'state':r.state,'attempts':r.attempts}
        for r in db.execute(select(service.additions).where(service.additions.c.owner==owner).order_by(service.additions.c.id).limit(100))]
    return {'recipes':recipes,'receipts':receipts,'additions':additions,'square_writes_enabled':square_store.writes_enabled(square_store.load(db, owner)[0]),
            'next_offset':offset+100 if len(recipes)==100 else None}


@router.get('/history')
def history(offset:int=Query(default=0,ge=0,le=1000000), query:str=Query(default='',max_length=100),
            state:str|None=Query(default=None,pattern='^(prepared|unknown|confirmed|withdrawn_baseline|in_baseline)$'),
            user=Depends(viewer),db:Session=Depends(get_db)):
    from modules.orders.production_history import history as read_history
    return read_history(db,square_store.tenant(user),offset=offset,query=query.strip(),state=state)


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


@router.post('/recipes',dependencies=[interactive, Depends(require_action('products.recipes', scope='write'))])
def create_recipe(data:RecipeInput,user=Depends(admin),db:Session=Depends(get_db)):
    return service.create_recipe(db,user,data)


@router.post('/inspections',dependencies=[Depends(require_production_operator()), Depends(require_action('products.inspections', scope='write:production'))])
def inspect_batch(data:InspectionInput,user=Depends(require_role('operator',scope='write:production')),db:Session=Depends(get_db)):
    return service.inspect_batch(db,user,data)


@router.post('/additions/{addition_id}/check',dependencies=[Depends(require_production_operator()), Depends(require_action('products.inspections', scope='write:production'))])
def check_addition(addition_id:str,user=Depends(require_role('operator',scope='write:production')),db:Session=Depends(get_db)):
    from modules.orders.stock_recovery import check
    return check(db,user,addition_id)


@router.get('/assortment')
def assortment_policy(user=Depends(viewer),db:Session=Depends(get_db)):
    from modules.orders.assortment import read
    return read(db,square_store.tenant(user))


from modules.orders.assortment import PolicyEdit


@router.put('/assortment/{product_id}',dependencies=[interactive,Depends(require_action('products.assortment',scope='write'))])
def save_assortment(product_id:int,data:PolicyEdit,user=Depends(admin),db:Session=Depends(get_db)):
    from modules.orders.assortment import edit
    return edit(db,user,product_id,data)


@router.post('/assortment/{product_id}/preview',dependencies=[interactive,Depends(require_action('products.assortment_preview',scope='write'))])
def preview_assortment(product_id:int,user=Depends(operator),db:Session=Depends(get_db)):
    from modules.orders.assortment import preview
    return preview(db,user,product_id)


from modules.orders.finishing import ComponentInput,FinishInput

@router.get('/finishing')
def finishing_overview(user=Depends(operator),db:Session=Depends(get_db)):
    from modules.orders.finishing import overview
    return overview(db,user)

@router.get('/finishing/{product_id}/requirements')
def finishing_requirements(product_id:int,user=Depends(operator),db:Session=Depends(get_db)):
    from modules.orders.finishing import preview
    return preview(db,user,product_id)

@router.post('/components',dependencies=[interactive,Depends(require_action('products.finishing',scope='write'))])
def inspect_components(data:ComponentInput,user=Depends(operator),db:Session=Depends(get_db)):
    from modules.orders.finishing import inspect
    return inspect(db,user,data)

@router.post('/finishing',dependencies=[interactive,Depends(require_action('products.finishing',scope='write'))])
def finish_components(data:FinishInput,user=Depends(operator),db:Session=Depends(get_db)):
    from modules.orders.finishing import finish
    return finish(db,user,data)
