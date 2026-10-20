"""Read-only readiness/reconciliation and an unreachable, disabled send executor.

No route or worker calls execute_prepared. Unknown operations are never resent.
"""
import json
from collections import defaultdict

from fastapi import HTTPException
from sqlalchemy import String, cast, select, update
from core.models import SystemConfig
from modules.orders import batch_production as batches, square_store as store
from modules.orders import square_delivery_client as wire


def operation(db, owner, addition_id):
    row=db.execute(select(batches.additions).where(batches.additions.c.owner==owner,
        batches.additions.c.id==addition_id)).mappings().one_or_none()
    if row is None:raise HTTPException(404,'Stock addition not found')
    if row['state'] not in ('prepared','unknown','confirmed') or type(row['attempts']) is not int or row['attempts']<0:
        wire.invalid()
    return dict(row)


def snapshot(db, owner, row, *, lock=False):
    request=wire.validate_request(row['request_body'])
    receipt=db.execute(select(batches.receipts).where(batches.receipts.c.owner==owner,
        batches.receipts.c.id==row['receipt_id'])).mappings().one_or_none()
    if not receipt or row['receipt_id']!=request['idempotency_key']:wire.invalid()
    try:
        detail=json.loads(receipt['definition']);dest=json.loads(row['destination'])
        doc,encoded=store.load(db,owner)
        if not doc:raise HTTPException(409,'Square is disconnected. Keep this addition for review.')
        if dest.get('merchant_id')!=doc['merchant_id'] or dest.get('environment')!=doc['environment']:
            raise HTTPException(409,'Square account changed. This addition must not be retargeted.')
        if dest.get('connection_revision')!=doc['revision']:
            raise HTTPException(409,'Square connection or mappings changed after inspection. Review the retained addition; do not recreate it.')
        quantities=defaultdict(int)
        for output in detail['outputs']:
            if type(output['accepted']) is not int or not 0<=output['accepted']<=10000:wire.invalid()
            if output['accepted']:
                product=store.own_product(db,owner,output['product_id'],lock=lock)
                if product.name!=output['product_name']:raise HTTPException(409,'An inspected product changed. Keep the receipt for review.')
                quantities[product.id]+=output['accepted']
        expected=[]
        for pid,quantity in sorted(quantities.items()):
            mapping=doc['mappings'].get(str(pid))
            if not mapping:raise HTTPException(409,'An inspected product no longer has its Square mapping.')
            expected.append({'type':'ADJUSTMENT','adjustment':{
                'catalog_object_id':mapping['variation_id'],'location_id':mapping['location_id'],
                'from_state':'NONE','to_state':'IN_STOCK','quantity':str(quantity),
                'occurred_at':detail['occurred_at'],'reference_id':row['receipt_id']}})
        if request['changes']!=expected:wire.invalid()
        return doc,encoded,dest
    except (KeyError,ValueError,TypeError,AttributeError):wire.invalid()


def live_check(doc, row, *, reconcile=False):
    reader=wire.SquareInventoryProbe(doc['environment'],doc['token'])
    if reader.merchant()!=doc['merchant_id']:raise HTTPException(409,'Square credential belongs to a different account.')
    locations={v['id'] for v in reader.locations(doc['merchant_id'])}
    request=wire.validate_request(row['request_body'])
    for change in request['changes']:
        a=change['adjustment']
        if a['location_id'] not in locations:raise HTTPException(409,'The saved Square location is inactive or belongs to another account.')
        if not reader.variation(a['catalog_object_id'],a['location_id'])['tracked']:
            raise HTTPException(409,'Square inventory tracking is disabled for a saved variation.')
    return reader.reconciled(row['request_body']) if reconcile else False


def lock_snapshot(db,user,addition_id,revision):
    owner=store.tenant(user)
    store.lock_owner(db,owner,user)
    row=operation(db,owner,addition_id)
    doc,encoded,dest=snapshot(db,owner,row,lock=True)
    if doc['revision']!=revision:raise HTTPException(409,'Square configuration changed during checks. Reload and review.')
    # Also fences global owner0, which has no group row.
    if db.execute(update(SystemConfig).where(SystemConfig.key==store.key(owner),
        cast(SystemConfig.value,String)==json.dumps(encoded)).values(value=SystemConfig.value)).rowcount!=1:
        raise HTTPException(409,'Square configuration changed during checks. Reload and review.')
    # Reload after acquiring the shared connector reservation.
    current,_=store.load(db,owner)
    if current['revision']!=revision:raise HTTPException(409,'Square configuration changed during checks. Reload and review.')
    return row,doc,dest


