"""Incoming is not on hand. Explicit physical facts mutate the existing stock."""
import csv,io,json,uuid,re,math
from collections import defaultdict
from datetime import datetime,timezone
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select,update,text
from modules.orders import material_store as store,square_store as square,batch_production as batches
from modules.orders.material_schemas import PurchaseLine,HardwareUse
from modules.inventory.models import Consumable,ConsumableUsage
from core.models import SystemConfig

def fail(message):raise HTTPException(409,message)
def whole(value):
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not 0<=value<=100000000 or value!=int(value):fail('Material stock must be a known nonnegative whole-piece count. Review its baseline.')
    return int(value)
def audit(db,cid,delta,event_id):
    if delta:db.add(ConsumableUsage(consumable_id=cid,quantity_used=-delta,notes='Managed material event '+event_id))
def change_stock(db,item,delta,event_id):
    before=whole(item.current_stock);after=before+delta
    if not 0<=after<=100000000:fail('Insufficient available material units or quantity limit exceeded.')
    if db.execute(update(Consumable).where(Consumable.id==item.id,Consumable.current_stock==before).values(current_stock=after,updated_at=datetime.now(timezone.utc)).execution_options(synchronize_session=False)).rowcount!=1:fail('Material stock changed. Reload before recording physical use.')
    item.current_stock=after;audit(db,item.id,delta,event_id)

def register(db,user,data,*,adopt=False):
    owner=square.tenant(user);doc,raw=store.lock(db,owner,user)
    if len(doc['materials'])>=200:fail('Managed material capacity reached. Retain records for review.')
    baseline=0
    if adopt:
        if not data.physically_counted:fail('Confirm a physical baseline count before adopting legacy material.')
        if db.execute(update(Consumable).where(Consumable.id==data.consumable_id).values(id=Consumable.id)).rowcount!=1:raise HTTPException(404,'Consumable not found')
        item=db.execute(select(Consumable).where(Consumable.id==data.consumable_id).execution_options(populate_existing=True)).scalar_one()
        if store.identity(db,item.id) is not None:fail('This material already has retained custody. Do not adopt it again.')
        if item.unit!='piece':fail('Only counted hardware pieces can enter this workflow.')
        # Old global history has no trustworthy owner for arbitrary notes.
        if db.execute(select(ConsumableUsage.id).where(ConsumableUsage.consumable_id==item.id).limit(1)).first():fail('Legacy usage history is unscoped. Retain it for a separately reviewed migration; do not reassign its private history.')
        owners=db.execute(text('SELECT p.org_id FROM product_consumables pc JOIN products p ON p.id=pc.product_id WHERE pc.consumable_id=:id'),{'id':item.id}).scalars().all()
        if any((o or 0)!=owner for o in owners):fail('Legacy material is linked to another organization; retain its shared history.')
        before=whole(item.current_stock);baseline=data.counted_units
        change_stock(db,item,baseline-before,uuid.uuid4().hex)
    else:
        item=Consumable(name=data.name,unit='piece',current_stock=0,cost_per_unit=None,status='active')
        db.add(item);db.flush();db.refresh(item)
    uid=uuid.uuid4().hex;identity={'version':1,'owner':owner,'uid':uid,'snapshot':store.snapshot(item)}
    db.add(SystemConfig(key=store.IDENTITY+str(item.id),value=batches.packed(identity)))
    doc['materials'][str(item.id)]={'uid':uid,'name':item.name,'unit':'piece','specification':data.specification,'baseline_units':baseline,'recorded_at':batches.now()}
    store.save(db,owner,doc,raw);db.flush();answer=read(db,user);db.commit();return answer

def mapping_signature(value):
    return {k:(value[k].casefold() if k=='vendor' else value[k]) for k in ('vendor','listing_id','variant','specification','units_per_pack')}
def add_mapping(db,user,data):
    if not data.verified:fail('Verify the exact listing, variant, specification and units per pack before mapping.')
    owner=square.tenant(user);doc,raw=store.lock(db,owner,user);item,identity=store.own_material(db,owner,data.consumable_id,locked=True)
    material=doc['materials'].get(str(item.id))
    if not material or material['uid']!=identity['uid'] or material['specification']!=data.specification:fail('Mapping specification must match the managed material. Changed parts need their own material identity.')
    sig=mapping_signature(data.model_dump())
    for m in doc['mappings'].values():
        if m['signature']==sig:
            if m['consumable_id']!=item.id or m['material_uid']!=identity['uid']:fail('This exact listing conversion already maps to another material; retain it for review.')
            return read(db,user)
    if len(doc['mappings'])>=200:fail('Mapping revision capacity reached. Retain history for review.')
    mid=uuid.uuid4().hex;doc['mappings'][mid]={'id':mid,'signature':sig,'consumable_id':item.id,'material_uid':identity['uid'],'verified_at':batches.now()}
    store.save(db,owner,doc,raw);db.flush();answer=read(db,user);db.commit();return answer

def line_key(owner,line):return uuid.uuid5(uuid.NAMESPACE_URL,'odin:purchase:'+str(owner)+':'+store.digest([line.vendor.casefold(),line.order_reference,line.line_reference])).hex
def canonical(line):
    value=line.model_dump(mode='json');value['vendor']=value['vendor'].casefold();return value

