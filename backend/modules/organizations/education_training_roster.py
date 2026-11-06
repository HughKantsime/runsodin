"""Batch roster projection, including retained records and shared-printer students."""
from sqlalchemy import text
from modules.organizations.education_training_policy import record_status
from modules.organizations.education_training_scope import matched_printers


def roster_items(db, training):
    params = {'org': training['org_id'], 'id': training['id']}
    records = {r['user_id']: dict(r) for r in db.execute(text(
        'SELECT * FROM education_training_records WHERE org_id=:org AND training_id=:id'
    ), params).mappings()}
    printers = {p['id'] for p in matched_printers(db, training)}
    grants = db.execute(text(
        'SELECT g.user_id,g.cost_center_id,g.state,e.printer_id FROM education_cost_center_grants g '
        'JOIN education_cost_centers c ON c.id=g.cost_center_id AND c.org_id=g.org_id '
        "LEFT JOIN education_cost_center_printers e ON e.cost_center_id=c.id AND e.org_id=c.org_id AND e.state='active' "
        "WHERE g.org_id=:org AND g.role='student' AND c.state='active'"
    ), params).mappings()
    eligible, active = set(), set()
    for grant in grants:
        if (training['school_wide'] or grant['cost_center_id'] == training['cost_center_id']
                or grant['printer_id'] in printers):
            eligible.add(grant['user_id'])
            if grant['state'] == 'active':
                active.add(grant['user_id'])
    users = db.execute(text('SELECT id,username,is_active FROM users WHERE group_id=:org ORDER BY username,id'), params)
    return [{'user_id': u.id, 'username': u.username, 'active': bool(u.is_active and u.id in active),
             'status': record_status(training, records.get(u.id)), 'record': records.get(u.id)}
            for u in users if u.id in eligible or u.id in records]
