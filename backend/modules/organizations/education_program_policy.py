"""School reporting settings and live delivery authority shared by all sinks."""
import json
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from fastapi import HTTPException
from sqlalchemy import text


def validate_settings(body):
    if 'program_reports_perkins_enabled' in body and not isinstance(body['program_reports_perkins_enabled'], bool):
        raise HTTPException(422, 'Perkins setting must be a boolean')
    if 'timezone' in body:
        try:
            ZoneInfo(body['timezone'])
        except (ZoneInfoNotFoundError, TypeError, ValueError):
            raise HTTPException(422, 'Use an IANA school timezone') from None


def options(db, org):
    raw = db.execute(text('SELECT settings_json FROM groups WHERE id=:o'), {'o': org}).scalar()
    try:
        settings = json.loads(raw or '{}')
        if not isinstance(settings, dict):
            settings = {}
    except (ValueError, TypeError):
        settings = {}
    zone = settings.get('timezone', 'UTC')
    try:
        ZoneInfo(zone)
    except (ZoneInfoNotFoundError, TypeError, ValueError):
        zone = 'UTC'
    return {'timezone': zone, 'perkins_enabled': settings.get('program_reports_perkins_enabled') is True}


def template_allowed(db, org, template):
    if template == 'perkins' and not options(db, org)['perkins_enabled']:
        raise HTTPException(403, 'Perkins worksheet is disabled for this school; field definitions not yet confirmed')


def validate_recipients(db, filters, recipients):
    # An address must resolve only to active school administrators/managers.
    # Do not return an account directory or identify which address was refused.
    if not isinstance(recipients, list) or not recipients:
        raise HTTPException(422, 'Program reports require active school admin or manager recipients')
    org = filters['org_id']
    center = filters.get('cost_center_id')
    if filters.get('class_id'):
        # Resolve again at send time; never trust a cached class/program mapping.
        cls = db.execute(text('SELECT cost_center_id FROM education_classes WHERE id=:c AND org_id=:o'),
                         {'c': filters['class_id'], 'o': org}).scalar()
        if cls is None or (center is not None and center != cls):
            raise HTTPException(422, 'Program report class is outside the selected school/program')
        center = cls
    for address in recipients:
        users = db.execute(text('SELECT id,group_id,is_active,role FROM users WHERE lower(email)=:email'),
                           {'email': address.strip().lower()}).mappings().all()
        if not users or any(not u['is_active'] or u['group_id'] != org for u in users):
            raise HTTPException(422, 'Program reports require active school admin or manager recipients')
        for user in users:
            if user['role'] == 'admin':
                continue
            if center is None or not db.execute(text('SELECT 1 FROM education_cost_center_grants '
                    "WHERE org_id=:o AND cost_center_id=:c AND user_id=:u AND role='manager' AND state='active'"),
                    {'o': org, 'c': center, 'u': user['id']}).first():
                raise HTTPException(422, 'Manager recipients require a selected program or class they manage')


def deactivate_schedules(db, user_id=None):
    """Called after account changes, GDPR erasure and before background delivery.

    Both the original owner and latest editor must remain live platform admins.
    The owner column stays stable when another admin edits the report.
    """
    for row in db.execute(text("SELECT id,created_by,filters FROM report_schedules "
                               "WHERE report_type='program_report' AND is_active IS TRUE")).mappings().all():
        config = json.loads(row['filters'] or '{}')
        owners = (row['created_by'], config.get('_program_actor'))
        invalid = user_id in owners if user_id is not None else False
        for uid in owners:
            if not db.execute(text("SELECT 1 FROM users WHERE id=:u AND role='admin' "
                                   'AND group_id IS NULL AND is_active IS TRUE'), {'u': uid}).first():
                invalid = True
        if invalid:
            db.execute(text('UPDATE report_schedules SET is_active=FALSE WHERE id=:i'), {'i': row['id']})
