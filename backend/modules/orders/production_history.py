"""Tenant-filtered, bounded inspection history without another stock balance."""
import json
from core.inspection_details import local_count_details
from sqlalchemy import JSON, String, cast, func, select
from modules.orders import batch_production as batches


def history(db, owner, *, offset=0, query='', state=None, limit=25):
    timestamp = (func.json_extract(batches.all_receipts.c.definition, '$.occurred_at')
                 if db.get_bind().dialect.name == 'sqlite'
                 else cast(batches.all_receipts.c.definition, JSON)['occurred_at'].as_string())
    statement = select(batches.all_receipts).where(batches.all_receipts.c.owner == owner)
    if query:
        # Literal substring, not user-supplied SQL or wildcard syntax.
        statement = statement.where(func.lower(batches.all_receipts.c.definition).contains(query.lower(), autoescape=True))
    if state == 'in_baseline':
        # Disposition belongs to the receipt, including mixed receipts with an addition.
        if db.get_bind().dialect.name == 'sqlite':
            from sqlalchemy import text
            statement = statement.where(text("EXISTS (SELECT 1 FROM json_each(inspection_receipts.definition, '$.outputs') o WHERE json_extract(o.value, '$.disposition') = 'in_baseline')"))
        else:
            from sqlalchemy import text
            statement = statement.where(text("EXISTS (SELECT 1 FROM json_array_elements(CAST(inspection_receipts.definition AS JSON)->'outputs') o WHERE o->>'disposition' = 'in_baseline')"))
    elif state:
        statement = statement.where(batches.all_receipts.c.id.in_(select(batches.additions.c.receipt_id).where(
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
    legacy=db.execute(statement.order_by(timestamp.desc(),batches.all_receipts.c.id).offset(start).limit(limit+len(finished)+1)).mappings().all()
    combined=[batches.receipt_public(r) for r in legacy]+[dict(local_count_details(r),recipe_id=None) for r in finished]
    # Timestamp descending, stable opaque ID ascending for ties, matching SQL.
    combined.sort(key=lambda r:r['id']);combined.sort(key=lambda r:r['occurred_at'],reverse=True)
    relative=offset-start;rows=combined[relative:relative+limit+1];page=rows[:limit]
    # A child can be on a different page or excluded by the active search.
    first_ids = [r['id'] for r in page if r.get('job_id') and not r.get('supplement_of')]
    if first_ids:
        from core.models import SystemConfig
        from modules.orders.inspection_supplements import PREFIX
        originals, recipes = batches.receipts, batches.recipes
        status = {rid: (bool(json.loads(definition).get('reference_job_id')), child is not None)
                  for rid, definition, child in db.execute(
                      select(originals.c.id, recipes.c.definition, SystemConfig.key).select_from(originals)
                      .join(recipes, (recipes.c.id == originals.c.recipe_id) & (recipes.c.owner == originals.c.owner))
                      .outerjoin(SystemConfig, SystemConfig.key == PREFIX + originals.c.id)
                      .where(originals.c.id.in_(first_ids), originals.c.owner == owner))}
        for row in page:
            if row['id'] in status:
                has_reference, has_supplement = status[row['id']]
                if has_supplement:
                    row['has_supplement'] = True
                if not has_reference:
                    row['can_supplement'] = False
    additions = []
    if page:
        additions = [dict(r) for r in db.execute(select(batches.additions.c.id, batches.additions.c.receipt_id,
            batches.additions.c.state, batches.additions.c.attempts).where(batches.additions.c.owner == owner,
                batches.additions.c.receipt_id.in_([r['id'] for r in page]))).mappings()]
    from modules.orders import square_store
    enabled = square_store.writes_enabled(square_store.load(db, owner)[0])
    return {'square_writes_enabled': enabled, 'receipts':page, 'additions':additions,
            'next_offset':offset+limit if len(rows)>limit else None}
