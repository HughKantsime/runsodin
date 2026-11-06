"""Retained component inspections and explicit assembly, never finished-stock totals.

The bounded owner document serializes component use, including global owner0.
Immutable receipts are not removed when consumed; full history fails closed.
"""
import copy,hashlib,json,uuid
from collections import defaultdict
from typing import Literal
from fastapi import HTTPException
from pydantic import BaseModel,ConfigDict,Field,StrictInt,StrictBool
from sqlalchemy import select,update,cast,String,text
from sqlalchemy.exc import IntegrityError
from core.models import SystemConfig
from modules.orders import square_store as store,batch_production as batches

PREFIX='production_finishing:'
class Input(BaseModel):
    model_config=ConfigDict(extra='forbid',str_strip_whitespace=True)
class ComponentInput(Input):
    job_id:StrictInt=Field(gt=0)
    accepted:StrictInt=Field(ge=0,le=10000)
    rejected:StrictInt=Field(ge=0,le=10000)
    unit_description:str=Field(min_length=1,max_length=200)
    inspection_confirmed:StrictBool
    single_component_type_confirmed:StrictBool
class Allocation(Input):
    receipt_id:str=Field(pattern=r'^[a-f0-9]{32}$')
    quantity:StrictInt=Field(gt=0,le=10000)
from modules.orders.material_schemas import HardwareUse

class FinishInput(Input):
    request_id:str=Field(pattern=r'^[a-f0-9]{32}$')
    product_id:StrictInt=Field(gt=0)
    bom_hash:str=Field(pattern=r'^[a-f0-9]{64}$')
    accepted:StrictInt=Field(ge=0,le=10000)
    rejected:StrictInt=Field(ge=0,le=10000)
    allocations:list[Allocation]=Field(min_length=1,max_length=100)
    assembly_confirmed:StrictBool
    hardware_confirmed:StrictBool
    square_revision:str|None=Field(default=None,max_length=64)
    hardware_usage:list[HardwareUse]|None=Field(default=None,max_length=100)
    hardware_used_confirmed:StrictBool=False

def key(owner):return PREFIX+str(owner)
def digest(value):return hashlib.sha256(batches.packed(value).encode()).hexdigest()
def empty(owner):return {'version':1,'owner':owner,'revision':None,'components':{},'finishes':{}}
def load(db,owner):
    raw=db.execute(select(SystemConfig.value).where(SystemConfig.key==key(owner))).scalar_one_or_none()
    if raw is None:return empty(owner),None
    try:
        if not isinstance(raw,str) or len(raw)>2000000:raise ValueError()
        doc=json.loads(raw)
        if doc['version']!=1 or doc['owner']!=owner or not isinstance(doc['components'],dict) or not isinstance(doc['finishes'],dict) or max(len(doc['components']),len(doc['finishes']))>500:raise ValueError()
        return doc,raw
    except (ValueError,KeyError,TypeError):raise HTTPException(503,'Finishing evidence needs administrator review. Retain these records.') from None

def lock(db,owner,user=None):
    store.lock_owner(db,owner,user)
    doc,raw=load(db,owner)
    if raw is None:
        try:db.add(SystemConfig(key=key(owner),value=batches.packed(doc)));db.flush()
        except IntegrityError:db.rollback();raise HTTPException(409,'Finishing state changed. Reload before continuing.') from None
    else:
        db.execute(update(SystemConfig).where(SystemConfig.key==key(owner)).values(value=SystemConfig.value).execution_options(synchronize_session=False))
    return load(db,owner)

def save(db,owner,doc,raw):
    doc['revision']=uuid.uuid4().hex;encoded=batches.packed(doc)
    if len(encoded)>2000000:raise HTTPException(409,'Finishing evidence capacity reached. Retain history for administrator review.')
    if db.execute(update(SystemConfig).where(SystemConfig.key==key(owner),cast(SystemConfig.value,String)==json.dumps(raw)).values(value=encoded).execution_options(synchronize_session=False)).rowcount!=1:
        raise HTTPException(409,'Finishing state changed. Reload before continuing.')

def component_for_job(doc,uid):return next((r for r in doc['components'].values() if r['job_uid']==uid),None)
def model_snapshot(db,owner,model_id,*,locked=False):
    from modules.models_library.models import Model
    clause=Model.org_id.is_(None) if owner==0 else Model.org_id==owner
    if locked and db.execute(update(Model).where(Model.id==model_id,clause).values(id=Model.id)).rowcount!=1:raise HTTPException(404,'Component model not found')
    row=db.execute(select(Model).where(Model.id==model_id,clause).execution_options(populate_existing=True)).scalar_one_or_none()
    if row is None:raise HTTPException(409,'Component source model is no longer available')
    return {'model_id':row.id,'name':row.name,'created_at':str(row.created_at),'print_file_id':row.print_file_id,'units_per_bed':row.units_per_bed,'quantity_per_bed':row.quantity_per_bed}

