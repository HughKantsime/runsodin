"""Recovery protocol for prepared additions; no built-in network transport.

No HTTP endpoint or background worker invokes this module. A future separately
approved Square writer must provide the transport. Injected simulations verify
crash/timeout recovery today. Never supply a production transport without review.
"""
import json
from sqlalchemy import select, update
from modules.orders.batch_production import additions, fail, packed


def deliver(db, owner, addition_id, *, transport, expected_destination):
    # A writer reservation serializes simultaneous delivery of one operation.
    locked=db.execute(update(additions).where(additions.c.id==addition_id,additions.c.owner==owner).values(attempts=additions.c.attempts)).rowcount
    if locked!=1:fail('Stock addition not found')
    row=db.execute(select(additions).where(additions.c.id==addition_id,additions.c.owner==owner)).mappings().one()
    destination=json.loads(row['destination'])
    if destination!=expected_destination:
        db.rollback();fail('Square account changed. Do not retarget this stock addition.')
    if row['state']=='confirmed':
        db.commit();return {'state':'confirmed','attempts':row['attempts']}
    # Persist intent before the external boundary. A crash after this point leaves
    # unknown, never unsent. Every retry uses the exact saved body/idempotency key.
    db.execute(update(additions).where(additions.c.id==addition_id).values(state='unknown',attempts=row['attempts']+1))
    body=row['request_body'];db.commit()
    try:
        confirmed=transport(destination,body) is True
    except Exception:
        confirmed=False  # Never expose remote text or secrets to an outcome record.
    state='confirmed' if confirmed else 'unknown'
    # A concurrent retry can confirm first. An older timeout must not downgrade it.
    db.execute(update(additions).where(additions.c.id==addition_id,additions.c.owner==owner,additions.c.state!='confirmed').values(state=state))
    db.commit()
    actual=db.execute(select(additions.c.state,additions.c.attempts).where(additions.c.id==addition_id)).one()
    return {'state':actual.state,'attempts':actual.attempts}
