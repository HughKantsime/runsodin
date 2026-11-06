"""System report runner adapter: explicit school, rolling window, live creator."""
from datetime import datetime, timedelta, timezone
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from fastapi import HTTPException
from core.audit import log_audit
from license_manager import require_feature
from modules.organizations.education_access import require_education_license
from modules.organizations.education_program_schemas import ReportFilters
from modules.organizations.education_program_reports import build_report
from modules.organizations.education_program_rendering import email_report
from typing import Literal
from zoneinfo import ZoneInfo
from modules.organizations.education_program_policy import options, template_allowed


class ScheduleFilters(BaseModel):
    model_config = ConfigDict(extra='forbid')
    org_id: int = Field(gt=0)
    days: int = Field(30, ge=1, le=367)
    cost_center_id: int | None = Field(None, gt=0)
    class_id: str | None = Field(None, min_length=1, max_length=36)
    term: str | None = Field(None, min_length=1, max_length=100)
    template: Literal['generic', 'perkins'] = 'generic'


def validate_filters(db, filters, creator):
    require_feature('program_reports')
    require_education_license()
    # Both HTTP callers and background jobs check the live persisted account.
    live = db.execute(text('SELECT id FROM users WHERE id=:u AND role=\'admin\' '
        'AND group_id IS NULL AND is_active IS TRUE'), {'u': creator}).first()
    if not live:
        raise HTTPException(403, 'Program email schedules require an active platform administrator')
    try:
        config = ScheduleFilters.model_validate({k: v for k, v in filters.items() if k != '_program_actor'})
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(422, 'Program schedule requires a school org_id and valid report filters') from None
    if config.template == 'perkins' and not (config.term and config.term.strip() or config.class_id):
        raise HTTPException(422, 'Perkins requires a term or class')
    template_allowed(db, config.org_id, config.template)
    if not db.execute(text('SELECT id FROM groups WHERE id=:o AND is_org IS TRUE'), {'o': config.org_id}).first():
        raise HTTPException(404, 'School organization not found')
    if config.cost_center_id and not db.execute(text('SELECT 1 FROM education_cost_centers WHERE id=:c AND org_id=:o'),
            {'c': config.cost_center_id, 'o': config.org_id}).first():
        raise HTTPException(404, 'Program not found')
    if config.class_id:
        cls = db.execute(text('SELECT cost_center_id,term FROM education_classes WHERE id=:c AND org_id=:o'),
            {'c': config.class_id, 'o': config.org_id}).first()
        if not cls or (config.cost_center_id and cls.cost_center_id != config.cost_center_id) or (config.term and cls.term != config.term):
            raise HTTPException(404, 'Class not found in selected program/term')
    return {**config.model_dump(mode='json'), '_program_actor': creator}


def generate_email(db, filters):
    config = validate_filters(db, filters, filters.get('_program_actor') if isinstance(filters, dict) else None)
    end = datetime.now(ZoneInfo(options(db, config['org_id'])['timezone'])).date() - timedelta(days=1)
    selected = ReportFilters(**{k: config[k] for k in ('cost_center_id', 'class_id', 'term', 'template')},
        start_date=end - timedelta(days=config['days'] - 1), end_date=end)
    result = build_report(db, {'id': config['_program_actor']}, selected, scheduled_org=config['org_id'])
    html = email_report(result)
    log_audit(db, 'program.email_generated', 'program_report', details={'org_id': config['org_id'],
        'authorized_by': config['_program_actor'], 'template': config['template']})
    db.commit()
    return html
