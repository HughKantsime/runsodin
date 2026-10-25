"""Private order aggregate; authorization precedes retained financial payload reads."""
from decimal import Decimal, localcontext
from fastapi import HTTPException
from sqlalchemy import text
from core.financial_read_authority import verify_financial_reader
from .valuation_parent_lock import lock_financial_attempt
from .valuation_financial_graph import prove_financial_graph
from .valuation_content_visibility import fence_content_membership,assert_content_visible
from .valuation_projection import parse_private_valuation
from .valuation_retained_evidence import verify_retained_basis
from .measured_filament import display_amount,MAX_AMOUNT,REASONS,value_observation

MAX_ATTEMPTS=1000


def summarize(attempts):
    known=[a for a in attempts if a['amount'] is not None]
    currencies={a['currency'] for a in known}
    mismatch=len(currencies)>1
    currency=next(iter(currencies)) if len(currencies)==1 else None
    with localcontext() as context:
        context.prec=100
        total=sum((Decimal(a['amount']) for a in known),Decimal(0)) if known and not mismatch else None
        overflow=total is not None and total>MAX_AMOUNT
        if overflow:total=None;currency=None
        def subtotal(outcomes):
            values=[Decimal(a['amount']) for a in known if a['outcome'] in outcomes]
            return display_amount(sum(values,Decimal(0))) if values and not mismatch and not overflow else None
        success=subtotal(('completed',));failed=subtotal(('failed','cancelled'))
    opened=sum(a['outcome']=='open' for a in attempts)
    unresolved=sum(a['amount'] is None and a['outcome']!='open' for a in attempts)
    reasons=sorted({a['reason'] for a in attempts if a['reason'] is not None}|({'currency_mismatch'} if mismatch else set())|({'valuation_bound_exceeded'} if overflow else set()))
    if not attempts:reasons=['no_participating_attempts']
    return {'measured_filament_known_subtotal':display_amount(total) if total is not None else None,
      'currency':currency,'filament_state':'fully_measured' if known and not unresolved and not opened and not mismatch and not overflow else 'partial' if known else 'unknown',
      'known_attempts':len(known),'unresolved_attempts':unresolved,'open_attempts':opened,'reasons':reasons,
      'production_cogs_state':'unavailable','successful_subtotal':success,'failed_cancelled_subtotal':failed,'attempts':attempts}


