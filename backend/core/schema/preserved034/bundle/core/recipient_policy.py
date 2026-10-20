"""Resolve alert resource scope before any recipient, event or channel delivery."""
from sqlalchemy import text
from core.rbac import check_org_access


def resolve_alert_scope(db, *, printer_id=None, job_id=None, spool_id=None):
    references = [('printers', 'org_id', printer_id),
                  ('jobs', 'charged_to_org_id', job_id), ('spools', 'org_id', spool_id)]
    owners = []
    for table, column, identity in references:
        if identity is None:
            continue
        # Table/column identifiers come exclusively from the fixed list above.
        row = db.execute(text(f'SELECT {column} AS org_id FROM {table} WHERE id=:id'),  # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- table/column iterate the literal references list in resolve_alert_scope (closed set); values are bound
                         {'id': identity}).mappings().first()
        if row is None:
            raise ValueError('Alert resource not found')
        owners.append(row['org_id'])
    tenants = {owner for owner in owners if owner is not None}
    if len(tenants) > 1:
        raise ValueError('Conflicting alert resource owners')
    users = db.execute(text('SELECT id,role,group_id FROM users WHERE is_active IS TRUE')).mappings()
    eligible = {u['id'] for u in users if all(check_org_access(dict(u), owner) for owner in owners)}
    return eligible, next(iter(tenants), None)
