"""Encrypted metadata on existing immutable order lifetimes, no payment transport."""
import hashlib,json,uuid
from datetime import datetime,timezone,date
from decimal import Decimal,InvalidOperation
from zoneinfo import ZoneInfo
from fastapi import HTTPException
from cryptography.fernet import InvalidToken
from sqlalchemy import select,text,update,String,cast
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload
from core.models import SystemConfig
from core.registry import registry
from core.dependencies import log_audit
from modules.orders.models import Order
from modules.orders import square_store

PREFIX='show_fulfillment:'


def now():return datetime.now(timezone.utc)
def status(order):return getattr(order.status,'value',order.status)
def key(owner,uid):return f'{PREFIX}{owner}:{uid}'


def identity(db,order):
    return db.execute(text('SELECT economic_uid FROM orders WHERE id=:id'),{'id':order.id}).scalar_one_or_none()


def has_record(db,order):
    uid=identity(db,order)
    return bool(uid and db.execute(select(SystemConfig.key).where(SystemConfig.key==key(order.org_id or 0,uid))).first())


def owned_order(db,user,order_id,lock=False):
    owner=square_store.tenant(user)
    clause=Order.org_id.is_(None) if owner==0 else Order.org_id==owner
    order=db.execute(select(Order).options(selectinload(Order.items)).where(Order.id==order_id,clause).execution_options(populate_existing=True)).scalar_one_or_none()
    if order is None:raise HTTPException(404,'Order not found')
    if lock:
        provider=registry.get_provider('OrderFulfillmentProvider')
        if provider is None:raise HTTPException(503,'Order fulfillment is unavailable')
        provider.lock_orders(db,[order_id]);db.expire_all()
        order=db.execute(select(Order).options(selectinload(Order.items)).where(Order.id==order_id,clause).execution_options(populate_existing=True)).scalar_one_or_none()
        if order is None:raise HTTPException(404,'Order ownership changed')
    return order


def load_identity(db,owner,uid):
    if not uid:return None,None
    raw=db.execute(select(SystemConfig.value).where(SystemConfig.key==key(owner,uid))).scalar_one_or_none()
    if raw is None:return None,None
    if not isinstance(raw,str) or len(raw)>500000:raise HTTPException(503,'Show-order record requires administrator review.')
    try:
        value=json.loads(square_store.cipher().decrypt(raw.encode()))
        if value['version']!=1 or value['owner']!=owner or value['order_uid']!=uid or not isinstance(value['revision'],str):raise ValueError()
        from modules.orders.show_order_schemas import Intake
        Intake.model_validate({**value['details'],'revision':value['revision']})
        if not isinstance(value.get('history',[]),list) or len(value.get('history',[]))>20:raise ValueError()
        return value,raw
    except (InvalidToken,ValueError,TypeError,KeyError,AttributeError):
        raise HTTPException(503,'Show-order details cannot be decrypted or validated. Restore the server encryption key; retain the order.') from None


def load(db,order):
    return load_identity(db,order.org_id or 0,identity(db,order))


def cents(value):
    try:
        n=Decimal(str(value))
        if not n.is_finite() or n<0 or n*100!=(n*100).to_integral_value() or n>1000000:raise ValueError()
        return int(n*100)
    except (InvalidOperation,ValueError,TypeError):raise HTTPException(409,'Review the order quote: amounts must be known, nonnegative USD cents.') from None


def binding(db,order):
    uid=identity(db,order)
    if not uid:raise HTTPException(409,'This legacy order has no immutable identity. Create a new intake order; retain the original history.')
    if not order.items or len(order.items)>100:raise HTTPException(409,'A show order needs between one and one hundred line items.')
    items=[]
    for item in sorted(order.items,key=lambda i:i.id):
        if type(item.quantity) is not int or not 1<=item.quantity<=10000:raise HTTPException(409,'Every order item needs a positive whole quantity.')
        item_uid=db.execute(text('SELECT economic_uid FROM order_items WHERE id=:id'),{'id':item.id}).scalar_one_or_none()
        if not item_uid:raise HTTPException(409,'An order item lacks an immutable identity. Retain it for review.')
        items.append([item_uid,item.product_id,item.quantity,cents(item.unit_price)])
    total=cents(order.revenue);shipping=cents(order.shipping_charged)
    if total<=0 or shipping>total:raise HTTPException(409,'Set a positive total including shipping, with shipping no greater than that total.')
    value={'customer_name':order.customer_name,'customer_email':order.customer_email,'uid':uid,'total_minor':total,'shipping_minor':shipping,'items':items}
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest(),total


