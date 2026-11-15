"""Ranked "what to print next" list across products. Advice only: no stock ledger.

Stock comes from Square, read-only: ONE count read per location for every mapped
variation, at most once per CACHE_SECONDS per organization (per server process).
When Square cannot be read (down, refused, or ITAR mode) the list falls back to
the last recorded snapshot and says how old it is; it never guesses a count.
Nothing here writes to Square. Queued plates are created by replenishment_queue.
"""
import json
import math
import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from fastapi import HTTPException
from sqlalchemy import func, select, text

from core.base import OrderStatus
from core.job_timeline import utc
from modules.orders import assortment, batch_production as batches, square_store as store
from modules.orders.models import Order, OrderItem, Product
from modules.orders.square_client import SquareReader

CACHE_SECONDS = 120
LIMIT = 1000
OPEN_ORDERS = (OrderStatus.PENDING, OrderStatus.IN_PROGRESS, OrderStatus.PARTIAL)
VELOCITY_WEEKS = 4
VELOCITY_MIN_DAYS = 14
UNKNOWN = {'quantity': None, 'calculated_at': None, 'state': 'unknown'}

_reads = {}  # owner -> {'at', 'read_at', 'revision', 'counts', 'error'}
_guard = threading.Lock()


def iso(value):
    return value.astimezone(timezone.utc).isoformat().replace('+00:00', 'Z')


def whole(quantity):
    """A recorded quantity as a whole, non-negative number of pieces, else None."""
    try:
        value = Decimal(quantity)
        if not value.is_finite() or value < 0 or value != value.to_integral_value():
            return None
        return int(value)
    except (InvalidOperation, TypeError, ValueError):
        return None


def forget_reads(owner=None):
    with _guard:
        _reads.clear() if owner is None else _reads.pop(owner, None)


def read_counts(doc, pairs):
    """Live counts for (variation, location) pairs: one read per location. Raises HTTPException."""
    reader = SquareReader(doc['environment'], doc['token'])
    if reader.merchant() != doc['merchant_id']:
        raise HTTPException(409, 'Square account no longer matches this connection.')
    active = {location['id'] for location in reader.locations(doc['merchant_id'])}
    by_location = {}
    for variation, location in pairs:
        by_location.setdefault(location, []).append(variation)
    counts = {}
    for location in sorted(by_location):
        if location not in active:
            counts.update({(variation, location): {**UNKNOWN, 'state': 'location_inactive'} for variation in by_location[location]})
            continue
        for variation, result in reader.counts(location, by_location[location]).items():
            counts[(variation, location)] = result
    return counts


def wanted_pairs(doc, policy):
    """Mapped variations of active products, in a stable order."""
    pairs = []
    for pid, mapping in sorted((doc or {}).get('mappings', {}).items()):
        entry = policy['products'].get(pid)
        if entry and entry['assortment'] == 'active':
            pairs.append((mapping['variation_id'], mapping['location_id']))
    return sorted(set(pairs))


def stock(db, owner, *, remote=True, now=None):
    """Current knowledge of Square counts: {'counts', 'read_at', 'error', 'refresh_in', 'revision'}.

    Contacts Square only when ``remote`` and the last attempt for this owner is
    older than CACHE_SECONDS; a failed attempt also waits out the interval.
    The caller's transaction is ended before any remote read.
    """
    now = now or datetime.now(timezone.utc)
    doc, _ = store.load(db, owner)
    if doc is None:
        return {'counts': {}, 'read_at': None, 'error': None, 'refresh_in': 0, 'revision': None, 'connected': False}
    policy, _ = assortment.load(db, owner)
    pairs = wanted_pairs(doc, policy)
    with _guard:
        cached = _reads.get(owner)
        if cached and cached['revision'] != doc['revision']:
            del _reads[owner]
            cached = None
        age = time.monotonic() - cached['at'] if cached else None
        due = bool(pairs) and remote and (cached is None or age >= CACHE_SECONDS)
        if due:
            # Claim the interval before the read so concurrent requests do not each call Square.
            claim = {'at': time.monotonic(), 'read_at': cached['read_at'] if cached else None, 'revision': doc['revision'],
                     'counts': cached['counts'] if cached else {}, 'error': cached['error'] if cached else None}
            _reads[owner] = cached = claim
    if due:
        revision = doc['revision']
        db.rollback()  # never hold a transaction across a remote read
        try:
            counts, error = read_counts(doc, pairs), None
        except HTTPException as exc:
            counts, error = None, str(exc.detail)
        if counts is not None:
            from modules.orders.stock_snapshots import best_effort
            best_effort(db.get_bind(), owner, [(variation, location, result, now) for (variation, location), result in counts.items()
                                                if result['state'] in ('known', 'unknown')])
        # Re-read authority after the remote read: a reconnect or remap during it voids the result.
        latest, _ = store.load(db, owner)
        if latest is None or latest['revision'] != revision:
            forget_reads(owner)
            db.rollback()
            raise HTTPException(409, 'Square connection changed during the stock read. Reload before continuing.')
        with _guard:
            if counts is not None:
                cached.update(counts=counts, read_at=now, error=None)
            else:
                cached.update(error=error)
    remaining = max(0, math.ceil(CACHE_SECONDS - (time.monotonic() - cached['at']))) if cached else 0
    return {'counts': dict(cached['counts']) if cached and cached['read_at'] else {}, 'read_at': cached['read_at'] if cached else None,
            'error': cached['error'] if cached else None, 'refresh_in': remaining, 'revision': doc['revision'], 'connected': True}


