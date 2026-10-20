"""Tenant-filtered, bounded inspection history without another stock balance."""
from sqlalchemy import JSON, String, cast, func, select
from modules.orders import batch_production as batches


def history(db, owner, *, offset=0, query='', state=None, limit=25):
    timestamp = (func.json_extract(batches.receipts.c.definition, '$.occurred_at')
                 if db.get_bind().dialect.name == 'sqlite'
                 else cast(batches.receipts.c.definition, JSON)['occurred_at'].as_string())
    statement = select(batches.receipts).where(batches.receipts.c.owner == owner)
    if query:
        # Literal substring, not user-supplied SQL or wildcard syntax.
        statement = statement.where(func.lower(batches.receipts.c.definition).contains(query.lower(), autoescape=True))
    if state:
        statement = statement.where(batches.receipts.c.id.in_(select(batches.additions.c.receipt_id).where(
            batches.additions.c.owner == owner, batches.additions.c.state == state)))
    rows = db.execute(statement.order_by(timestamp.desc(), batches.receipts.c.id).offset(offset).limit(limit+1)).mappings().all()
    page = rows[:limit]
    additions = []
    if page:
        additions = [dict(r) for r in db.execute(select(batches.additions.c.id, batches.additions.c.receipt_id,
            batches.additions.c.state, batches.additions.c.attempts).where(batches.additions.c.owner == owner,
                batches.additions.c.receipt_id.in_([r['id'] for r in page]))).mappings()]
    return {'receipts':[batches.receipt_public(r) for r in page], 'additions':additions,
            'next_offset':offset+limit if len(rows)>limit else None}
