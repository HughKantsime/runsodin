"""Cutover attestations and append-only addition reviews, under the connector CAS."""
import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import select, update

from core.dependencies import log_audit
from core.models import AuditLog
from core.rbac import is_interactive_session
from modules.orders import square_store as store
from modules.orders.square_setup_auth import assert_square_setup, setup_audit, fresh_setup_actor


def admin(user):
    store.assert_interactive(user)
    if user.get('role') != 'admin' or not is_interactive_session(user):
        raise HTTPException(403, 'Interactive admin access is required')


def utc(value):
    """Server legacy naive observation timestamps were recorded in UTC."""
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except OverflowError:
        raise HTTPException(422, 'Enter a baseline time that can be represented in UTC') from None
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(409, 'Missing or conflicting observation time evidence; review this batch') from None


def version(mapping):
    baseline = (mapping or {}).get('stock_baseline')
    return baseline['version'] if baseline else None


def posted_outputs(db, owner, row):
    from modules.orders import batch_production as batches
    raw = db.execute(select(batches.receipts.c.definition).where(
        batches.receipts.c.owner == owner, batches.receipts.c.id == row['receipt_id'])).scalar_one_or_none()
    if raw is None:
        from modules.orders.finishing import receipt
        finished = receipt(db, owner, row['receipt_id'])
        if finished is None:
            raise HTTPException(409, 'Missing retained production receipt; review this addition')
        raw = batches.packed(finished)
    return [o for o in json.loads(raw)['outputs'] if o.get('send_quantity', o['accepted']) > 0]


def versions(db, owner, row, doc):
    return {str(o['product_id']): version(doc['mappings'].get(str(o['product_id'])))
            for o in posted_outputs(db, owner, row)}


def barrier(db, owner, row, doc):
    # An unknown operation has already crossed the boundary. Reads must remain
    # possible after baseline/credential changes; there is never a second write.
    if row['state'] != 'prepared':
        return
    outputs = posted_outputs(db, owner, row)
    current = versions(db, owner, row, doc)
    if all(o.get('baseline_version') == current[str(o['product_id'])] for o in outputs):
        return
    reviews = db.execute(select(AuditLog.details).where(
        AuditLog.action == 'square.baseline_review',
        AuditLog.details['addition_id'].as_string() == row['id']).order_by(AuditLog.id.desc())).scalars()
    for review in reviews:
        if (isinstance(review, dict) and review.get('owner') == owner
                and review.get('addition_id') == row['id']
                and review.get('answer') == 'not_counted' and review.get('versions') == current):
            return
    raise HTTPException(409, 'baseline_review')


def locked_connection(db, user, revision):
    owner = store.tenant(user)
    store.lock_owner(db, owner, user)
    doc, encoded = store.load(db, owner)
    store.check_revision(doc, revision)
    if doc is None:
        raise HTTPException(409, 'Connect Square first')
    doc, encoded = store.fence(db, owner, doc, encoded)
    return owner, doc, encoded


def set_baseline(db, user, product_id, data):
    from modules.orders import batch_production as batches
    assert_square_setup(user)
    attribution = setup_audit(user, getattr(data, 'approval_note', None))
    at = utc(data.at)
    if attribution:
        validate_baseline_time(at)
        read_token_baseline(db, user, product_id, data)
    owner, doc, encoded = locked_connection(db, user, data.revision)
    store.own_product(db, owner, product_id)
    mapping = doc['mappings'].get(str(product_id))
    if not mapping:
        raise HTTPException(409, 'Map the product before attesting its baseline')
    if attribution and mapping.get('stock_baseline') is not None:
        raise HTTPException(409, 'This product already has a stock baseline. A signed-in administrator must change it.')
    pending = []
    for row in db.execute(select(batches.additions).where(batches.additions.c.owner == owner,
            batches.additions.c.state.in_(['unknown', 'prepared']))).mappings():
        if any(o['product_id'] == product_id for o in posted_outputs(db, owner, row)):
            if row['state'] == 'unknown':
                raise HTTPException(409, 'Reconcile unknown additions before setting this baseline')
            pending.append(row['id'])
    if attribution:
        attribution = setup_audit(fresh_setup_actor(db, user), data.approval_note)
        validate_baseline_time(at)
    baseline = {'version': str(uuid.uuid4()), 'at': at.isoformat(),
                'quantity': data.quantity, 'attested_by': user['id']}
    previous_baseline = mapping.get('stock_baseline')
    mapping['stock_baseline'] = baseline
    store.save(db, owner, doc, encoded)
    log_audit(db, 'square.stock_baseline', 'product', product_id,
              {'owner': owner, **baseline, 'pending_additions': pending,
               'previous_baseline': previous_baseline, **attribution})
    db.commit()
    return {'connection': store.public(db, owner, doc), 'pending_additions': pending}