def snapshots(db, owner):
    rows = db.execute(text('SELECT variation,location,quantity,read_at FROM stock_snapshots WHERE owner_scope=:scope AND owner_key=:owner LIMIT :limit'),
                      dict(scope='org' if owner else 'global', owner=owner, limit=LIMIT + 1)).all()
    result = {}
    for variation, location, quantity, read_at in rows[:LIMIT]:
        try:
            result[(variation, location)] = (quantity, utc(read_at))
        except (TypeError, ValueError):
            continue
    return result


def on_the_way(db, owner, recipes, can_see):
    """Pieces per product already coming, from the same evidence pending_work checks.

    Returns ({product_id: {'pieces', 'job_ids', 'awaiting_stock'}}, problem), with
    an internal 'restricted' flag for products receiving hidden work. A problem
    (an incomplete or unreadable scan) means nothing may be suggested.
    """
    coming = {}
    def entry(product_id):
        return coming.setdefault(product_id, {'pieces': 0, 'job_ids': [], 'awaiting_stock': 0})
    outstanding = db.execute(select(batches.additions.c.receipt_id).where(batches.additions.c.owner == owner,
                             batches.additions.c.state.in_(('prepared', 'unknown'))).limit(LIMIT + 1)).scalars().all()
    if len(outstanding) > LIMIT:
        return coming, 'Pending-addition scan is incomplete. Review existing work first.'
    details = {}
    recipe_models = {r['id']: r['payload']['model_id'] for r in recipes if r.get('payload')}
    if outstanding:
        details = {rid: {**json.loads(raw), 'recipe_id': recipe_id} for rid, recipe_id, raw in db.execute(select(
            batches.receipts.c.id, batches.receipts.c.recipe_id, batches.receipts.c.definition).where(
            batches.receipts.c.owner == owner, batches.receipts.c.id.in_(outstanding)))}
    for receipt_id in outstanding:
        detail = details.get(receipt_id)
        if detail is None:
            from modules.orders.finishing import receipt as finish_receipt
            detail = finish_receipt(db, owner, receipt_id)
        if detail is None:
            return coming, 'A pending stock addition has missing evidence. Review existing work first.'
        restricted = any(identifier is not None and not can_see(kind, identifier) for kind, identifier in (
            ('job', detail.get('job_id')), ('print_job', detail.get('print_job_id')),
            ('model', recipe_models.get(detail.get('recipe_id')))))
        for output in detail['outputs']:
            if output['accepted'] > 0:
                # Inspected pieces that Square does not count yet.
                pieces = output.get('send_quantity', output['accepted'])
                entry(output['product_id'])['awaiting_stock'] += pieces
                entry(output['product_id'])['pieces'] += pieces
                if pieces and restricted:
                    entry(output['product_id'])['restricted'] = True
    models = {r['payload']['model_id'] for r in recipes if r.get('payload')}
    if not models:
        return coming, None
    from modules.jobs.models import Job
    from sqlalchemy import literal_column
    clause = Job.charged_to_org_id.is_(None) if owner == 0 else Job.charged_to_org_id == owner
    rows = db.execute(select(Job, literal_column('jobs.economic_uid')).where(clause, Job.model_id.in_(models),
        Job.status.in_(assortment.WORK_STATES)).order_by(Job.id.desc()).limit(LIMIT + 1)).all()
    if len(rows) > LIMIT:
        return coming, 'Matching-job scan is incomplete. Review existing work first.'
    uids = [uid for _, uid in rows if uid]
    inspected = set(db.execute(select(batches.receipts.c.job_uid).where(batches.receipts.c.owner == owner,
        batches.receipts.c.job_uid.in_(uids))).scalars()) if uids else set()
    for job, job_uid in rows:
        if str(getattr(job.status, 'value', job.status)) == 'completed' and job_uid in inspected:
            continue
        makes = {}
        for recipe in recipes:
            if not recipe.get('payload') or not assortment.job_may_produce(job, recipe['payload']):
                continue
            for output in recipe['outputs']:
                if output['kind'] == 'main':
                    makes[output['product_id']] = max(makes.get(output['product_id'], 0), output['expected'] * max(job.quantity or 1, 1))
        for product_id, pieces in makes.items():
            entry(product_id)['pieces'] += pieces
            entry(product_id)['job_ids'].append(job.id)
            if not can_see('model', job.model_id) or not can_see('job', job.id):
                entry(product_id)['restricted'] = True
    return coming, None


