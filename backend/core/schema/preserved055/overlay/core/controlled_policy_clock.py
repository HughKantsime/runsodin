"""Stable mark origins and per-media retention changes, never resource deadlines."""
import json
from datetime import datetime, timedelta, timezone
from sqlalchemy import text
from core.db_compat import as_utc_datetime


def update_clock(db, resource_type, identifier, before, body, *, now=None):
    now = now or datetime.now(timezone.utc)
    values = {'kind': resource_type, 'id': identifier}
    saved = db.execute(text('SELECT * FROM controlled_retention_clocks WHERE resource_type=:kind AND resource_id=:id'), values).mappings().first()
    recent = saved and saved['released_at'] and now - as_utc_datetime(saved['released_at']) <= timedelta(days=30)
    marked = before['marked_at'] if before else saved['marked_at'] if recent else now.isoformat()
    same_clock = saved and saved['marked_at'] == marked
    history = json.loads(saved['changes']) if same_clock else []
    initial = saved['initial_days'] if same_clock else before['retention_days'] if before else body.retention_days
    previous = history[-1]['days'] if history else initial
    if body.controlled and body.retention_days != previous:
        history.append({'previous_days': previous, 'days': body.retention_days, 'changed_at': now.isoformat()})
    db.execute(text("""INSERT INTO controlled_retention_clocks
        (resource_type,resource_id,marked_at,initial_days,changes,released_at)
        VALUES (:kind,:id,:marked,:initial,:changes,:released)
        ON CONFLICT (resource_type,resource_id) DO UPDATE SET
        marked_at=excluded.marked_at,initial_days=excluded.initial_days,
        changes=excluded.changes,released_at=excluded.released_at"""),
        {**values, 'marked': marked, 'initial': initial, 'changes': json.dumps(history),
         'released': None if body.controlled else now.isoformat()})
    return marked


def deadline_sql(kind, key, alias='cp'):
    from core.controlled_sql import inner_alias
    clock = inner_alias('cc', alias)
    predicate = (f" FROM controlled_retention_clocks {clock} WHERE {clock}.resource_type='" + kind +
                 f"' AND {clock}.resource_id=" + alias + '.' + key + f' AND {clock}.marked_at=' + alias + '.marked_at)')
    return ('(SELECT initial_days' + predicate + ' AS retention_initial_days,' +
            '(SELECT changes' + predicate + ' AS retention_changes')


def media_deadline(rule, completed_at):
    completed = as_utc_datetime(completed_at)
    base = max(completed, as_utc_datetime(rule['marked_at']))
    deadline = base + timedelta(days=rule.get('retention_initial_days') or rule['retention_days'])
    ceiling = None
    for change in json.loads(rule.get('retention_changes') or '[]'):
        changed = as_utc_datetime(change['changed_at'])
        proposed = base + timedelta(days=change['days'])
        if completed > changed:
            # Prints completed after this policy change receive the full window.
            deadline, ceiling = proposed, None
        elif change['days'] < change['previous_days']:
            deadline = min(deadline, max(proposed, changed + timedelta(days=7)))
            ceiling = min(ceiling, deadline) if ceiling else deadline
        else:
            # An explicit increase is audited, but cannot undo a prior shortening
            # for media already subject to it (including repeated 30/29 cycles).
            deadline = min(proposed, ceiling) if ceiling else proposed
    return deadline