def save(db,order,doc,previous):
    uid=identity(db,order)
    if not uid:raise HTTPException(409,'Order identity missing')
    doc['revision']=uuid.uuid4().hex
    value=square_store.cipher().encrypt(json.dumps(doc,separators=(',',':')).encode()).decode()
    try:
        if previous is None:db.add(SystemConfig(key=key(order.org_id or 0,uid),value=value));db.flush()
        elif db.execute(update(SystemConfig).where(SystemConfig.key==key(order.org_id or 0,uid),
            cast(SystemConfig.value,String)==json.dumps(previous)).values(value=value).execution_options(synchronize_session=False)).rowcount!=1:
            raise HTTPException(409,'Show-order details changed. Reload before saving.')
    except IntegrityError:
        db.rollback();raise HTTPException(409,'Show-order details changed. Reload before saving.') from None


def utc(value):
    if value is None or value.tzinfo is None:raise HTTPException(409,'Enter an actual timestamp with its time zone.')
    value=value.astimezone(timezone.utc)
    if value>now():raise HTTPException(409,'A future timestamp is not evidence of payment or parcel handoff.')
    return value


def summary(db,order,doc):
    result={'enrolled':bool(doc),'order_id':order.id,'order_status':status(order),'payment_verification':'manual operator attestation only',
            'production_remaining':sum(max(0,i.quantity-i.fulfilled_quantity) for i in order.items),
            'can_dispatch':False,'state':'not_enrolled','needs_review':False,'revision':None,'details':None}
    try:current,total=binding(db,order);result['quoted_total_minor']=total
    except HTTPException:
        if not doc:return {**result,'state':'quote_review','needs_review':True}
        current=None
    if not doc:return result
    details=doc['details'];result.update(revision=doc['revision'],details=details,dispatch=doc.get('dispatch'),
        payment_history=[{'amount_received_minor':h['details']['amount_received_minor'],'payment_method':h['details']['payment_method'],
                          'payment_reference':h['details']['payment_reference'],'recorded_at':h['recorded_at']} for h in doc.get('history',[])])
    same=current==doc['binding']
    paid=details['payment_confirmed'] and details['amount_received_minor']==doc['quoted_total_minor']
    complete=bool(order.items) and all(i.quantity>0 and i.fulfilled_quantity>=i.quantity for i in order.items)
    if doc.get('dispatch'):
        return {**result,'state':'dispatched' if status(order)=='shipped' and same else 'dispatch_review','needs_review':not same or status(order)!='shipped'}
    if status(order)=='cancelled':
        received=details['amount_received_minor']>0 or any(h['details']['amount_received_minor']>0 for h in doc.get('history',[]))
        return {**result,'state':'cancelled_payment_review' if received else 'cancelled','needs_review':received}
    if not same:return {**result,'state':'quote_changed','needs_review':True}
    if not paid:return {**result,'state':'partial_payment' if 0<details['amount_received_minor']<doc['quoted_total_minor'] else 'payment_unconfirmed'}
    committed=details['address_confirmed']
    result.update(state='paid_committed' if committed else 'address_unconfirmed',
        can_dispatch=bool(committed and complete and status(order) not in ('shipped','cancelled')))
    result['overdue']=committed and date.fromisoformat(details['dispatch_by'])<now().astimezone(ZoneInfo(details.get('dispatch_timezone','America/New_York'))).date()
    return result


def read(db,user,order_id):
    order=owned_order(db,user,order_id);doc,_=load(db,order)
    return summary(db,order,doc)


def intake(db,user,order_id,data):
    square_store.lock_owner(db,square_store.tenant(user),user)
    order=owned_order(db,user,order_id,lock=True);old,previous=load(db,order)
    if old and old.get('dispatch'):raise HTTPException(409,'Dispatched handoff records are immutable. Retain this order for review.')
    if status(order) in ('shipped','cancelled'):raise HTTPException(409,'Cannot enroll or rewrite a shipped or cancelled order.')
    if (old['revision'] if old else None)!=data.revision:raise HTTPException(409,'Show-order details changed. Reload before saving.')
    digest,total=binding(db,order)
    if data.amount_received_minor>total:raise HTTPException(409,'Received payment exceeds the current quote. Review payment and quote before recording it.')
    if data.amount_received_minor:
        if not data.payment_reference or data.payment_received_at is None:raise HTTPException(409,'Record the external receipt reference and actual payment time.')
        utc(data.payment_received_at)
    if data.payment_confirmed and (data.amount_received_minor!=total or not data.payment_reference or data.payment_received_at is None):
        raise HTTPException(409,'Full-payment confirmation requires the full quoted amount, receipt reference and payment time.')
    details=data.model_dump(mode='json',exclude={'revision'})
    history=list(old.get('history',[])) if old else []
    if old:
        receipt_fields=('amount_received_minor','payment_method','payment_reference','payment_received_at')
        if old['details']['amount_received_minor']>0 and any(old['details'][k]!=details[k] for k in receipt_fields) and not data.correction_note:
            raise HTTPException(409,'Give a correction reason when changing an already-recorded payment receipt; the earlier evidence is retained.')
        if len(history)>=20:raise HTTPException(409,'This commitment has twenty retained revisions. Keep it for administrator review rather than discarding payment history.')
        history.append({'details':old['details'],'quoted_total_minor':old['quoted_total_minor'],'binding':old['binding'],'recorded_at':now().isoformat()})
    doc={'version':1,'owner':order.org_id or 0,'order_uid':identity(db,order),'binding':digest,'quoted_total_minor':total,
         'details':details,'dispatch':None,'history':history,'updated_by':user['id'],'updated_at':now().isoformat()}
    save(db,order,doc,previous)
    log_audit(db,'show_order.intake_saved','order',order.id,'Operator-entered show-order facts saved; no payment captured or message sent')
    db.commit();return summary(db,order,doc)


