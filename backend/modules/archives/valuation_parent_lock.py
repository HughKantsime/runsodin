"""Stable parent-first attempt binding; caller must verify reader beforehand."""
from sqlalchemy import text
from core.order_production import prepare_parent_graph,recheck_parent_graph


def lock_financial_attempt(db,attempt_id):
    if type(attempt_id) is not int or attempt_id<=0:
        raise ValueError('Invalid valuation attempt')
    before=db.execute(text('SELECT scheduled_job_id,economic_birth_nonce,economic_uid FROM print_jobs WHERE id=:id'),{'id':attempt_id}).first()
    if before is None or type(before[0]) is not int or before[0]<=0 or not before[1] or not before[2]:
        raise ValueError('Unproved valuation attempt')
    parents=prepare_parent_graph(db,[before[0]])
    postgres=db.dialect.name=='postgresql' if hasattr(db,'dialect') else db.get_bind().dialect.name=='postgresql'
    for table,identifier in [('jobs',before[0]),('print_jobs',attempt_id)]:
        sql=f'SELECT id FROM {table} WHERE id=:id FOR UPDATE NOWAIT' if postgres else f'UPDATE {table} SET id=id WHERE id=:id RETURNING id'
        if db.execute(text(sql),{'id':identifier}).first() is None:
            raise ValueError('Valuation parent disappeared')
    after=db.execute(text('SELECT scheduled_job_id,economic_birth_nonce,economic_uid FROM print_jobs WHERE id=:id'),{'id':attempt_id}).first()
    if after is None or tuple(after)!=tuple(before):
        raise ValueError('Valuation attempt binding changed')
    recheck_parent_graph(db,parents)
    return tuple(after)