def inspect(db,user,data):
    owner=store.tenant(user);doc,raw=lock(db,owner,user)
    if not data.inspection_confirmed or not data.single_component_type_confirmed or data.accepted+data.rejected<1:raise HTTPException(409,'Inspect and count one component type. Mixed-part plates need distinct source models before acceptance.')
    evidence=batches.job_evidence(db,owner,data.job_id,lock=True);request_hash=digest(data.model_dump())
    old=component_for_job(doc,evidence['economic_uid'])
    if old:
        if old['request_hash']!=request_hash:raise HTTPException(409,'This component batch already has a different retained inspection')
        return old
    if db.execute(select(batches.receipts.c.id).where(batches.receipts.c.job_uid==evidence['economic_uid'])).first():raise HTTPException(409,'This job already has finished-product acceptance. Do not count it again as components.')
    if len(doc['components'])>=500:raise HTTPException(409,'Component receipt capacity reached; retain history for review.')
    identifier=uuid.uuid5(uuid.NAMESPACE_URL,'odin:component:'+evidence['economic_uid']).hex
    row={'id':identifier,'job_id':data.job_id,'job_uid':evidence['economic_uid'],'claim_token':evidence['claim_token'],
         'source':model_snapshot(db,owner,evidence['model_id'],locked=True),'payload':batches.recipe_payload(evidence),
         'physical_uid':evidence['observation_uid'],'accepted':data.accepted,'rejected':data.rejected,'unit_description':data.unit_description,
         'request_hash':request_hash,'occurred_at':batches.now(),'stock_status':'components_only'}
    doc['components'][identifier]=row;save(db,owner,doc,raw);db.commit();return row

def requirements(db,owner,product_id,*,locked=False):
    product=store.own_product(db,owner,product_id,lock=locked)
    from modules.orders.models import ProductComponent
    parts=db.execute(select(ProductComponent).where(ProductComponent.product_id==product_id).order_by(ProductComponent.id)).scalars().all()
    if not parts or len(parts)>100:raise HTTPException(409,'Define a bounded printed-component BOM before finishing.')
    needs=defaultdict(int);sources={}
    for part in parts:
        if type(part.quantity_needed) is not int or not 0<part.quantity_needed<=10000:raise HTTPException(409,'BOM component counts must be positive whole units')
        needs[part.model_id]+=part.quantity_needed;sources[str(part.model_id)]=model_snapshot(db,owner,part.model_id,locked=locked)
    hardware=[dict(r) for r in db.execute(text('SELECT consumable_id,quantity_per_product FROM product_consumables WHERE product_id=:id ORDER BY id'),{'id':product_id}).mappings()]
    from modules.inventory.models import Consumable
    if locked:
        for cid in sorted({h['consumable_id'] for h in hardware}):
            if db.execute(update(Consumable).where(Consumable.id==cid).values(id=Consumable.id)).rowcount!=1:raise HTTPException(409,'Required hardware is missing')
    for h in hardware:
        c=db.execute(select(Consumable).where(Consumable.id==h['consumable_id']).execution_options(populate_existing=True)).scalar_one_or_none()
        if c is None:raise HTTPException(409,'Required hardware is missing')
        from modules.orders import material_store
        identity=material_store.guard(db,c,owner)
        h.update(name=c.name,unit=c.unit,managed=bool(identity),material_uid=identity['uid'] if identity else None)
        from decimal import Decimal,InvalidOperation
        try:
            quantity=Decimal(str(h['quantity_per_product']))
            if not quantity.is_finite() or quantity<=0:raise ValueError()
        except (ValueError,InvalidOperation):raise HTTPException(409,'Review the required hardware quantities') from None
    snapshot={'product_id':product.id,'product_name':product.name,'components':dict(needs),'sources':sources,'hardware':hardware}
    return snapshot,digest(snapshot)

def available(doc):
    used=defaultdict(int)
    for row in doc['finishes'].values():
        for a in row['allocations']:used[a['receipt_id']]+=a['quantity']
    result={rid:row['accepted']-used[rid] for rid,row in doc['components'].items()}
    if any(q<0 for q in result.values()):raise HTTPException(503,'Component allocations need administrator review')
    return result

def preview(db,user,product_id):
    owner=store.tenant(user);snapshot,bom_hash=requirements(db,owner,product_id);doc,_=load(db,owner);balances=available(doc)
    from modules.orders.material_store import own_material
    hardware_stock={str(h['consumable_id']):own_material(db,owner,h['consumable_id'])[0].current_stock for h in snapshot['hardware'] if h.get('managed')}
    return {'hardware_stock':hardware_stock,'bom':snapshot,'bom_hash':bom_hash,'components':[dict(row,available=balances[rid],source_current=(row['source']==snapshot['sources'].get(str(row['source']['model_id'])))) for rid,row in doc['components'].items() if str(row['source']['model_id']) in snapshot['sources']], 'stock_updated':False}