def resolve(doc,line):
    sig=mapping_signature(line.model_dump());matches=[m for m in doc['mappings'].values() if m['signature']==sig]
    if len(matches)!=1:fail('No unique verified listing/variant/specification/pack conversion. Review or approve the mapping first.')
    units=line.packs*line.units_per_pack
    if units>100000000:fail('Purchase unit quantity exceeds the supported limit.')
    return matches[0],units

def totals(doc,lid):
    counts=defaultdict(int)
    for e in doc['events'].values():
        if e['line_id']==lid:counts[e['kind']]+=e['units']
    ordered=doc['lines'][lid]['ordered_units']
    incoming=ordered-counts['receive_good']-counts['receive_damaged']-counts['cancel']
    if incoming<0:raise HTTPException(503,'Purchase receipt totals need administrator review.')
    return {'ordered_units':ordered,'incoming_units':incoming,**{k:counts[k] for k in ('receive_good','receive_damaged','cancel','return_good','return_damaged')}}

def read(db,user):
    owner=square.tenant(user);doc,_=store.load(db,owner);materials=[]
    for cid,metadata in doc['materials'].items():
        item,identity=store.own_material(db,owner,int(cid))
        if identity['uid']!=metadata['uid']:fail('Managed material lifetime changed. Retain the original evidence.')
        materials.append({'consumable_id':item.id,**metadata,'current_stock':whole(item.current_stock)})
    ids=[m['consumable_id'] for m in materials]
    usage=[{'id':u.id,'consumable_id':u.consumable_id,'quantity_used':u.quantity_used,'used_at':u.used_at.isoformat() if u.used_at else None,'reference':u.notes} for u in db.execute(select(ConsumableUsage).where(ConsumableUsage.consumable_id.in_(ids)).order_by(ConsumableUsage.id.desc()).limit(100)).scalars()] if ids else []
    return {'usage':usage,'revision':doc['revision'],'can_register':user.get('role')=='admin','materials':materials,'mappings':list(doc['mappings'].values()),
            'lines':[dict(line,totals=totals(doc,lid)) for lid,line in doc['lines'].items()],
            'events':list(doc['events'].values()),'live_import_enabled':False,'message':'Incoming purchases are not available stock. Only confirmed physical good receipts add units. No Amazon/email integration or bookkeeping posting.'}

def purchases(db,user,data):
    owner=square.tenant(user);doc,raw=store.lock(db,owner,user);new=[]
    for line in data.lines:
        lid=line_key(owner,line);facts=canonical(line);fingerprint=store.digest(facts);old=doc['lines'].get(lid)
        if old:
            if old['request_hash']!=fingerprint:fail('This vendor/order/line already has different retained purchase facts. Do not recreate or silently change its quantity.')
            continue
        mapping,units=resolve(doc,line)
        if len(doc['lines'])>=500:fail('Purchase line capacity reached. Retain history for review.')
        row={'id':lid,'facts':facts,'mapping_id':mapping['id'],'consumable_id':mapping['consumable_id'],'material_uid':mapping['material_uid'],'ordered_units':units,'request_hash':fingerprint,'recorded_at':batches.now()}
        doc['lines'][lid]=row;new.append(row)
    for cid in sorted({r['consumable_id'] for r in new}):
        _,identity=store.own_material(db,owner,cid,locked=True)
        if any(r['material_uid']!=identity['uid'] for r in new if r['consumable_id']==cid):fail('Purchase mapping material identity changed.')
    store.save(db,owner,doc,raw);db.flush();answer=read(db,user);db.commit();return answer

def preview_csv(db,user,data):
    owner=square.tenant(user);doc,_=store.load(db,owner)
    required={'vendor','order_reference','line_reference','listing_id','variant','specification','packs','units_per_pack'}
    allowed=required|{'total_cost_minor','expected_on','source_reference'}
    try:
        if '\x00' in data.csv_text:raise ValueError()
        reader=csv.DictReader(io.StringIO(data.csv_text.lstrip('\ufeff')),strict=True)
        headers=reader.fieldnames
        if not headers or len(headers)!=len(set(headers)) or not required<=set(headers) or not set(headers)<=allowed:raise ValueError()
        rows=[]
        for index,row in enumerate(reader,2):
            if len(rows)>=50 or None in row or any(v is None for v in row.values()):raise ValueError()
            entry={'row':index,'line':None,'status':'needs_review','message':''}
            try:
                clean={k:v.strip() for k,v in row.items()}
                for name in ('packs','units_per_pack','total_cost_minor'):
                    if name in clean and clean[name]!='':
                        if not re.fullmatch(r'\d{1,9}',clean[name]):raise ValueError()
                        clean[name]=int(clean[name])
                    elif name=='total_cost_minor':clean.pop(name,None)
                if not clean.get('expected_on'):clean.pop('expected_on',None)
                line=PurchaseLine.model_validate(clean);entry['line']=line.model_dump(mode='json');mapping,units=resolve(doc,line)
                old=doc['lines'].get(line_key(owner,line))
                if old and old['request_hash']!=store.digest(canonical(line)):fail('Existing order line differs; retain original facts and review.')
                entry.update(status='already_recorded' if old else 'ready',ordered_units=units,consumable_id=mapping['consumable_id'],message='Dry-run only; no purchase or stock changed.')
            except HTTPException as exc:entry['message']=str(exc.detail)
            except (ValidationError,ValueError,TypeError):entry['message']='Check required fields, ISO dates and nonnegative integer units/costs. No row was imported.'
            rows.append(entry)
        if not rows:raise ValueError()
        return {'rows':rows,'can_import':all(r['status'] in ('ready','already_recorded') for r in rows),'stock_changed':False}
    except (csv.Error,ValueError,TypeError):raise HTTPException(422,'Use a UTF8 CSV with the expected unique headers and at most fifty complete rows.') from None

