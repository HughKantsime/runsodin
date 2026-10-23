"""On-demand show preparation, entered capacity and saved-file evidence.

No scheduler, stock totals, calendar assumptions or physical actions.
"""
import hashlib,json,uuid
from datetime import date
from typing import Literal
from pydantic import BaseModel,ConfigDict,Field,StrictInt,StrictBool,model_validator
from fastapi import HTTPException
from sqlalchemy import select,update,cast,String,delete,text
from sqlalchemy.exc import IntegrityError
from core.models import SystemConfig
from modules.orders import square_store as store,batch_production as batches

PREFIX='production_operations:'
class Input(BaseModel):model_config=ConfigDict(extra='forbid',str_strip_whitespace=True)
class KitEdit(Input):
    revision:str|None=Field(default=None,max_length=64)
    item_id:str|None=Field(default=None,pattern=r'^[a-f0-9]{32}$')
    name:str=Field(min_length=1,max_length=120)
    available:StrictInt=Field(ge=0,le=1000)
    condition:Literal['ready','damaged','missing']
    issue:str=Field(default='',max_length=300)
    resolution:str=Field(default='',max_length=300)
    active:StrictBool=True
class Pick(Input):
    item_id:str=Field(pattern=r'^[a-f0-9]{32}$')
    quantity:StrictInt=Field(gt=0,le=1000)
class EventEdit(Input):
    revision:str|None=Field(default=None,max_length=64)
    event_id:str|None=Field(default=None,pattern=r'^[a-f0-9]{32}$')
    title:str=Field(min_length=1,max_length=120)
    starts_on:date
    status:Literal['planned','booked','completed','cancelled']
    picks:list[Pick]=Field(max_length=100)
class PackEdit(Input):
    revision:str=Field(min_length=1,max_length=64)
    confirmed:StrictBool
class WorkEstimate(Input):
    order_id:StrictInt=Field(gt=0)
    minutes:StrictInt|None=Field(default=None,ge=0,le=100000)
    promised_by:date|None=None
class CapacityEdit(Input):
    revision:str|None=Field(default=None,max_length=64)
    starts_on:date
    ends_on:date
    available_minutes:StrictInt=Field(ge=0,le=100000)
    estimates:list[WorkEstimate]=Field(max_length=100)
    @model_validator(mode='after')
    def window(self):
        days=(self.ends_on-self.starts_on).days+1
        if not 1<=days<=31 or self.available_minutes>days*1440:raise ValueError('Choose a bounded real capacity window')
        if len({v.order_id for v in self.estimates})!=len(self.estimates):raise ValueError('Use each order once')
        return self

def key(owner):return PREFIX+str(owner)
def digest(value):return hashlib.sha256(batches.packed(value).encode()).hexdigest()
def load(db,owner):
    raw=db.execute(select(SystemConfig.value).where(SystemConfig.key==key(owner))).scalar_one_or_none()
    if raw is None:return {'version':1,'owner':owner,'revision':None,'kit':{},'events':{},'capacity':None},None
    try:
        if not isinstance(raw,str) or len(raw)>500000:raise ValueError()
        doc=json.loads(raw)
        if doc['version']!=1 or doc['owner']!=owner or not isinstance(doc['kit'],dict) or not isinstance(doc['events'],dict) or len(doc['kit'])>100 or len(doc['events'])>100:raise ValueError()
        return doc,raw
    except (ValueError,KeyError,TypeError):raise HTTPException(503,'Operations planning state needs administrator review') from None

def editing(db,user,revision):
    owner=store.tenant(user);store.lock_owner(db,owner,user);doc,raw=load(db,owner)
    if doc['revision']!=revision:raise HTTPException(409,'Operations state changed. Reload before saving.')
    return owner,doc,raw