def dispatch(db,user,order_id,data):
    square_store.lock_owner(db,square_store.tenant(user),user)
    order=owned_order(db,user,order_id,lock=True);doc,previous=load(db,order)
    if not doc or doc['revision']!=data.revision:raise HTTPException(409,'Reload the saved show-order commitment before confirming handoff.')
    if doc.get('dispatch'):raise HTTPException(409,'Handoff already recorded. Do not dispatch this order again.')
    provider=registry.get_provider('OrderFulfillmentProvider');provider.reconcile_orders(db,[order_id]);db.expire_all()
    order=owned_order(db,user,order_id)
    facts=summary(db,order,doc)
    if not facts['can_dispatch'] or not data.physically_handed_over:raise HTTPException(409,'Confirm full payment, unchanged address/quote, complete production and actual parcel handoff first.')
    when=utc(data.handed_at)
    paid_at=datetime.fromisoformat(doc['details']['payment_received_at'].replace('Z','+00:00')).astimezone(timezone.utc)
    if when<paid_at:raise HTTPException(409,'Parcel handoff cannot precede the recorded full-payment time.')
    doc['dispatch']={'physically_handed_over':True,'recorded_by':user['id'],'carrier':data.carrier,'tracking_number':data.tracking_number,'handed_at':when.isoformat()}
    save(db,order,doc,previous)
    order.status='shipped';order.shipped_date=when;order.tracking_number=data.tracking_number
    log_audit(db,'show_order.parcel_handoff','order',order.id,'Operator confirmed physical parcel handoff; no label purchased or message sent')
    db.commit();return summary(db,order,doc)


def guard_patch(db,order,changes):
    doc,_=load(db,order)
    if not doc:return
    if doc.get('dispatch') and changes:raise HTTPException(409,'Dispatched show orders cannot be rewritten through generic editing.')
    if changes.get('status') in ('shipped',) or any(k in changes for k in ('shipped_date','tracking_number')):
        raise HTTPException(409,'Use the show-order physical handoff confirmation, not generic shipping fields.')


def guard_structure(db,order_id):
    row=db.execute(text('SELECT economic_uid,org_id FROM orders WHERE id=:id'),{'id':order_id}).first()
    if row:
        doc,_=load_identity(db,row.org_id or 0,row.economic_uid)
        if doc and doc.get('dispatch'):raise HTTPException(409,'Dispatched show-order contents are immutable.')


def guard_delete_or_ship(db,order):
    if has_record(db,order):raise HTTPException(409,'Keep the show-order record. Use its guided handoff, or cancel and review any received payment; no refund is automatic.')


def overview(db,user,offset=0):
    owner=square_store.tenant(user);clause=Order.org_id.is_(None) if owner==0 else Order.org_id==owner
    orders=db.execute(select(Order).options(selectinload(Order.items)).where(clause,Order.status.notin_(('shipped',)))
        .order_by(Order.id.desc()).offset(offset).limit(51)).scalars().all()
    rows=[]
    for order in orders[:50]:
        doc,_=load(db,order)
        if not doc:continue
        facts=summary(db,order,doc)
        rows.append({'order_id':order.id,'order_number':order.order_number,'state':facts['state'],
            'dispatch_by':doc['details']['dispatch_by'],'production_remaining':facts['production_remaining'],
            'overdue':facts.get('overdue',False),'priority':'paid commitment before routine stock; protect wholesale promises' if facts['state']=='paid_committed' else 'needs review'})
    rows.sort(key=lambda r:(r['state']!='paid_committed',r['dispatch_by'],r['order_id']))
    return {'orders':rows,'next_offset':offset+50 if len(orders)>50 else None,
        'capacity_verified':False,'message':'Promises are recorded, not capacity-certified. No queue priorities were changed.'}


def assert_tenant_deletable(db,owner):
    """Preserve private receipt history and prevent organization-ID reuse exposure."""
    if db.execute(select(SystemConfig.key).where(SystemConfig.key.startswith(f"show_fulfillment:{owner}:"))).first():
        raise HTTPException(409,"Retain this organization while show-order payment and handoff records exist.")