def validate_baseline_time(at):
    age = (datetime.now(timezone.utc) - at).total_seconds()
    if age < -60 or age > 15 * 60:
        raise HTTPException(422, 'The baseline time must be no more than 15 minutes old and no more than 60 seconds in the future.')


def read_token_baseline(db, user, product_id, data):
    """Read before locking, then the caller rechecks revision and authorization."""
    from modules.orders.square_client import SquareReader, refuse_under_itar
    refuse_under_itar()
    owner = store.tenant(user)
    store.own_product(db, owner, product_id)
    doc, _ = store.load(db, owner)
    store.check_revision(doc, data.revision)
    if doc is None:
        raise HTTPException(409, 'Connect Square first')
    mapping = doc['mappings'].get(str(product_id))
    if not mapping:
        raise HTTPException(409, 'Map the product before attesting its baseline')
    if mapping.get('stock_baseline') is not None:
        raise HTTPException(409, 'This product already has a stock baseline. A signed-in administrator must change it.')
    db.rollback()
    reader = SquareReader(doc['environment'], doc['token'])
    if not reader.variation(mapping['variation_id'], mapping['location_id'])['tracked']:
        raise HTTPException(409, 'Square is not tracking a stock count for this variation at this location. In Square, open the item, set this variation to "Track by stock count" (not "Track by availability") for this location, then map it again.')
    count = reader.count(mapping['variation_id'], mapping['location_id'])
    if count['state'] != 'known' or count['quantity'] is None:
        raise HTTPException(409, 'Square has no stock count for this variation at this location. Nothing was changed.')
    if Decimal(count['quantity']) != data.quantity:
        raise HTTPException(409, f"Square shows {count['quantity']}; you stated {data.quantity}. Nothing was changed.")


def review(db, user, addition_id, data):
    from modules.orders import batch_production as batches, stock_recovery as recovery
    admin(user)
    owner, doc, _ = locked_connection(db, user, data.revision)
    row = recovery.operation(db, owner, addition_id)
    if row['state'] != 'prepared' or row['attempts'] != 0:
        raise HTTPException(409, 'Only an unreserved prepared addition can be reviewed or withdrawn')
    # Validates destination and saved request, but deliberately skips the barrier
    # being resolved. No remote reads are needed for a cutover attestation.
    recovery.snapshot(db, owner, row, enforce_barrier=False)
    current = versions(db, owner, row, doc)
    answers = data.answers
    if set(answers) != set(current) or len(set(answers.values())) != 1:
        raise HTTPException(409, 'baseline_review: attest the same answer for every posted product; mixed answers cannot split this request')
    answer = next(iter(answers.values()))
    changed = db.execute(update(batches.additions).where(batches.additions.c.owner == owner,
        batches.additions.c.id == addition_id, batches.additions.c.state == 'prepared',
        batches.additions.c.attempts == 0).values(
            state='withdrawn_baseline' if answer == 'counted' else 'prepared')).rowcount
    if changed != 1:
        raise HTTPException(409, 'Addition was reserved before this review')
    log_audit(db, 'square.baseline_review', 'stock_addition', None,
              {'owner': owner, 'addition_id': addition_id, 'answer': answer,
               'versions': current, 'attested_by': user['id']})
    db.commit()
    return recovery.result(recovery.operation(db, owner, addition_id),
        'withdrawn_baseline' if answer == 'counted' else 'reviewed', 'Baseline review recorded', enabled=store.writes_enabled(doc))