def open_orders(db, owner):
    """Unfulfilled pieces per product on open orders, with the oldest order date."""
    clause = Order.org_id.is_(None) if owner == 0 else Order.org_id == owner
    placed = func.coalesce(Order.order_date, Order.created_at)
    left = OrderItem.quantity - func.coalesce(OrderItem.fulfilled_quantity, 0)
    rows = db.execute(select(OrderItem.product_id, func.sum(left), func.min(placed)).join(Order, Order.id == OrderItem.order_id)
                      .where(clause, Order.status.in_(OPEN_ORDERS), left > 0).group_by(OrderItem.product_id).limit(LIMIT + 1)).all()
    return {pid: {'pieces': int(pieces), 'oldest': oldest} for pid, pieces, oldest in rows[:LIMIT]}


def velocity(db, owner, now):
    """Units sold per week from ODIN orders over the last four weeks; {} with under 14 days of orders."""
    clause = Order.org_id.is_(None) if owner == 0 else Order.org_id == owner
    placed = func.coalesce(Order.order_date, Order.created_at)
    first = db.execute(select(func.min(placed)).where(clause)).scalar()
    naive = now.astimezone(timezone.utc).replace(tzinfo=None)
    try:
        if first is None or naive - utc(first).replace(tzinfo=None) < timedelta(days=VELOCITY_MIN_DAYS):
            return {}
    except (TypeError, ValueError):
        return {}
    rows = db.execute(select(OrderItem.product_id, func.sum(OrderItem.quantity)).join(Order, Order.id == OrderItem.order_id)
                      .where(clause, Order.status != OrderStatus.CANCELLED, placed >= naive - timedelta(weeks=VELOCITY_WEEKS))
                      .group_by(OrderItem.product_id).limit(LIMIT + 1)).all()
    return {pid: {'units_per_week': round(int(units) / VELOCITY_WEEKS, 1), 'source': 'ODIN orders', 'weeks': VELOCITY_WEEKS}
            for pid, units in rows[:LIMIT] if units}


def plate_hours(db, recipes):
    files = {r['payload'].get('print_file_id') for r in recipes if r.get('payload')} - {None}
    if not files:
        return {}
    from sqlalchemy import bindparam
    rows = db.execute(text('SELECT id,print_time_seconds FROM print_files WHERE id IN :ids').bindparams(bindparam('ids', expanding=True)),
                      {'ids': sorted(files)}).all()
    return {fid: round(seconds / 3600, 2) for fid, seconds in rows if isinstance(seconds, (int, float)) and seconds > 0}


def plates_for(shortfall, per_plate, minimum_batch):
    """Whole plates to cover the shortfall; a run is never smaller than the smallest batch."""
    if shortfall <= 0:
        return 0
    return math.ceil(max(shortfall, minimum_batch) / per_plate)