def read_order_measured_costs(db,request,actor,order_id):
    reader=verify_financial_reader(db,request,actor)
    fence_content_membership(db)
    try:
        if type(order_id) is not int or order_id<=0:raise ValueError('Invalid order')
        # Same parent-first lock used by the financial attempt reader.
        postgres=db.get_bind().dialect.name=='postgresql'
        query='SELECT id FROM orders WHERE id=:id FOR UPDATE NOWAIT' if postgres else 'UPDATE orders SET id=id WHERE id=:id RETURNING id'
        if db.execute(text(query),{'id':order_id}).first() is None:raise ValueError('Missing order')
        order=db.execute(text('SELECT org_id,economic_uid FROM orders WHERE id=:id'),{'id':order_id}).first()
        owner=('global',0) if order[0] is None else ('org',order[0])
        if not (reader.role=='admin' and reader.group_id is None) and (owner[0]!='org' or reader.group_id!=owner[1]):raise ValueError('Foreign order')
        uid=order[1]
        if uid:
            registry=db.execute(text('SELECT resource_kind,resource_id,owner_scope,owner_key FROM cost_identities WHERE identity_uid=:uid'),{'uid':uid}).first()
            if registry is None or tuple(registry)!=('order',order_id,*owner):raise ValueError('Order lifetime differs')
        # Include legacy attempts, open work and every retained bound physical attempt.
        rows=db.execute(text('''SELECT DISTINCT p.id FROM print_jobs p JOIN jobs j ON j.id=p.scheduled_job_id
          JOIN order_items i ON i.id=j.order_item_id WHERE i.order_id=:id
          UNION SELECT p.id FROM cost_attempt_bindings b LEFT JOIN print_jobs p ON p.economic_uid=b.attempt_uid
          WHERE b.order_uid=:uid LIMIT 1001'''),{'id':order_id,'uid':uid}).all()
        if len(rows)>MAX_ATTEMPTS:raise ValueError('Order attempt bound exceeded')
        attempts=[]
        for (identifier,) in sorted(rows,key=lambda r:r[0] or 0):
            if identifier is None:raise ValueError('Retained attempt is no longer visible')
            current=db.execute(text('SELECT status,economic_uid,economic_birth_nonce FROM print_jobs WHERE id=:id'),{'id':identifier}).first()
            if current is None:raise ValueError('Attempt disappeared')
            graph=None
            if current[1]:
                lock_financial_attempt(db,identifier)
                graph=prove_financial_graph(db,identifier)
                if (graph['order_uid'],graph['owner_scope'],graph['owner_key'])!=(uid,*owner):raise ValueError('Wrong order binding')
                assert_content_visible(db,graph=graph,principal=actor)
            else:
                # Cannot safely establish content audience for an unbound physical attempt.
                # Refusal prevents a hidden legacy print from implying a complete total.
                raise ValueError('Unproved historical attempt')
            outcome=current[0] if current[0] in ('completed','failed','cancelled') else 'open'
            item={'attempt_id':identifier,'outcome':outcome,'measurement_basis':'unavailable','amount':None,'currency':None,'reason':'open_attempt' if outcome=='open' else 'missing_terminal_observation'}
            observation=db.execute(text('''SELECT observation_id,attempt_birth,outcome,measurement_basis,snapshot_json,measured_grams,reconciliation_reason
              FROM terminal_filament_measurements WHERE attempt_uid=:uid AND order_uid=:order_uid AND owner_scope=:scope AND owner_key=:owner'''),
              {'uid':graph['attempt_uid'],'order_uid':uid,'scope':owner[0],'owner':owner[1]}).first()
            if observation is not None:
                if (observation[1],observation[2])!=(graph['attempt_birth'],outcome):raise ValueError('Terminal evidence differs')
                event=db.execute(text('''SELECT currency,amount_decimal,reason,spool_uid,purchase_evidence_nonce FROM filament_cost_events WHERE observation_id=:id ORDER BY revision DESC LIMIT 1'''),{'id':observation[0]}).first()
                if event is None or event[2] is not None and event[2] not in REASONS:raise ValueError('Invalid retained event')
                if event[1] is not None:
                    unit=parse_private_valuation(observation[4])
                    if unit['target'] is None or any(unit['target'][k]!=graph[k] for k in ('order_uid','item_uid','job_uid','owner_scope','owner_key')) or (unit['attempt_uid'],unit['attempt_birth'])!=(graph['attempt_uid'],graph['attempt_birth']):raise ValueError('Retained snapshot differs')
                    verify_retained_basis(db,unit)
                    calculated=value_observation(db,graph,observation[4],observation[3],observation[5])
                    if (calculated['amount_decimal'],calculated['currency'],calculated['spool_uid'],calculated['purchase_evidence_nonce'])!=(event[1],event[0],event[3],event[4]):raise ValueError('Retained calculation differs')
                    amount=Decimal(event[1])
                    if not amount.is_finite() or not 0<=amount<=MAX_AMOUNT:raise ValueError('Invalid retained amount')
                item.update(measurement_basis=observation[3],amount=event[1],currency=event[0],reason=event[2] or observation[6])
            attempts.append(item)
        result=summarize(attempts)
        for attempt in result['attempts']:
            if attempt['amount'] is not None:attempt['amount']=display_amount(Decimal(attempt['amount']))
        return result
    except (ValueError,ArithmeticError):
        raise HTTPException(404,'Order measured costs not found',headers={'Cache-Control':'private, no-store'}) from None
