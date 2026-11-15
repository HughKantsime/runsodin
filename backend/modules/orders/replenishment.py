"""Ranked "what to print next" list across products. Advice only: no stock ledger.

Stock comes from Square, read-only: ONE count read per location for every mapped
variation, at most once per CACHE_SECONDS per organization (per server process).
When Square cannot be read (down, refused, or ITAR mode) the list falls back to
the last recorded snapshot and says how old it is; it never guesses a count.
Nothing here writes to Square. Queued plates are created by replenishment_queue.
"""
import math
import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from fastapi import HTTPException
from sqlalchemy import func, select, text

from core.base import OrderStatus
from core.job_timeline import utc
from modules.orders import assortment, replenishment_evidence as evidence, square_store as store
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


HIDDEN_WORK = 'Work in progress is hidden because this farm has restricted parts you are not cleared to see.'
PLANNING_FIELDS = ('on_the_way', 'shortfall', 'rank', 'rank_reason', 'recipe', 'plates_needed')


def full_view(db, owner, user):
    """One farm-wide gate; orphaned marks require clearance too. Admin is no bypass."""
    from core.controlled_parts import member
    audiences = db.execute(text('''
        SELECT c.audience_id FROM controlled_files c LEFT JOIN print_files f ON f.id=c.file_id
        WHERE f.id IS NULL OR COALESCE(f.org_id,0)=:owner
        UNION SELECT c.audience_id FROM controlled_jobs c LEFT JOIN jobs j ON j.id=c.job_id
        WHERE j.id IS NULL OR COALESCE(j.charged_to_org_id,0)=:owner
        UNION SELECT c.audience_id FROM controlled_job_tombstones c LEFT JOIN jobs j ON j.id=c.job_id
        WHERE j.id IS NULL OR COALESCE(j.charged_to_org_id,0)=:owner
    '''), {'owner': owner}).scalars()
    return all(member(db, user, audience) for audience in audiences)


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


def plate_hours(db, owner, recipes):
    files = {r['payload'].get('print_file_id') for r in recipes if r.get('payload')} - {None}
    if not files:
        return {}
    from sqlalchemy import bindparam
    rows = db.execute(text('SELECT id,print_time_seconds FROM print_files WHERE id IN :ids AND ((:owner=0 AND org_id IS NULL) OR (:owner>0 AND org_id=:owner))').bindparams(bindparam('ids', expanding=True)),
                      {'ids': sorted(files), 'owner': owner}).all()
    return {fid: round(seconds / 3600, 2) for fid, seconds in rows if isinstance(seconds, (int, float)) and seconds > 0}


def plates_for(shortfall, per_plate, minimum_batch):
    """Whole plates to cover the shortfall; a run is never smaller than the smallest batch."""
    if shortfall <= 0:
        return 0
    return math.ceil(max(shortfall, minimum_batch) / per_plate)


def build(db, owner, user, known, *, now=None, visible=None):
    """Rows for every active product from local data plus the given stock knowledge. No remote reads."""
    now = now or datetime.now(timezone.utc)
    policy, _ = assortment.load(db, owner)
    doc, _ = store.load(db, owner)
    mappings = (doc or {}).get('mappings', {})
    clause = Product.org_id.is_(None) if owner == 0 else Product.org_id == owner
    products = db.execute(select(Product.id, Product.name).where(clause).order_by(Product.id).limit(LIMIT + 1)).all()
    active = [p for p in products[:LIMIT] if (policy['products'].get(str(p.id)) or {}).get('assortment') == 'active']
    visible = full_view(db, owner, user) if visible is None else visible
    recipes, coming, scan_problem = [], {}, None
    if active and visible:
        try:
            recipes, coming = evidence.scan(db, owner)
        except evidence.ScanProblem as exc:
            scan_problem = str(exc)
    orders = open_orders(db, owner) if active else {}
    sold = velocity(db, owner, now) if active else {}
    hours = plate_hours(db, owner, recipes)
    saved = snapshots(db, owner) if active else {}
    def minimum(product_id):
        return assortment.rule(policy['products'].get(str(product_id)))['minimum_main_batch'] or 1

    rows = []
    for product in active:
        numbers = assortment.numbers(policy['products'][str(product.id)])
        way = coming.get(product.id) or {'pieces': 0, 'job_ids': [], 'awaiting_stock': 0}
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
        row['below_point'] = quantity < numbers['reorder_point']
        if not visible or scan_problem:
            continue
        uncovered = bool(demand) and demand['pieces'] > quantity
        if not row['below_point'] and not uncovered:
            row.update(status='ok', shortfall=0, reason='At or above its reorder point.')
            continue
        # Refill to the target when below the point; open orders that stock cannot cover count in full.
        wanted = max(numbers['target'] if row['below_point'] else 0, demand['pieces'] if uncovered else 0)
        row['shortfall'] = max(0, wanted - quantity - way['pieces'])
        row['uncovered_orders'] = uncovered
        if row['shortfall'] == 0:
            row.update(status='below', reason=f"Below its point, but {way['pieces']} piece(s) already on the way cover it.")
            continue
        candidates, run_plates = [], {}
        for recipe in recipes:
            main = next((o for o in recipe['outputs'] if o['product_id'] == product.id and o['kind'] == 'main'), None)
            if main is None or recipe.get('qualified') is not True:
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
            row['reason'] = 'Needs printing, but no qualified recipe meets its smallest batch. Qualify one first.'
            continue
        # Cheapest known print time per piece first; unknown times last; then the bigger plate.
        candidates.sort(key=lambda c: (c['hours_per_piece'] is None, c['hours_per_piece'] or 0, -c['pieces_per_plate'], c['id']))
        row['recipe'] = candidates[0]
        row['plates_needed'] = run_plates[candidates[0]['id']]
        row.update(status='below', reason=f"Needs {row['shortfall']} more to reach {wanted}.")
    if not visible or scan_problem:
        for row in rows:
            for field in PLANNING_FIELDS:
                row.pop(field, None)
            problem = HIDDEN_WORK if not visible else scan_problem
            row.update(status='blocked', reason=f"{row['reason']} {problem}" if row['reason'] else problem)
        rows.sort(key=lambda r: (r['product_name'].lower(), r['product_id']))
    else:
        rows = rank(rows)
    return rows, len(products) > LIMIT


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
    visible = full_view(db, owner, user)
    rows, truncated = build(db, owner, user, known, now=now, visible=visible)
    result = {'generated_at': iso(now), 'stock': knowledge_public(known, rows, now), 'rows': rows, 'truncated': truncated,
            'work_hidden': not visible}
    if visible and all('shortfall' in r for r in rows):
        result['print_next_count'] = sum(1 for r in rows if (r['shortfall'] or 0) > 0)
    return result


def print_next_count(db, user):
    """How many products need a plate, from recorded stock only (never contacts Square)."""
    return listing(db, user, remote=False).get('print_next_count')