def build(db, owner, user, known, *, now=None):
    """Rows for every active product from local data plus the given stock knowledge. No remote reads."""
    now = now or datetime.now(timezone.utc)
    policy, _ = assortment.load(db, owner)
    doc, _ = store.load(db, owner)
    mappings = (doc or {}).get('mappings', {})
    clause = Product.org_id.is_(None) if owner == 0 else Product.org_id == owner
    products = db.execute(select(Product.id, Product.name).where(clause).order_by(Product.id).limit(LIMIT + 1)).all()
    active = [p for p in products[:LIMIT] if (policy['products'].get(str(p.id)) or {}).get('assortment') == 'active']
    raw = db.execute(select(batches.recipes.c.id, batches.recipes.c.definition).where(batches.recipes.c.owner == owner).limit(LIMIT + 1)).all() if active else []
    recipes = [{'id': rid, **json.loads(definition)} for rid, definition in raw[:LIMIT]]
    scan_problem = 'Recipe scan is incomplete. Review recipes before planning more stock.' if len(raw) > LIMIT else None
    orders = open_orders(db, owner) if active else {}
    sold = velocity(db, owner, now) if active else {}
    hours = plate_hours(db, recipes)
    saved = snapshots(db, owner) if active else {}
    from core.controlled_parts import allowed, any_controlled_data
    controlled = bool(active) and any_controlled_data(db)
    visible = {}
    def can_see(kind, identifier):
        if not controlled:
            return True
        key = (kind, identifier)
        if key not in visible:
            visible[key] = allowed(db, kind, identifier, user)
        return visible[key]
    def minimum(product_id):
        return assortment.rule(policy['products'].get(str(product_id)))['minimum_main_batch'] or 1

    coming, coming_problem = on_the_way(db, owner, recipes, can_see) if active else ({}, None)
    hidden_products = {pid for pid, work in coming.items() if work.get('restricted')}
    rows = []
    for product in active:
        numbers = assortment.numbers(policy['products'][str(product.id)])
        way = (coming.get(product.id) if product.id not in hidden_products else None) or {'pieces': 0, 'job_ids': [], 'awaiting_stock': 0}
        demand = orders.get(product.id)
        row = {'product_id': product.id, 'product_name': product.name, **numbers, 'status': 'blocked', 'reason': '', 'below_point': None,
               'on_hand': None, 'shortfall': None, 'plates_needed': 0, 'recipe': None, 'rank': None, 'rank_reason': None,
               'on_the_way': {'pieces': way['pieces'], 'job_ids': sorted(way['job_ids'])[:20], 'awaiting_stock': way['awaiting_stock']},
               'open_orders': {'pieces': demand['pieces'], 'oldest': str(demand['oldest']) if demand['oldest'] else None} if demand else None,
               'velocity': sold.get(product.id)}
        rows.append(row)
        mapping = mappings.get(str(product.id))
        if not mapping:
            row['reason'] = 'Map this product to a Square item to see its stock.' if doc else 'Connect Square and map this product to see its stock.'
            continue
        pair = (mapping['variation_id'], mapping['location_id'])
        live = known['counts'].get(pair)
        quantity = read_at = source = None
        if live is not None:
            if live['state'] == 'location_inactive':
                row['reason'] = 'The mapped Square location is inactive. Review the mapping.'
                continue
            if live['state'] == 'known':
                quantity, read_at, source = whole(live['quantity']), known['read_at'], 'square'
                if quantity is None:
                    row['reason'] = 'Square quantity is negative or fractional. Review the physical count; nothing is inferred.'
                    continue
            else:
                row['reason'] = 'Square has no stock count for this product. Check that it is tracked by stock count.'
                continue
        elif pair in saved:
            quantity, read_at, source = whole(saved[pair][0]), saved[pair][1], 'snapshot'
            if quantity is None:
                row['reason'] = 'The last recorded Square quantity is negative or fractional. Review the physical count.'
                continue
        else:
            row['reason'] = 'No stock count has been read for this product yet.'
            continue
        row['on_hand'] = {'quantity': quantity, 'read_at': iso(read_at), 'age_seconds': max(0, int((now - read_at).total_seconds())), 'source': source}
        uncovered = bool(demand) and demand['pieces'] > quantity
        row['below_point'] = quantity < numbers['reorder_point']
        if not row['below_point'] and not uncovered:
            row.update(status='ok', shortfall=0, reason='At or above its reorder point.')
            continue
        # Square stock is public to this caller; restricted production and derived
        # shortfalls are not. Block only when actual hidden work could be duplicated.
        if product.id in hidden_products:
            row['reason'] = 'The plate that makes this product is controlled work you cannot see.'
            continue
        # Refill to the target when below the point; open orders that stock cannot cover count in full.
        wanted = max(numbers['target'] if row['below_point'] else 0, demand['pieces'] if uncovered else 0)
        row['shortfall'] = max(0, wanted - quantity - way['pieces'])
        row['uncovered_orders'] = uncovered
        problem = coming_problem or scan_problem
        if problem:
            row['reason'] = problem
            continue
        if row['shortfall'] == 0:
            row.update(status='below', reason=f"Below its point, but {way['pieces']} piece(s) already on the way cover it.")
            continue
        candidates, run_plates, hidden_recipe = [], {}, False
        for recipe in recipes:
            main = next((o for o in recipe['outputs'] if o['product_id'] == product.id and o['kind'] == 'main'), None)
            if main is None or recipe.get('qualified') is not True or not recipe.get('payload'):
                continue
            if not can_see('model', recipe['payload']['model_id']):
                hidden_recipe = True
                continue
            run_plates[recipe['id']] = max(
                plates_for(row['shortfall'], main['expected'], numbers['minimum_batch']),
                *(math.ceil(minimum(o['product_id']) / o['expected']) for o in recipe['outputs'] if o['kind'] == 'main'))
            per_plate_hours = hours.get(recipe['payload'].get('print_file_id'))
            candidates.append({'id': recipe['id'], 'name': recipe['name'], 'pieces_per_plate': main['expected'],
                               'printer_id': recipe['payload']['printer_id'], 'materials': recipe['materials'],
                               'hours_per_plate': per_plate_hours,
                               'hours_per_piece': round(per_plate_hours / main['expected'], 3) if per_plate_hours else None})
        if not candidates:
            row['reason'] = ('The plate that makes this product is controlled work you cannot see.' if hidden_recipe else
                             'Needs printing, but no qualified recipe meets its smallest batch. Qualify one first.')
            continue
        # Cheapest known print time per piece first; unknown times last; then the bigger plate.
        candidates.sort(key=lambda c: (c['hours_per_piece'] is None, c['hours_per_piece'] or 0, -c['pieces_per_plate'], c['id']))
        row['recipe'] = candidates[0]
        row['plates_needed'] = run_plates[candidates[0]['id']]
        row.update(status='below', reason=f"Needs {row['shortfall']} more to reach {wanted}.")
    return rank(rows), len(products) > LIMIT


