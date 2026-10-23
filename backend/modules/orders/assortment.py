"""Explicit show assortment policy, never a stock ledger or scheduler."""
import copy
import json
import uuid
from decimal import Decimal, InvalidOperation

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal
from sqlalchemy import String, cast, delete, select, update, literal_column
from sqlalchemy.exc import IntegrityError
from core.models import SystemConfig
from modules.orders import square_store as store, batch_production as batches
from modules.orders.models import Product
from modules.orders.square_client import SquareReader

PREFIX='production_assortment:'


class Policy(BaseModel):
    model_config=ConfigDict(extra='forbid')
    assortment:Literal['active','trial','inactive']
    kind:Literal['multicolor_character','dragon_jumbo','dragon_half','dragon_other','other']


class PolicyEdit(Policy):
    revision:str|None=Field(default=None,max_length=64)


def key(owner):return PREFIX+str(owner)


def rule(policy):
    if not policy:return {'trigger_below':None,'minimum_main_batch':None,'routine_replenishment':False}
    return {'trigger_below':15 if policy['kind'] in ('dragon_jumbo','dragon_half') else 5,
            'minimum_main_batch':5 if policy['kind']=='multicolor_character' else 1,
            'routine_replenishment':policy['assortment']=='active'}


def load(db,owner):
    raw=db.execute(select(SystemConfig.value).where(SystemConfig.key==key(owner))).scalar_one_or_none()
    if raw is None:return {'version':1,'owner':owner,'revision':None,'products':{}},None
    try:
        doc=json.loads(raw)
        if doc['version']!=1 or doc['owner']!=owner or not isinstance(doc['products'],dict) or len(doc['products'])>1000 or not isinstance(doc['revision'],str):raise ValueError()
        for pid,entry in doc['products'].items():
            if not pid.isdigit() or int(pid)<=0:raise ValueError()
            Policy.model_validate(entry)
        return doc,raw
    except (KeyError,TypeError,ValueError):
        raise HTTPException(503,'Assortment policy cannot be validated. Retain it for administrator review.') from None


def save(db,owner,doc,previous):
    doc['revision']=uuid.uuid4().hex;raw=batches.packed(doc)
    try:
        if previous is None:
            db.add(SystemConfig(key=key(owner),value=raw));db.flush()
        elif db.execute(update(SystemConfig).where(SystemConfig.key==key(owner),
            cast(SystemConfig.value,String)==json.dumps(previous)).values(value=raw).execution_options(synchronize_session=False)).rowcount!=1:
            raise HTTPException(409,'Assortment changed. Reload before saving.')
    except IntegrityError:
        db.rollback();raise HTTPException(409,'Assortment changed. Reload before saving.') from None
    return doc


def read(db,owner):
    doc,_=load(db,owner)
    clause=Product.org_id.is_(None) if owner==0 else Product.org_id==owner
    products=db.execute(select(Product.id,Product.name).where(clause).order_by(Product.id).limit(1001)).all()
    return {'revision':doc['revision'],'products':[{'id':p.id,'name':p.name,
            'policy':doc['products'].get(str(p.id)), 'rules':rule(doc['products'].get(str(p.id)))} for p in products[:1000]],
            'truncated':len(products)>1000}


def edit(db,user,product_id,data):
    owner=store.tenant(user);store.lock_owner(db,owner,user);store.own_product(db,owner,product_id,lock=True)
    doc,previous=load(db,owner)
    if doc['revision']!=data.revision:raise HTTPException(409,'Assortment changed. Reload before saving.')
    if str(product_id) not in doc['products'] and len(doc['products'])>=1000:raise HTTPException(409,'Assortment policy limit reached.')
    doc['products'][str(product_id)]={'assortment':data.assortment,'kind':data.kind}
    save(db,owner,doc,previous)
    from core.dependencies import log_audit
    log_audit(db,'production.assortment_policy_saved','product',product_id,
              f'Assortment={data.assortment}; category={data.kind}; stock and jobs unchanged')
    db.commit()
    return read(db,owner)