def save(db,owner,doc,raw):
    doc['revision']=uuid.uuid4().hex
    try:
        if raw is None:db.add(SystemConfig(key=key(owner),value=batches.packed(doc)));db.flush()
        elif db.execute(update(SystemConfig).where(SystemConfig.key==key(owner),cast(SystemConfig.value,String)==json.dumps(raw)).values(value=batches.packed(doc)).execution_options(synchronize_session=False)).rowcount!=1:raise HTTPException(409,'Operations state changed. Reload before saving.')
    except IntegrityError:db.rollback();raise HTTPException(409,'Operations state changed. Reload before saving.') from None
    db.commit()

def pick_hash(item,quantity):return digest({'item':item,'quantity':quantity})
def event_public(doc,row):
    picks=[]
    for pick in row['picks']:
        item=doc['kit'].get(pick['item_id']);ready=bool(item and item['active'] and item['condition']=='ready' and item['available']>=pick['quantity'])
        packed=ready and row.get('packed',{}).get(pick['item_id'])==pick_hash(item,pick['quantity'])
        picks.append({**pick,'name':item['name'] if item else 'Missing kit item','ready':ready,'packed':bool(packed),'issue':item.get('issue','') if item else 'Item missing'})
    return {**row,'picks':picks,'ready_to_leave':False,'all_selected_packed':bool(picks) and all(p['packed'] for p in picks),'message':'Kit checks only. Stock, transport fit, off-calendar responsibilities and booking details require separate review.'}

def read(db,user):
    doc,_=load(db,store.tenant(user));return {'revision':doc['revision'],'kit':[dict(r,id=rid) for rid,r in doc['kit'].items()],
        'events':[event_public(doc,r) for r in doc['events'].values()],'capacity':doc['capacity'],'automatic_actions':False}

def edit_kit(db,user,data):
    owner,doc,raw=editing(db,user,data.revision);rid=data.item_id or uuid.uuid4().hex;old=doc['kit'].get(rid)
    if data.item_id and old is None:raise HTTPException(404,'Kit item not found')
    if old is None and len(doc['kit'])>=100:raise HTTPException(409,'Retain or archive existing kit items; kit capacity reached.')
    if data.condition!='ready' and not data.issue:raise HTTPException(409,'Keep a reason for damaged or missing kit items')
    if old and old['condition']!='ready' and (data.condition=='ready' or not data.active) and not data.resolution:raise HTTPException(409,'Record how the unresolved kit issue was repaired, replaced or deliberately retired')
    doc['kit'][rid]={k:v for k,v in data.model_dump(exclude={'revision','item_id'}).items()};save(db,owner,doc,raw);return read(db,user)

def edit_event(db,user,data):
    owner,doc,raw=editing(db,user,data.revision);rid=data.event_id or uuid.uuid4().hex
    old=doc['events'].get(rid)
    if data.event_id and old is None:raise HTTPException(404,'Show preparation record not found')
    if old is None and len(doc['events'])>=100:raise HTTPException(409,'Show preparation capacity reached. Retain past records for review.')
    if len({p.item_id for p in data.picks})!=len(data.picks) or any(p.item_id not in doc['kit'] for p in data.picks):raise HTTPException(409,'Choose each known kit item once')
    row={'id':rid,**data.model_dump(mode='json',exclude={'revision','event_id'}),'packed':{}}
    # Date/status changes invalidate previous packing, requiring a fresh physical check.
    doc['events'][rid]=row;save(db,owner,doc,raw);return read(db,user)

def pack(db,user,event_id,item_id,data):
    owner,doc,raw=editing(db,user,data.revision);event=doc['events'].get(event_id);item=doc['kit'].get(item_id)
    if not event or not item:raise HTTPException(404,'Show preparation item not found')
    pick=next((p for p in event['picks'] if p['item_id']==item_id),None)
    if not pick or event['status'] not in ('planned','booked'):raise HTTPException(409,'Choose an active show preparation item')
    if data.confirmed:
        if item['condition']!='ready' or not item['active'] or item['available']<pick['quantity']:raise HTTPException(409,'Resolve kit damage or missing quantity before confirming packing')
        event['packed'][item_id]=pick_hash(item,pick['quantity'])
    else:event['packed'].pop(item_id,None)
    save(db,owner,doc,raw);return read(db,user)

