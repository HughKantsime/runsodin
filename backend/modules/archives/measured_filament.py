"""Prospective terminal observations. Caller owns the physical attempt lock/commit.

Only frozen first-start receipts enter valuation. A retained observation is read
before retry; inventory markers have no bearing on financial event insertion.
"""
import json
from decimal import Decimal, InvalidOperation, localcontext, ROUND_HALF_UP
from uuid import uuid4
from sqlalchemy import text
from core.dispatch_observation_executor import TransactionView
from .valuation_financial_graph import prove_financial_graph
from .valuation_projection import parse_private_valuation, MAX_SNAPSHOT_BYTES
from .valuation_retained_evidence import verify_retained_basis

REASONS = ('missing_measurement','invalid_measurement','missing_historical_valuation',
 'unresolved_allocation','unresolved_purchase_evidence','currency_mismatch',
 'valuation_bound_exceeded','mixed_order_plate','archive_ownership_unresolved')
MAX_GRAMS = Decimal('1000000000')
MAX_AMOUNT = Decimal('1000000000000000000')


def measurement(value):
    if value is None:return 'unavailable',None
    if type(value) not in (int,float,Decimal):return 'invalid',None
    try:
        number=Decimal(str(value))
        if not number.is_finite() or not 0<=number<=MAX_GRAMS or not -12<=number.as_tuple().exponent<=9:
            return 'invalid',None
        return 'printer_reported',format(number,'f')
    except (InvalidOperation,ValueError):return 'invalid',None


def calculate(grams, price, net):
    from core.spool_purchase_validation import canonical_decimal
    if not canonical_decimal(price) or not canonical_decimal(net,positive=True):raise ValueError('Invalid frozen purchase basis')
    basis,value=measurement(grams)
    if basis!='printer_reported':raise ValueError('Invalid terminal measurement')
    with localcontext() as context:
        context.prec=90
        amount=Decimal(value)*Decimal(price)/Decimal(net)
        if amount>MAX_AMOUNT:raise ValueError('Valuation bound exceeded')
        return format(amount,'f')


def value_observation(db, graph, snapshot, basis, grams, *, ownership_known=True):
    empty={'spool_uid':None,'purchase_evidence_nonce':None,'currency':None,'amount_decimal':None,'reason':None}
    def unknown(reason):return dict(empty,reason=reason)
    if not ownership_known:return unknown('archive_ownership_unresolved')
    if basis!='printer_reported':return unknown('missing_measurement' if basis=='unavailable' else 'invalid_measurement')
    try:unit=parse_private_valuation(snapshot)
    except ValueError:return unknown('missing_historical_valuation')
    target=unit['target']
    if target is None or (unit['attempt_uid'],unit['attempt_birth'])!=(graph['attempt_uid'],graph['attempt_birth']) or any(target[k]!=graph[k] for k in ('order_uid','item_uid','job_uid','owner_scope','owner_key')):
        return unknown('missing_historical_valuation')
    try:verify_retained_basis(db,unit)
    except ValueError:return unknown('unresolved_purchase_evidence')
    foreign=db.execute(text('''SELECT 1 FROM cost_attempt_participation p JOIN cost_obligations o ON o.obligation_uid=p.obligation_uid
      WHERE p.attempt_uid=:uid AND (o.order_uid<>:order_uid OR o.owner_scope<>:scope OR o.owner_key<>:owner) LIMIT 1'''),
      {'uid':graph['attempt_uid'],'order_uid':graph['order_uid'],'scope':graph['owner_scope'],'owner':graph['owner_key']}).first()
    if foreign:return unknown('mixed_order_plate')
    try:plan=json.loads(snapshot)['material_plan'];allocations=plan['allocations']
    except (ValueError,KeyError,TypeError):return unknown('unresolved_allocation')
    # A whole-printer total cannot be split by slicer ratios, even if totals agree.
    if not isinstance(plan,dict) or not isinstance(allocations,list) or len(allocations)!=1 or not isinstance(allocations[0],dict) or plan.get('state')!='complete' or len(unit['sources'])!=1:return unknown('unresolved_allocation')
    source=unit['sources'][0]
    if source.get('reason') in ('currency_mismatch','missing_target_currency'):return unknown('currency_mismatch')
    if source['state']!='known':return unknown('unresolved_purchase_evidence')
    # Prove the material slot names exactly the retained spool lifetime.
    lifetime=db.execute(text('SELECT resource_id FROM cost_source_lifetimes WHERE birth_nonce=:birth'),{'birth':source['birth_nonce']}).first()
    if lifetime is None or allocations[0].get('spool_id')!=lifetime[0]:return unknown('unresolved_allocation')
    try:amount=calculate(Decimal(grams),source['price_decimal'],source['net_weight_g_decimal'])
    except (ValueError,InvalidOperation):return unknown('valuation_bound_exceeded')
    return dict(empty,spool_uid=source['spool_uid'],purchase_evidence_nonce=source['evidence_nonce'],currency=source['currency'],amount_decimal=amount)


