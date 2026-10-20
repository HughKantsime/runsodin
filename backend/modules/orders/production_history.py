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
    if state == 'in_baseline':
        # Disposition belongs to the receipt, including mixed receipts with an addition.
        if db.get_bind().dialect.name == 'sqlite':
            from sqlalchemy import text
            statement = statement.where(text("EXISTS (SELECT 1 FROM json_each(production_acceptances.definition, '$.outputs') o WHERE json_extract(o.value, '$.disposition') = 'in_baseline')"))
        else:
            from sqlalchemy import text
            statement = statement.where(text("EXISTS (SELECT 1 FROM json_array_elements(CAST(production_acceptances.definition AS JSON)->'outputs') o WHERE o->>'disposition' = 'in_baseline')"))
    elif state:
        statement = statement.where(batches.receipts.c.id.in_(select(batches.additions.c.receipt_id).where(
            batches.additions.c.owner == owner, batches.additions.c.state == state)))
    from modules.orders.finishing import load as load_finishing
    finished=list(load_finishing(db,owner)[0]['finishes'].values())
    if query:finished=[r for r in finished if query.lower() in batches.packed(r).lower()]
    if state == 'in_baseline':
        finished=[r for r in finished if any(o.get('disposition') == 'in_baseline' for o in r['outputs'])]
    elif state:
        matched=set(db.execute(select(batches.additions.c.receipt_id).where(batches.additions.c.owner==owner,batches.additions.c.state==state)).scalars())
        finished=[r for r in finished if r['id'] in matched]
    # At most500 assembly records. Skip only legacy rows guaranteed to precede
    # the requested merged page, keeping every page bounded to limit+501 rows.
    start=max(0,offset-len(finished))
    legacy=db.execute(statement.order_by(timestamp.desc(),batches.receipts.c.id).offset(start).limit(limit+len(finished)+1)).mappings().all()
    combined=[batches.receipt_public(r) for r in legacy]+[dict(r,recipe_id=None) for r in finished]
    # Timestamp descending, stable opaque ID ascending for ties, matching SQL.
    combined.sort(key=lambda r:r['id']);combined.sort(key=lambda r:r['occurred_at'],reverse=True)
    relative=offset-start;rows=combined[relative:relative+limit+1];page=rows[:limit]
    additions = []
    if page:
        additions = [dict(r) for r in db.execute(select(batches.additions.c.id, batches.additions.c.receipt_id,
            batches.additions.c.state, batches.additions.c.attempts).where(batches.additions.c.owner == owner,
                batches.additions.c.receipt_id.in_([r['id'] for r in page]))).mappings()]
    from modules.orders import square_store
    enabled = square_store.writes_enabled(square_store.load(db, owner)[0])
    return {'square_writes_enabled': enabled, 'receipts':page, 'additions':additions,
            'next_offset':offset+limit if len(rows)>limit else None}
