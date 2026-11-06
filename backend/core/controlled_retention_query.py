"""Set-based notice calculation, sharing the per-media retention clock semantics."""
from datetime import datetime, timezone
from sqlalchemy import text
from core.controlled_parts import selected_file_sql
from core.controlled_policy_clock import deadline_sql
from core.controlled_history import timestamp_seconds


def pending_count(db, *, now=None):
    pg = db.get_bind().dialect.name == 'postgresql'
    seconds = lambda value: timestamp_seconds(value, pg)
    low, high = ('LEAST', 'GREATEST') if pg else ('min', 'max')

    def change(field):
        return (f"(CAST(changes AS JSONB)->step->>'{field}')" if pg
                else f"json_extract(changes, '$[' || step || '].{field}')")

    length = 'jsonb_array_length(CAST(changes AS JSONB))' if pg else 'json_array_length(changes)'
    changed = seconds(change('changed_at'))
    days = f"CAST({change('days')} AS INTEGER)"
    previous = f"CAST({change('previous_days')} AS INTEGER)"
    proposed = f'(base + {days} * 86400.0)'
    shortened = f'{low}(deadline, {high}({proposed}, {changed} + 604800.0))'
    deadline = (f'CASE WHEN completed > {changed} THEN {proposed} '
                f'WHEN {days} < {previous} THEN {shortened} '
                f'ELSE {low}({proposed}, COALESCE(ceiling, {proposed})) END')
    ceiling = (f'CASE WHEN completed > {changed} THEN CAST(NULL AS DOUBLE PRECISION) '
               f'WHEN {days} < {previous} THEN {low}(COALESCE(ceiling, {shortened}), {shortened}) '
               f'ELSE ceiling END')
    fields = 'cp.retention_days,cp.marked_at,cp.purge_previewed_at,'
    # Resolve live selected files and immutable captured files. Shared storage
    # aliases inherit the same restrictions as policies(), including job marks.
    query = f"""WITH RECURSIVE
        file_rules AS (
            SELECT candidate.id AS file_id,{fields}{deadline_sql('files', 'file_id')}
            FROM controlled_files cp JOIN print_files marked ON marked.id=cp.file_id
            JOIN print_files candidate ON candidate.id=marked.id OR
                (marked.stored_path IS NOT NULL AND candidate.stored_path=marked.stored_path)
            UNION ALL
            SELECT candidate.id,{fields}{deadline_sql('jobs', 'job_id')}
            FROM controlled_jobs cp JOIN print_files marked ON marked.id=({selected_file_sql('cp.job_id')})
            JOIN print_files candidate ON candidate.id=marked.id OR
                (marked.stored_path IS NOT NULL AND candidate.stored_path=marked.stored_path)
        ), media AS (
            SELECT t.id,t.print_job_id,COALESCE(t.completed_at,t.created_at) AS completed_at,
                ({selected_file_sql('t.print_job_id')}) AS selected_file_id,a.print_file_id AS captured_file_id
            FROM timelapses t LEFT JOIN timelapse_ownership_anchors a ON a.operation_id=t.ownership_operation_id
            WHERE t.status IN ('ready','failed','deleting')
        ), rules AS (
            SELECT m.id,m.completed_at,{fields}{deadline_sql('jobs', 'job_id')}
            FROM media m JOIN controlled_jobs cp ON cp.job_id=m.print_job_id
            UNION ALL
            SELECT m.id,m.completed_at,{fields}cp.retention_initial_days,cp.retention_changes
            FROM media m JOIN file_rules cp ON cp.file_id=m.selected_file_id OR cp.file_id=m.captured_file_id
            UNION ALL
            SELECT m.id,m.completed_at,{fields}cp.retention_initial_days,cp.retention_changes
            FROM media m JOIN controlled_job_tombstones cp ON cp.job_id=m.print_job_id
        ), origins AS (
            SELECT id,purge_previewed_at,{seconds('completed_at')} AS completed,
                {high}({seconds('completed_at')},{seconds('marked_at')}) AS base,
                COALESCE(retention_initial_days,retention_days) AS initial_days,
                COALESCE(retention_changes,'[]') AS changes FROM rules WHERE completed_at IS NOT NULL
        ), clocks(id,purge_previewed_at,completed,base,changes,step,deadline,ceiling) AS (
            SELECT id,purge_previewed_at,completed,base,changes,0,
                base + initial_days * 86400.0,CAST(NULL AS DOUBLE PRECISION) FROM origins
            UNION ALL
            SELECT id,purge_previewed_at,completed,base,changes,step+1,{deadline},{ceiling}
            FROM clocks WHERE step < {length}
        ) SELECT count(*) FROM (
            SELECT id FROM clocks WHERE step={length} GROUP BY id
            HAVING min(deadline)<=:now AND max(CASE WHEN purge_previewed_at IS NULL THEN 1 ELSE 0 END)=1
        ) due"""
    return db.execute(text(query), {'now': (now or datetime.now(timezone.utc)).timestamp()}).scalar_one()
