"""Read-only reconciliation and exactly-once reservation for explicit stock sends."""
import json
from collections import defaultdict

from fastapi import HTTPException
from sqlalchemy import select, update
from modules.orders import batch_production as batches, square_store as store
from modules.orders import square_delivery_client as wire


def operation(db, owner, addition_id):
    row=db.execute(select(batches.additions).where(batches.additions.c.owner==owner,
        batches.additions.c.id==addition_id)).mappings().one_or_none()
    if row is None:raise HTTPException(404,'Stock addition not found')
    if row['state'] not in ('prepared','unknown','confirmed','withdrawn_baseline') or type(row['attempts']) is not int or row['attempts']<0:
        wire.invalid()
    return dict(row)


def snapshot(db, owner, row, *, lock=False, enforce_barrier=True):
    request=wire.read_request(row['request_body'])
    receipt=db.execute(select(batches.receipts).where(batches.receipts.c.owner==owner,
        batches.receipts.c.id==row['receipt_id'])).mappings().one_or_none()
    if receipt is None:
        from modules.orders.finishing import receipt as finish_receipt
        finished=finish_receipt(db,owner,row['receipt_id'])
        if finished is not None:receipt={'definition':batches.packed(finished)}
    if not receipt or row['receipt_id']!=request['idempotency_key']:wire.invalid()
    try:
        detail=json.loads(receipt['definition']);dest=json.loads(row['destination'])
        doc,encoded=store.load(db,owner)
        if not doc:raise HTTPException(409,'Square is disconnected. Keep this addition for review.')
        if dest.get('merchant_id')!=doc['merchant_id'] or dest.get('environment')!=doc['environment']:
            raise HTTPException(409,'Square account changed. This addition must not be retargeted.')
        quantities=defaultdict(int)
        for output in detail['outputs']:
            if type(output['accepted']) is not int or not 0<=output['accepted']<=10000:wire.invalid()
            quantity = output.get('send_quantity', output['accepted'])
            if type(quantity) is not int or not 0 <= quantity <= output['accepted']: wire.invalid()
            if quantity:
                product=store.own_product(db,owner,output['product_id'],lock=lock)
                if product.name!=output['product_name']:raise HTTPException(409,'An inspected product changed. Keep the receipt for review.')
                quantities[product.id]+=quantity
        expected=[]
        for pid,quantity in sorted(quantities.items()):
            mapping=doc['mappings'].get(str(pid))
            if not mapping:raise HTTPException(409,'An inspected product no longer has its Square mapping.')
            expected.append({'type':'ADJUSTMENT','adjustment':{
                'catalog_object_id':mapping['variation_id'],'from_location_id':mapping['location_id'],'to_location_id':mapping['location_id'],
                'from_state':'NONE','to_state':'IN_STOCK','quantity':str(quantity),
                'occurred_at':detail['occurred_at'],'reference_id':row['receipt_id']}})
        if request['changes']!=expected:wire.invalid()
        if enforce_barrier:
            from modules.orders.stock_baseline import barrier
            barrier(db, owner, row, doc)
        return doc,encoded,dest
    except (KeyError,ValueError,TypeError,AttributeError):wire.invalid()


def live_check(doc, row, *, reconcile=False):
    reader=wire.SquareInventoryProbe(doc['environment'],doc['token'])
    if reader.merchant()!=doc['merchant_id']:raise HTTPException(409,'Square credential belongs to a different account.')
    locations={v['id'] for v in reader.locations(doc['merchant_id'])}
    request=wire.read_request(row['request_body'])
    for change in request['changes']:
        a=change['adjustment']
        if a['to_location_id'] not in locations:raise HTTPException(409,'The saved Square location is inactive or belongs to another account.')
        if not reader.variation(a['catalog_object_id'],a['to_location_id'])['tracked']:
            raise HTTPException(409,'Square inventory tracking is disabled for a saved variation.')
    if not reconcile:
        reader.require_write_scope()
    return reader.reconciled(row['request_body']) if reconcile else False


def lock_snapshot(db,user,addition_id,revision):
    owner=store.tenant(user)
    store.lock_owner(db,owner,user)
    row=operation(db,owner,addition_id)
    doc,encoded,dest=snapshot(db,owner,row,lock=True)
    if doc['revision']!=revision:raise HTTPException(409,'Square configuration changed during checks. Reload and review.')
    doc, _ = store.fence(db, owner, doc, encoded)
    # Re-read operation, baseline and review binding after the connector CAS.
    row=operation(db,owner,addition_id)
    doc,_,dest=snapshot(db,owner,row,lock=True)
    return row,doc,dest


def result(row, readiness, message, *, enabled=False):
    return {'id':row['id'],'state':row['state'],'attempts':row['attempts'],
        'readiness':readiness,'message':message,'square_writes_enabled':enabled,'retry_allowed':False}