def finish(db,user,data):
    owner=store.tenant(user);doc,raw=lock(db,owner,user);request_hash=digest(data.model_dump())
    old=doc['finishes'].get(data.request_id)
    if old:
        if old['request_hash']!=request_hash:raise HTTPException(409,'This finishing request already has different retained facts')
        return old
    if len(doc['finishes'])>=500:raise HTTPException(409,'Finishing receipt capacity reached; retain history for review.')
    if not data.assembly_confirmed or not data.hardware_confirmed or data.accepted+data.rejected<1:raise HTTPException(409,'Confirm physical assembly, fit/quality inspection and required hardware before finishing.')
    from modules.orders import material_store
    material_store.reserve(db,owner,user)
    snapshot,bom_hash=requirements(db,owner,data.product_id,locked=True)
    if bom_hash!=data.bom_hash:raise HTTPException(409,'BOM or source changed. Reload requirements and review the physical components.')
    balances=available(doc);supplied=defaultdict(int);ids=set()
    for a in data.allocations:
        row=doc['components'].get(a.receipt_id)
        if a.receipt_id in ids or row is None or a.quantity>balances.get(a.receipt_id,0):raise HTTPException(409,'Select distinct available inspected component quantities')
        ids.add(a.receipt_id);mid=row['source']['model_id']
        if row['source']!=snapshot['sources'].get(str(mid)):raise HTTPException(409,'An inspected component source changed or does not match this BOM')
        supplied[mid]+=a.quantity
    expected={int(mid):qty*(data.accepted+data.rejected) for mid,qty in snapshot['components'].items()}
    if dict(supplied)!=expected:raise HTTPException(409,'Consumed components must exactly match the BOM for accepted plus rejected assemblies')
    timestamp=batches.now();identifier=uuid.uuid5(uuid.NAMESPACE_URL,f'odin:finish:{owner}:{data.request_id}').hex
    output={'product_id':data.product_id,'product_name':snapshot['product_name'],'kind':'main','expected':data.accepted+data.rejected,'accepted':data.accepted,'rejected':data.rejected}
    row={'id':identifier,'kind':'assembly','occurred_at':timestamp,'request_hash':request_hash,'bom':snapshot,'bom_hash':bom_hash,
         'allocations':[a.model_dump() for a in data.allocations],'outputs':[output], 'recipe_name':'Physical finishing: '+snapshot['product_name'],
         'assembly_confirmed':True,'hardware_confirmed':True,'stock_status':'prepared' if data.accepted else 'no_sellable_output'}
    from modules.orders.material_purchases import consume_hardware,whole
    uses=[]
    for h in snapshot['hardware']:
        expected=h['quantity_per_product']*(data.accepted+data.rejected)
        if h['managed']:expected=whole(expected)
        uses.append({**h,'expected_units':expected})
    row['material_use']=consume_hardware(db,owner,uses,data.hardware_usage,data.hardware_used_confirmed,identifier)
    if data.accepted:
        config,encoded=store.load(db,owner)
        if not config:raise HTTPException(409,'Connect and map the finished product to Square before accepting good assemblies')
        store.check_revision(config,data.square_revision)
        if db.execute(update(SystemConfig).where(SystemConfig.key==store.key(owner),cast(SystemConfig.value,String)==json.dumps(encoded)).values(value=SystemConfig.value)).rowcount!=1:raise HTTPException(409,'Square connection changed. Reload before finishing.')
        mapping=config['mappings'].get(str(data.product_id))
        if not mapping:raise HTTPException(409,'Map the finished product to Square before accepting good assemblies')
        dest={'merchant_id':config['merchant_id'],'environment':config['environment'],'connection_revision':config['revision']}
        change={'type':'ADJUSTMENT','adjustment':{'catalog_object_id':mapping['variation_id'],'from_location_id':mapping['location_id'],'to_location_id':mapping['location_id'],'from_state':'NONE','to_state':'IN_STOCK','quantity':str(data.accepted),'occurred_at':timestamp,'reference_id':identifier}}
        db.execute(batches.additions.insert().values(id=uuid.uuid4().hex,owner=owner,receipt_id=identifier,destination=batches.packed(dest),request_body=batches.packed({'idempotency_key':identifier,'changes':[change]}),state='prepared',attempts=0))
    doc['finishes'][data.request_id]=row;save(db,owner,doc,raw);db.commit();return row

def receipt(db,owner,receipt_id):
    doc,_=load(db,owner);return next((r for r in doc['finishes'].values() if r['id']==receipt_id),None)
def overview(db,user):
    owner=store.tenant(user);doc,_=load(db,owner);balances=available(doc)
    return {'revision':doc['revision'],'components':[dict(r,available=balances[rid]) for rid,r in doc['components'].items()],
            'finishes':list(doc['finishes'].values()),'square_writes_enabled':False}
def guard_product(db,product):
    doc,_=load(db,product.org_id or 0)
    if any(r['bom']['product_id']==product.id for r in doc['finishes'].values()):raise HTTPException(409,'Keep products with retained assembly receipts')
def guard_tenant(db,owner):
    doc,_=load(db,owner)
    if doc['components'] or doc['finishes']:raise HTTPException(409,'Keep organizations with retained component or finishing evidence')
def guard_model_delete(db,owner,model_id):
    doc,_=lock(db,owner)
    if any(r['source']['model_id']==model_id for r in doc['components'].values()):raise HTTPException(409,'Keep this model to preserve inspected component identity')