def forget_product(db,product):
    owner=product.org_id or 0;doc,previous=load(db,owner)
    if str(product.id) in doc['products']:
        del doc['products'][str(product.id)];save(db,owner,doc,previous)


def forget_tenant(db,owner):
    db.execute(delete(SystemConfig).where(SystemConfig.key==key(owner)))


def validate_qualified_outputs(db,owner,outputs):
    doc,_=load(db,owner)
    for output in outputs:
        if output.kind=='main' and output.expected<(rule(doc['products'].get(str(output.product_id)))['minimum_main_batch'] or 1):
            raise HTTPException(409,'Multicolor character recipes need at least five main pieces per plate. Flush outputs are counted separately.')


def recipes_for(db,owner,product_id):
    values=db.execute(select(batches.recipes.c.id,batches.recipes.c.definition).where(batches.recipes.c.owner==owner).limit(1001)).all()
    if len(values)>1000:raise HTTPException(409,'Recipe scan is incomplete. Review recipes before planning more stock.')
    result=[]
    for rid,raw in values:
        spec=json.loads(raw)
        if any(o['product_id']==product_id and o['kind']=='main' for o in spec['outputs']):result.append({'id':rid,**spec})
    return result


def pending_work(db,owner,product_id,recipes):
    outstanding=db.execute(select(batches.additions.c.receipt_id).where(batches.additions.c.owner==owner,
               batches.additions.c.state.in_(('prepared','unknown'))).limit(1001)).scalars().all()
    if len(outstanding)>1000:return 'Pending-addition scan is incomplete. Review existing work first.'
    from modules.orders.finishing import receipt as finish_receipt
    for receipt_id in outstanding:
        raw=db.execute(select(batches.receipts.c.definition).where(batches.receipts.c.owner==owner,batches.receipts.c.id==receipt_id)).scalar_one_or_none()
        detail=json.loads(raw) if raw else finish_receipt(db,owner,receipt_id)
        if detail is None:return 'A pending stock addition has missing evidence. Review existing work first.'
        if any(o['product_id']==product_id and o['accepted']>0 for o in detail['outputs']):
            return 'Inspected pieces have a prepared or uncertain Square addition. Reconcile them before replenishing.'
    if not recipes:return None
    from modules.jobs.models import Job
    clause=Job.charged_to_org_id.is_(None) if owner==0 else Job.charged_to_org_id==owner
    models={r['payload']['model_id'] for r in recipes}
    rows=db.execute(select(Job,literal_column('jobs.economic_uid')).where(clause,Job.model_id.in_(models),
        Job.status.in_(('pending','submitted','scheduled','printing','paused','completed'))).order_by(Job.id.desc()).limit(201)).all()
    if len(rows)>200:return 'Matching-job scan is incomplete. Review existing work first.'
    inspected=set(db.execute(select(batches.receipts.c.job_uid).where(batches.receipts.c.owner==owner,
        batches.receipts.c.job_uid.in_([uid for _,uid in rows if uid]))).scalars())
    for job,job_uid in rows:
        if str(getattr(job.status,'value',job.status))=='completed' and job_uid in inspected:continue
        for recipe in recipes:
            payload=recipe['payload']
            if job.model_id!=payload['model_id']:continue
            # Missing preparation/assignment is uncertainty, never proof of free capacity.
            if any(getattr(job,k) is not None and getattr(job,k)!=payload[k] for k in ('print_file_id','payload_sha256','plate_index')):continue
            return f'Job {job.id} may already produce this plate or awaits inspection. Review it before suggesting another plate.'
    return None


