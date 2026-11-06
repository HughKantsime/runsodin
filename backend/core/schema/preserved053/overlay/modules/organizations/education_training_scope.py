"""School-wide and shared-printer training scope, without write side effects."""
from modules.organizations.education_training_policy import _adapter


def scope_matches(training, printer):
    kind = training['scope_kind']
    return kind == 'printer_group' or (printer[kind] or '').strip().casefold() == training['scope_value'].strip().casefold()


def scope_printers(db, training):
    return _adapter(db).fetchall(
        'SELECT p.id,p.name,p.machine_type,p.model FROM printers p '
        'WHERE p.org_id=:org AND p.is_active IS TRUE AND p.shared IS NOT TRUE '
        "AND (:school=1 OR EXISTS (SELECT 1 FROM education_cost_center_printers e "
        "JOIN education_cost_centers c ON c.id=e.cost_center_id AND c.org_id=e.org_id "
        "WHERE e.printer_id=p.id AND e.org_id=p.org_id AND e.cost_center_id=:center "
        "AND e.state='active' AND c.state='active')) ORDER BY p.id",
        {'org': training['org_id'], 'center': training['cost_center_id'], 'school': int(training['school_wide'])})


def matched_printers(db, training):
    return [p for p in scope_printers(db, training) if scope_matches(training, p)]


def applicable_trainings(db, org_id, printer_id):
    return _adapter(db).fetchall(
        'SELECT t.* FROM education_trainings t WHERE t.org_id=:org AND t.active IS TRUE '
        "AND (t.school_wide IS TRUE OR EXISTS (SELECT 1 FROM education_cost_center_printers e "
        "JOIN education_cost_centers c ON c.id=e.cost_center_id AND c.org_id=e.org_id "
        "WHERE e.cost_center_id=t.cost_center_id AND e.org_id=t.org_id AND e.printer_id=:printer "
        "AND e.state='active' AND c.state='active')) ORDER BY t.id",
        {'org': org_id, 'printer': printer_id})


def student_in_scope(db, training, user_id, *, active=True):
    # A student in a different class sharing a required printer must be able to
    # see and complete that requirement. The owning class still administers it.
    a = _adapter(db)
    grants = a.fetchall(
        "SELECT g.cost_center_id,g.state,u.is_active FROM education_cost_center_grants g "
        "JOIN users u ON u.id=g.user_id AND u.group_id=g.org_id "
        "JOIN education_cost_centers c ON c.id=g.cost_center_id AND c.org_id=g.org_id "
        "WHERE g.user_id=:user AND g.org_id=:org AND g.role='student' AND c.state='active'",
        {'user': user_id, 'org': training['org_id']})
    printers = {p['id'] for p in matched_printers(db, training)}
    for grant in grants:
        if active and (grant['state'] != 'active' or not grant['is_active']):
            continue
        if training['school_wide'] or grant['cost_center_id'] == training['cost_center_id']:
            return True
        class_printers = a.fetchall(
            "SELECT printer_id FROM education_cost_center_printers WHERE org_id=:org "
            "AND cost_center_id=:center AND state='active'",
            {'org': training['org_id'], 'center': grant['cost_center_id']})
        if printers.intersection(p['printer_id'] for p in class_printers):
            return True
    return False


def printer_edit_warnings(db, printer, changes):
    """Warn the editor whenever an active requirement loses this printer."""
    if not {'machine_type', 'model', 'is_active', 'shared', 'org_id'}.intersection(changes):
        return []
    from sqlalchemy import inspect
    if not inspect(db.connection()).has_table('education_trainings'):
        return []
    old = {key: getattr(printer, key) for key in ('machine_type', 'model', 'is_active', 'shared', 'org_id')}
    new = {**old, **changes}
    warnings = []
    for training in applicable_trainings(db, printer.org_id, printer.id):
        if not old['is_active'] or old['shared'] or not scope_matches(training, old):
            continue
        if not new['is_active'] or new['shared'] or new['org_id'] != old['org_id'] or not scope_matches(training, new):
            warnings.append(f"Printer no longer matches training: {training['name']}. Review its training scope.")
    return warnings


def scope_loss_warnings(db, *, org_id, removed_printer_ids, center_id=None):
    """Warn only about a nonempty scope becoming empty in this mutation."""
    from sqlalchemy import inspect
    if not removed_printer_ids or not inspect(db.connection()).has_table('education_trainings'):
        return []
    warnings = []
    rows = _adapter(db).fetchall('SELECT * FROM education_trainings WHERE org_id=:org AND active IS TRUE', {'org': org_id})
    for training in rows:
        # Removing a class entitlement does not remove a school-wide printer.
        if center_id is not None and (training['school_wide'] or training['cost_center_id'] != center_id):
            continue
        matches = {p['id'] for p in matched_printers(db, training)}
        if matches and not matches.difference(removed_printer_ids):
            warnings.append(f"Training no longer matches any printer: {training['name']}. Review its training scope.")
    return warnings