def check(db,user,addition_id):
    owner=store.tenant(user);row=operation(db,owner,addition_id)
    if row['state']=='withdrawn_baseline':return result(row,'withdrawn_baseline','Counted in baseline; permanently withdrawn, never sent.')
    if row['state']=='confirmed':return result(row,'confirmed','Square previously acknowledged this addition. Do not send it again.')
    try:
        doc,_,_=snapshot(db,owner,row)
        revision=doc['revision']
        db.rollback()  # No database locks held during remote reads.
        matched=live_check(doc,row,reconcile=row['state']=='unknown')
        preview=[]
        if row['state']=='prepared':
            reader=wire.SquareInventoryProbe(doc['environment'],doc['token'])
            locations={v['id']:v['name'] for v in reader.locations(doc['merchant_id'])}
            for change in wire.read_request(row['request_body'])['changes']:
                a=change['adjustment']
                pid,mapping=next((pid,m) for pid,m in doc['mappings'].items() if
                    m['location_id']==a['to_location_id'] and m['variation_id']==a['catalog_object_id'])
                if a['to_location_id'] not in locations:
                    raise HTTPException(409,'The saved Square location became unavailable during checks.')
                preview.append({'product': store.own_product(db,owner,int(pid)).name, 'quantity': a['quantity'],
                    'location': locations[a['to_location_id']], 'location_id': a['to_location_id'],
                    'current_count': reader.count(a['catalog_object_id'],a['to_location_id'])['quantity']})

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
        enabled=store.writes_enabled(doc)
        db.rollback()
        return {**result(current,'checks_passed','Account, location, mapping, tracking and write scope checks passed.',enabled=enabled), 'preview':preview}
    except HTTPException as exc:
        db.rollback()
        # Reader/store errors have fixed text, never upstream response bodies.
        return result(row,'blocked',str(exc.detail))


def execute_prepared(db,user,addition_id):
    """Reserve exactly once after live reads and fenced local revalidation."""
    from modules.orders.production_auth import assert_production_operator
    assert_production_operator(user)
    if not wire.LIVE_WRITES_ENABLED:
        raise HTTPException(503,'Square stock writes require both activation keys')
    owner=store.tenant(user);row=operation(db,owner,addition_id)
    if row['state']=='confirmed':return result(row,'confirmed','Already confirmed; no request sent.')
    if row['state']=='withdrawn_baseline':
        raise HTTPException(409,'This addition was withdrawn into a baseline and is never sent.')
    if row['state']!='prepared' or row['attempts']!=0:
        raise HTTPException(409,'Uncertain additions require read-only reconciliation, never another send.')
    wire.validate_request(row['request_body'])  # Refuse retired evidence before reads or reservation.
    doc,_,_=snapshot(db,owner,row)
    if not store.writes_enabled(doc):
        raise HTTPException(503,'Square stock writes require both activation keys')
    revision=doc['revision'];db.rollback()
    live_check(doc,row)
    current,doc,_=lock_snapshot(db,user,addition_id,revision)
    if current['request_body']!=row['request_body']:wire.invalid()
    # Reservation linearizes disable, cutover, withdrawal and concurrent sends.
    if not store.writes_enabled(doc):
        db.rollback()
        raise HTTPException(503,'Square stock writes require both activation keys')
    changed=db.execute(update(batches.additions).where(batches.additions.c.id==addition_id,
        batches.additions.c.owner==owner,batches.additions.c.state=='prepared',batches.additions.c.attempts==0)
        .values(state='unknown',attempts=1)).rowcount
    if changed!=1:
        db.rollback();raise HTTPException(409,'This addition is already reserved. Reconcile its outcome instead of sending again.')
    from core.audit import log_audit
    log_audit(db, 'production.stock_send_reserved', 'production_stock_addition', None,
        {'addition_id':addition_id, 'receipt_id':current['receipt_id'], 'operator_id':user['id'],
         'auth_kind':user.get('_auth_kind'), 'api_token_id':user.get('_token_id')})
    db.commit()  # Durable intent; connector changes from here cannot retarget saved body.
    try:confirmed=wire.SquareWriter(doc['environment'],doc['token']).send(row['request_body'],tenant_enabled=doc.get('stock_writes_enabled')) is True
    except Exception:confirmed=False
    if confirmed:
        db.execute(update(batches.additions).where(batches.additions.c.id==addition_id,
            batches.additions.c.owner==owner,batches.additions.c.state=='unknown').values(state='confirmed'))
        db.commit()
    return result(operation(db,owner,addition_id),'confirmed' if confirmed else 'manual_review',
        'Square acknowledged this addition.' if confirmed else 'Outcome unknown. Use read-only reconciliation; do not resend.')