def _append_event(db, observation_id, value, *, expected_revision, audit_reason):
    """Append a correction with compare-and-swap; original evidence is never edited.

    Internal transaction API only. Corrections recompute from the original frozen
    observation, so this slice cannot rewrite attribution, grams or purchase basis.
    """
    if type(expected_revision) is not int or not 0<=expected_revision<2147483647 or not isinstance(audit_reason,str) or not 0<len(audit_reason.strip())<=200:
        raise ValueError('Invalid measured filament revision')
    db.execute(text('''INSERT INTO filament_cost_events
      (event_id,observation_id,revision,expected_revision,audit_reason,spool_uid,purchase_evidence_nonce,currency,amount_decimal,reason,calculation_version)
      VALUES(:event_id,:observation_id,:revision,:expected_revision,:audit_reason,:spool_uid,:purchase_evidence_nonce,:currency,:amount_decimal,:reason,1)'''),
      dict(value,event_id=uuid4().hex,observation_id=observation_id,revision=expected_revision+1,expected_revision=expected_revision,audit_reason=audit_reason))


def retain_terminal(executor, attempt_id, *, ownership_known=True, reconciliation_reason=None):
    db=TransactionView(executor)
    row=db.execute(text('SELECT status,filament_used_g,spool_snapshot FROM print_jobs WHERE id=:id'),{'id':attempt_id}).first()
    if row is None or row[0] not in ('completed','failed','cancelled'):raise ValueError('Terminal attempt required')
    try:graph=prove_financial_graph(db,attempt_id)
    except ValueError:return None # Legacy/unbound attempts cannot acquire historical authority.
    basis,grams=measurement(row[1])
    reported=grams if basis=='printer_reported' else (None if row[1] is None else str(row[1])[:128])
    snapshot=row[2]
    if type(snapshot) is not str or len(snapshot.encode('utf-8'))>MAX_SNAPSHOT_BYTES:snapshot='{}'
    # No snapshot parsing until immutable identity/owner proof succeeded.
    original=db.execute(text('SELECT observation_id,outcome,measurement_basis,measured_grams,snapshot_json,reported_value FROM terminal_filament_measurements WHERE attempt_uid=:uid AND allocation_key=\'attempt-total\''),{'uid':graph['attempt_uid']}).first()
    if original is not None:
        if tuple(original[1:])!=(row[0],basis,grams,snapshot,reported):raise ValueError('Conflicting terminal measurement callback')
        return original[0]
    identifier=uuid4().hex
    db.execute(text('''INSERT INTO terminal_filament_measurements
      (observation_id,attempt_uid,attempt_birth,owner_scope,owner_key,order_uid,item_uid,allocation_key,outcome,measurement_basis,measured_grams,snapshot_json,reported_value,reconciliation_reason)
      VALUES(:observation_id,:attempt_uid,:attempt_birth,:owner_scope,:owner_key,:order_uid,:item_uid,'attempt-total',:outcome,:measurement_basis,:measured_grams,:snapshot_json,:reported_value,:reconciliation_reason)'''),
      {**{k:graph[k] for k in ('attempt_uid','attempt_birth','owner_scope','owner_key','order_uid','item_uid')},'observation_id':identifier,'outcome':row[0],'measurement_basis':basis,'measured_grams':grams,'snapshot_json':snapshot,'reported_value':reported,'reconciliation_reason':'inventory_shortfall' if reconciliation_reason=='inventory_shortfall' else None})
    value=value_observation(db,graph,snapshot,basis,grams,ownership_known=ownership_known)
    _append_event(db,identifier,value,expected_revision=0,audit_reason='Original terminal observation')
    return identifier


def display_amount(value):
    with localcontext() as context:
        context.prec=100
        return format(value.quantize(Decimal('.01'),rounding=ROUND_HALF_UP),'f')


def revise_valuation(executor, observation_id, *, expected_revision, audit_reason):
    """Internal correction API: re-evaluate original evidence with expected revision.

    No measurement, attribution, currency or receipt replacement is accepted.
    Caller supplies authority/audit context; there is no correction HTTP surface.
    """
    db=TransactionView(executor)
    db.execute(text('UPDATE terminal_filament_measurements SET observation_id=observation_id WHERE 0')) if db.dialect.name=='sqlite' else None
    lock=' FOR UPDATE NOWAIT' if db.dialect.name=='postgresql' else ''
    row=db.execute(text('SELECT attempt_uid,attempt_birth,order_uid,item_uid,owner_scope,owner_key,measurement_basis,measured_grams,snapshot_json FROM terminal_filament_measurements WHERE observation_id=:id'+lock),{'id':observation_id}).first()
    if row is None:raise ValueError('Missing terminal observation')
    bound=db.execute(text('SELECT job_uid FROM cost_attempt_bindings WHERE attempt_uid=:uid'),{'uid':row[0]}).first()
    if bound is None:raise ValueError('Missing retained binding')
    graph=dict(zip(('attempt_uid','attempt_birth','order_uid','item_uid','owner_scope','owner_key'),row[:6]),job_uid=bound[0])
    original=db.execute(text('SELECT reason FROM filament_cost_events WHERE observation_id=:id AND revision=1'),{'id':observation_id}).first()
    if original is None:raise ValueError('Missing original valuation')
    value=value_observation(db,graph,row[8],row[6],row[7],ownership_known=original[0]!='archive_ownership_unresolved')
    _append_event(db,observation_id,value,expected_revision=expected_revision,audit_reason=audit_reason)