def work_binding(db,order):
    uid=db.execute(text('SELECT economic_uid FROM orders WHERE id=:id'),{'id':order.id}).scalar_one_or_none()
    if not uid:raise HTTPException(409,'Order identity unavailable; retain for review')
    return digest({'uid':uid,'status':str(order.status),'items':sorted([(i.id,i.product_id,i.quantity,i.fulfilled_quantity) for i in order.items])})

def set_capacity(db,user,data):
    from modules.orders.show_fulfillment import owned_order
    owner,doc,raw=editing(db,user,data.revision);estimates=[]
    for entered in sorted(data.estimates,key=lambda e:e.order_id):
        order=owned_order(db,user,entered.order_id,lock=True)
        estimates.append({**entered.model_dump(mode='json'),'binding':work_binding(db,order)})
    doc['capacity']={**data.model_dump(mode='json',exclude={'revision','estimates'}),'estimates':estimates,'recorded_on':date.today().isoformat()}
    save(db,owner,doc,raw);return conflicts(db,user)

def conflicts(db,user):
    from modules.orders.models import Order
    from modules.orders import show_fulfillment as show
    from sqlalchemy.orm import selectinload
    owner=store.tenant(user);doc,_=load(db,owner);capacity=doc.get('capacity');clause=Order.org_id.is_(None) if owner==0 else Order.org_id==owner
    orders=db.execute(select(Order).options(selectinload(Order.items)).where(clause,Order.status.notin_(('shipped','cancelled'))).order_by(Order.id).limit(101)).scalars().all()
    estimates={e['order_id']:e for e in capacity['estimates']} if capacity else {};rows=[];total=0;unknown=not capacity or len(orders)>100;reasons=[]
    for order in orders[:100]:
        receipt,_=show.load(db,order);facts=show.summary(db,order,receipt);estimate=estimates.get(order.id);minutes=None;promise=None
        if estimate:
            try:current=estimate['binding']==work_binding(db,order)
            except HTTPException:current=False
            if current:minutes=estimate['minutes']
            promise=estimate.get('promised_by')
        paid=facts['state']=='paid_committed'
        if paid:promise=receipt['details']['dispatch_by']
        in_window=bool(capacity and promise and capacity['starts_on']<=promise<=capacity['ends_on'])
        needs=not capacity or not promise or minutes is None or (capacity and capacity['recorded_on']!=date.today().isoformat())
        if needs:unknown=True
        if in_window and minutes is not None:total+=minutes
        if capacity and promise and promise<capacity['starts_on']:reasons.append(f'Order {order.id} is promised before the entered capacity window.')
        elif in_window and promise<capacity['ends_on']:reasons.append(f'Order {order.id} is due before the end of the capacity window; review when its work can happen.')
        rows.append({'order_id':order.id,'order_number':order.order_number,'platform':order.platform,'paid_show':paid,'show_state':facts['state'],'promised_by':promise,'remaining_pieces':facts['production_remaining'],'entered_minutes':minutes,'needs_review':bool(needs),'in_window':in_window})
    over=bool(capacity and total>capacity['available_minutes'])
    return {'revision':doc['revision'],'capacity':capacity,'orders':rows,'truncated':len(orders)>100,'known_minutes_in_window':total,
            'state':'conflict' if over or reasons else 'unknown' if unknown else 'within_entered_total', 'reasons':reasons,
            'capacity_verified':False,'queue_changed':False,'message':'Entered estimates only, not a feasible schedule. Paid orders and wholesale promises need an explicit decision; jobs are unchanged.'}