def rank(rows):
    """Open orders stock cannot cover (oldest first), then zero stock, then shortfall as a share of target."""
    def key(row):
        hours = (row['recipe'] or {}).get('hours_per_piece')
        tie = (hours is None, hours or 0, row['product_name'].lower(), row['product_id'])
        if row.get('uncovered_orders'):
            return (0, row['open_orders']['oldest'] or '', 0, *tie)
        if row['on_hand']['quantity'] == 0:
            return (1, '', 0, *tie)
        share = row['shortfall'] / row['target'] if row['target'] else 0
        return (2, '', -share, *tie)
    needing = sorted((r for r in rows if r['status'] == 'below' and r['plates_needed'] > 0), key=key)
    for position, row in enumerate(needing, 1):
        group = key(row)[0]
        row['rank'] = position
        row['rank_reason'] = (f"Open orders need {row['open_orders']['pieces']} and stock is {row['on_hand']['quantity']}." if group == 0 else
                              'Out of stock.' if group == 1 else
                              f"{round(100 * row['shortfall'] / row['target']) if row['target'] else 0}% of its target is missing.")
    for row in rows:
        row.pop('uncovered_orders', None)
    listed = {id(r) for r in needing}
    rest = [r for r in rows if id(r) not in listed]
    order = {'below': 0, 'blocked': 1, 'ok': 2}
    rest.sort(key=lambda r: (order[r['status']], r['product_name'].lower(), r['product_id']))
    return needing + rest


def knowledge_public(known, rows, now):
    read_at = known['read_at']
    used_snapshot = any(r['on_hand'] and r['on_hand']['source'] == 'snapshot' for r in rows)
    return {'connected': known['connected'], 'read_at': iso(read_at) if read_at else None,
            'age_seconds': max(0, int((now - read_at).total_seconds())) if read_at else None,
            'source': 'square' if read_at and not known['error'] else 'snapshot' if used_snapshot or known['error'] else 'none',
            'error': known['error'], 'refresh_in_seconds': known['refresh_in'], 'cache_seconds': CACHE_SECONDS}


def listing(db, user, *, remote=True):
    owner = store.tenant(user)
    now = datetime.now(timezone.utc)
    known = stock(db, owner, remote=remote, now=now)
    if known['error']:
        # Square could not be read: show the last recorded snapshot with its age, never the failed attempt's leftovers.
        known = {**known, 'counts': {}}
    rows, truncated = build(db, owner, user, known, now=now)
    return {'generated_at': iso(now), 'stock': knowledge_public(known, rows, now), 'rows': rows, 'truncated': truncated,
            'print_next_count': sum(1 for r in rows if (r['shortfall'] or 0) > 0)}


def print_next_count(db, user):
    """How many products need a plate, from recorded stock only (never contacts Square)."""
    return listing(db, user, remote=False)['print_next_count']