def preview(db,user,product_id):
    owner=store.tenant(user);product=store.own_product(db,owner,product_id)
    policy,_=load(db,owner);entry=policy['products'].get(str(product_id));effective=rule(entry)
    result={'product_id':product_id,'product_name':product.name,'policy':entry,'rules':effective,
            'status':'unconfigured','message':'Choose an explicit assortment policy first.','stock':None,'recipes':[],
            'advisory_only':True,'retrieved_at':None}
    if not entry:return result
    if entry['assortment']=='inactive':return {**result,'status':'inactive','message':'Inactive for routine show replenishment. Existing stock, files and orders are retained.'}
    if entry['assortment']=='trial':return {**result,'status':'trial','message':'A deliberate trial, not automatic replenishment. Multicolor character main batches still need at least five pieces.'}
    doc,_=store.load(db,owner)
    if not doc or str(product_id) not in doc['mappings']:return {**result,'status':'blocked','message':'Connect Square and map this product before checking recorded stock.'}
    revision=doc['revision'];policy_revision=policy['revision'];mapping=copy.deepcopy(doc['mappings'][str(product_id)])
    product_name=product.name
    db.rollback()
    reader=SquareReader(doc['environment'],doc['token'])
    try:
        if reader.merchant()!=doc['merchant_id']:raise HTTPException(409,'Square account no longer matches this connection.')
        if mapping['location_id'] not in {l['id'] for l in reader.locations(doc['merchant_id'])}:raise HTTPException(409,'Square location is inactive or belongs to another account.')
        if not reader.variation(mapping['variation_id'],mapping['location_id'])['tracked']:raise HTTPException(409,'Enable Square inventory tracking before suggesting stock.')
        stock=reader.count(mapping['variation_id'],mapping['location_id'])
    except HTTPException as exc:return {**result,'status':'blocked','message':str(exc.detail)}
    # Re-read authority after remote reads; no stale preview after edits/deletion.
    store.lock_owner(db,owner,user);current=store.own_product(db,owner,product_id,lock=True)
    latest,_=store.load(db,owner);latest_policy,_=load(db,owner)
    if not latest or latest['revision']!=revision or latest_policy['revision']!=policy_revision or current.name!=product_name:
        db.rollback();raise HTTPException(409,'Product, Square connection or assortment changed during checks. Reload before continuing.')
    result.update(stock=stock,retrieved_at=batches.now())
    try:
        value=Decimal(stock['quantity']) if stock['state']=='known' and isinstance(stock['quantity'],str) else None
        if value is None or not value.is_finite() or value<0 or value!=value.to_integral_value():
            return {**result,'status':'stock_review','message':'Square quantity is unknown, negative or fractional. Review the physical baseline; no replenishment inferred.'}
    except (InvalidOperation,KeyError,TypeError):return {**result,'status':'stock_review','message':'Square quantity could not be verified. No replenishment inferred.'}
    if value>=effective['trigger_below']:return {**result,'status':'enough','message':'Recorded Square stock is at or above the trigger. Nothing suggested.'}
    recipes=recipes_for(db,owner,product_id)
    pending=pending_work(db,owner,product_id,recipes)
    if pending:return {**result,'status':'pending_review','message':pending}
    candidates=[]
    for recipe in recipes:
        main=next(o for o in recipe['outputs'] if o['product_id']==product_id and o['kind']=='main')
        if recipe['qualified'] is not True:continue
        if any(o['kind']=='main' and o['expected']<(rule(latest_policy['products'].get(str(o['product_id'])))['minimum_main_batch'] or 1) for o in recipe['outputs']):continue
        candidates.append({'id':recipe['id'],'name':recipe['name'],'main_yield':main['expected'],
            'printer_id':recipe['payload']['printer_id'],'outputs':recipe['outputs'],'materials':recipe['materials']})
    if not candidates:return {**result,'status':'recipe_needed','message':'Below the trigger, but no qualified main-output recipe meets the batch rule. Qualify a matching revision before planning a plate.'}
    return {**result,'status':'candidate','recipes':candidates,'message':'Below the trigger. These are fixed-yield plate options, not an exact-deficit order. Check paid commitments, materials and fresh printer readiness before any start.'}