def result(row, readiness, message):
    return {'id':row['id'],'state':row['state'],'attempts':row['attempts'],
        'readiness':readiness,'message':message,'square_writes_enabled':False,'retry_allowed':False}


def check(db,user,addition_id):
    owner=store.tenant(user);row=operation(db,owner,addition_id)
    if row['state']=='confirmed':return result(row,'confirmed','Square previously acknowledged this addition. Do not send it again.')
    try:
        doc,_,_=snapshot(db,owner,row)
        revision=doc['revision']
        db.rollback()  # No database locks held during remote reads.
        matched=live_check(doc,row,reconcile=row['state']=='unknown')
        current,_,_=lock_snapshot(db,user,addition_id,revision)
        if current['state']=='confirmed':
            db.rollback();return result(current,'confirmed','Square previously acknowledged this addition. Do not send it again.')
        if current['state']!=row['state'] or current['request_body']!=row['request_body']:
            raise HTTPException(409,'Addition changed while checking. Reload before continuing.')
        if current['state']=='unknown':
            if matched:
                db.execute(update(batches.additions).where(batches.additions.c.id==addition_id,
                    batches.additions.c.owner==owner,batches.additions.c.state=='unknown').values(state='confirmed'))
                db.commit();current['state']='confirmed'
                return result(current,'confirmed','Exact Square adjustment history confirms this addition. No stock was sent.')
            db.rollback()
            return result(current,'manual_review','The outcome is still unknown. Retain this receipt and review Square history; do not create another addition.')
        db.rollback()
        return result(current,'checks_passed','Account, location, mapping and tracking checks passed. Sending remains disabled.')
    except HTTPException as exc:
        db.rollback()
        # Reader/store errors have fixed text, never upstream response bodies.
        return result(row,'blocked',str(exc.detail))


def execute_prepared(db,user,addition_id):
    """Future integration seam only. Disabled before credentials or network access."""
    from core.rbac import is_interactive_session
    if user.get('role') not in ('admin','operator') or not is_interactive_session(user):
        raise HTTPException(403,'Interactive operator access is required')
    if not wire.LIVE_WRITES_ENABLED:
        raise HTTPException(503,'Live Square stock updates are disabled in this build')
    owner=store.tenant(user);row=operation(db,owner,addition_id)
    if row['state']=='confirmed':return result(row,'confirmed','Already confirmed; no request sent.')
    if row['state']!='prepared' or row['attempts']!=0:
        raise HTTPException(409,'Uncertain additions require read-only reconciliation, never another send.')
    doc,_,_=snapshot(db,owner,row);revision=doc['revision'];db.rollback()
    live_check(doc,row)
    current,doc,_=lock_snapshot(db,user,addition_id,revision)
    if current['request_body']!=row['request_body']:wire.invalid()
    changed=db.execute(update(batches.additions).where(batches.additions.c.id==addition_id,
        batches.additions.c.owner==owner,batches.additions.c.state=='prepared',batches.additions.c.attempts==0)
        .values(state='unknown',attempts=1)).rowcount
    if changed!=1:
        db.rollback();raise HTTPException(409,'This addition is already reserved. Reconcile its outcome instead of sending again.')
    db.commit()  # Durable intent; connector changes from here cannot retarget saved body.
    try:confirmed=wire.SquareWriter(doc['environment'],doc['token']).send(row['request_body']) is True
    except Exception:confirmed=False
    if confirmed:
        db.execute(update(batches.additions).where(batches.additions.c.id==addition_id,
            batches.additions.c.owner==owner,batches.additions.c.state=='unknown').values(state='confirmed'))
        db.commit()
    return result(operation(db,owner,addition_id),'confirmed' if confirmed else 'manual_review',
        'Square acknowledged this addition.' if confirmed else 'Outcome unknown. Use read-only reconciliation; do not resend.')