def jobs(db,user):
    owner=store.tenant(user)
    rows=db.execute(text('SELECT j.id,j.item_name,j.status FROM jobs j WHERE ((:owner=0 AND j.charged_to_org_id IS NULL) OR j.charged_to_org_id=:owner) AND NOT EXISTS (SELECT 1 FROM education_submissions e WHERE e.job_id=j.id) ORDER BY j.id DESC LIMIT 101'),{'owner':owner}).mappings().all()
    return {'jobs':[dict(r) for r in rows[:100]],'truncated':len(rows)>100}

def readiness(db,user,job_id):
    owner=store.tenant(user)
    row=db.execute(text('SELECT j.id,j.model_id,j.print_file_id,j.payload_sha256,j.plate_index,j.printer_id,j.status FROM jobs j WHERE j.id=:id AND ((:owner=0 AND j.charged_to_org_id IS NULL) OR j.charged_to_org_id=:owner) AND NOT EXISTS (SELECT 1 FROM education_submissions e WHERE e.job_id=j.id)'),{'id':job_id,'owner':owner}).mappings().one_or_none()
    if row is None:raise HTTPException(404,'Job not found')
    from core.interfaces.education_policy import assert_generic_model_reference_allowed
    assert_generic_model_reference_allowed(db,row['model_id'])
    reasons=[]
    for field in ('model_id','print_file_id','payload_sha256','plate_index','printer_id'):
        if row[field] is None:reasons.append(f'Missing saved {field.replace("_"," ")}.')
    payload={k:row[k] for k in ('model_id','print_file_id','payload_sha256','plate_index','printer_id')}
    from modules.orders.finishing import model_snapshot
    try:model_snapshot(db,owner,row['model_id'])
    except HTTPException:reasons.append('Source model is missing or belongs to another organization.')
    printer=db.execute(text('SELECT org_id FROM printers WHERE id=:id'),{'id':row['printer_id']}).first()
    if not printer or (printer.org_id or 0)!=owner:reasons.append('Saved printer is missing or belongs to another organization.')
    saved_file=db.execute(text('SELECT model_id,file_hash FROM print_files WHERE id=:id'),{'id':row['print_file_id']}).first()
    if not saved_file or saved_file.model_id!=row['model_id']:reasons.append('Saved print-file record is missing or is not bound to this source model.')
    import re
    if not isinstance(row['payload_sha256'],str) or not re.fullmatch(r'[a-f0-9]{64}',row['payload_sha256']):reasons.append('Saved payload hash is missing or invalid.')
    if type(row['plate_index']) is not int or row['plate_index']<0:reasons.append('Saved plate index is invalid.')
    preparation_complete=not reasons
    matches=[]
    specs=db.execute(select(batches.recipes.c.id,batches.recipes.c.definition).where(batches.recipes.c.owner==owner).limit(1001)).all()
    if len(specs)>1000:reasons.append('Recipe scan is incomplete; do not infer readiness.')
    for rid,raw in specs[:1000]:
        spec=json.loads(raw)
        if spec['qualified'] and all(spec['payload'].get(k)==v for k,v in payload.items()):matches.append({'id':rid,'name':spec['name'],'materials':spec['materials']})
    physical=False
    if row['status']=='completed':
        try:batches.job_evidence(db,owner,job_id);physical=True
        except HTTPException:reasons.append('Retained physical completion evidence is incomplete or no longer matches.')
    else:reasons.append('This job has not passed retained completed-print evidence checks.')
    if not matches:reasons.append('No matching qualified recipe for the saved payload, plate and printer.')
    return {'job_id':job_id,'status':row['status'],'saved_preparation_complete':preparation_complete and len(specs)<=1000,
            'retained_physical_evidence':physical,'matching_recipes':matches,'reasons':reasons,
            'dispatch_ready':False,'file_bytes_checked':False,'message':'Saved preparation and retained evidence only. File existence/bytes, current printer setup, filament and bed clearance are not certified. Names alone do not qualify a file.'}

def forget(db,owner):db.execute(delete(SystemConfig).where(SystemConfig.key==key(owner)))