def receipt(db,user,data):
    owner=square.tenant(user);doc,raw=store.lock(db,owner,user);fingerprint=store.digest(data.model_dump(mode='json'))
    old=doc['events'].get(data.request_id)
    if old:
        if old['request_hash']!=fingerprint:fail('This receipt request already has different retained facts.')
        return read(db,user)
    source_hash=store.digest(data.model_dump(mode='json',exclude={'request_id'}))
    for previous in doc['events'].values():
        if (previous['line_id'],previous['kind'],previous['source_event_reference'])==(data.line_id,data.kind,data.source_event_reference):
            if previous['source_hash']!=source_hash:fail('This physical package/disposition reference already has different retained facts. Review it instead of receiving it again.')
            return read(db,user)
    if not data.confirmed:fail('Confirm the physical receipt, damage, cancellation or actual return before recording units.')
    if data.occurred_at.tzinfo is None or data.occurred_at.astimezone(timezone.utc)>datetime.now(timezone.utc):fail('Use an actual event time with its time zone; future notices are not receipt evidence.')
    line=doc['lines'].get(data.line_id)
    if line is None:raise HTTPException(404,'Purchase line not found')
    if len(doc['events'])>=1000:fail('Material receipt history capacity reached. Retain evidence for review.')
    balance=totals(doc,data.line_id)
    if data.kind in ('receive_good','receive_damaged','cancel') and data.units>balance['incoming_units']:fail('This exceeds the remaining unreceived purchase units. Returns never reopen incoming allowance.')
    if data.kind=='return_good' and data.units>balance['receive_good']-balance['return_good']:fail('Return exceeds good units physically received from this line.')
    if data.kind=='return_damaged' and data.units>balance['receive_damaged']-balance['return_damaged']:fail('Return exceeds damaged units physically received from this line.')
    if data.kind!='receive_good' and not data.note:fail('Keep a reason for damage, cancellation or return.')
    item,identity=store.own_material(db,owner,line['consumable_id'],locked=True)
    if identity['uid']!=line['material_uid']:fail('Purchase material identity changed. Retain its records.')
    eid=uuid.uuid5(uuid.NAMESPACE_URL,f'odin:material-event:{owner}:{data.request_id}').hex
    delta=data.units if data.kind=='receive_good' else -data.units if data.kind=='return_good' else 0
    if delta:change_stock(db,item,delta,eid)
    doc['events'][data.request_id]={'id':eid,**data.model_dump(mode='json'),'request_hash':fingerprint,'source_hash':source_hash,'recorded_at':batches.now(),'consumable_id':item.id,'stock_delta':delta}
    store.save(db,owner,doc,raw);db.flush();answer=read(db,user);db.commit();return answer

def consume_hardware(db,owner,hardware,uses,confirmed,assembly_id):
    """Caller holds material reservation then sorted hardware rows; no commit."""
    expected=defaultdict(int);managed={};unmanaged=[]
    for h in hardware:
        identity=store.identity(db,h['consumable_id'])
        if identity is None:unmanaged.append(h['consumable_id']);continue
        if identity['owner']!=owner:raise HTTPException(404,'Material not found')
        item,_=store.own_material(db,owner,h['consumable_id'])
        expected[item.id]+=h['expected_units'];managed[item.id]=item
    supplied={u.consumable_id:u for u in (uses or [])}
    if len(supplied)!=len(uses or []) or set(supplied)!=set(expected):fail('Confirm actual usage for every managed BOM material exactly once, with no unrelated materials.')
    if expected and not confirmed:fail('Confirm the actual managed hardware used during this assembly.')
    result=[]
    for cid in sorted(expected):
        u=supplied[cid]
        if u.units!=expected[cid] and not u.reason:fail('Explain actual hardware use that differs from the full assembly BOM, including reuse, rework or partial rejects.')
        if u.units:change_stock(db,managed[cid],-u.units,assembly_id)
        result.append({'name':managed[cid].name,'consumable_id':cid,'material_uid':store.identity(db,cid)['uid'],'expected_units':expected[cid],'used_units':u.units,'reason':u.reason})
    return {'managed':result,'unmanaged_ids':sorted(set(unmanaged)),'confirmed':bool(confirmed and expected),'reclaimed_units':0}
