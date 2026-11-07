"""Deleted-job rules use durable job identities, independent of media timestamps."""
from sqlalchemy import text
from core.controlled_sql import inner_alias


def timestamp_seconds(value, pg):
    if pg:
        return f'CAST(EXTRACT(EPOCH FROM CAST({value} AS TIMESTAMPTZ)) AS DOUBLE PRECISION)'
    # Julian-day subtraction loses microseconds, moving exact deadlines
    # past Python's <= boundary. Parse whole seconds and the stored ISO
    # fraction separately; SQLite's date functions round fractions to ms.
    zone = (f"CASE WHEN length({value})>=25 AND substr({value},-3,1)=':' "
            f"AND substr({value},-6,1) IN ('+','-') "
            f"THEN substr({value},-6) ELSE '' END")
    fraction = (f"CASE WHEN substr({value},20,1)='.' "
                f"THEN CAST('0.' || substr({value},21) AS DOUBLE PRECISION) ELSE 0 END")
    return f"(CAST(strftime('%s',substr({value},1,19) || {zone}) AS INTEGER) + {fraction})"



def tombstone_policies(db, job_id):
    return [dict(row) for row in db.execute(text(
        'SELECT label,audience_id,printer_tag,retention_days,marked_at,purge_previewed_at,'
        'retention_initial_days,retention_changes FROM controlled_job_tombstones '
        'WHERE job_id=:id'), {'id': job_id}).mappings()]


def media_visibility(actor, job_id):
    from core.controlled_parts import _audience, job_visibility
    history, values = _audience(actor, 'SELECT audience_id FROM controlled_job_tombstones cp '
                               'WHERE cp.job_id=' + job_id)
    jobs, _ = job_visibility(actor, 'j')
    live = ('NOT EXISTS (SELECT 1 FROM jobs j WHERE j.id=' + job_id + ' AND NOT (' + jobs + '))')
    return '(' + history + ') AND ' + live, values


def retained_media_sql(alias):
    tl, pj, pa = (inner_alias(name, alias) for name in ('tl', 'pj', 'pa'))
    return (f'EXISTS (SELECT 1 FROM timelapses {tl} WHERE {tl}.print_job_id={alias}.job_id) OR '
            f'EXISTS (SELECT 1 FROM print_jobs {pj} WHERE {pj}.scheduled_job_id={alias}.job_id) OR '
            f'EXISTS (SELECT 1 FROM print_archives {pa} WHERE {pa}.job_id={alias}.job_id)')
