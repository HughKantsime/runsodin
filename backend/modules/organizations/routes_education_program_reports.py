"""D3 licensed HTTP boundary with D2 live manager/tenant authority."""
from datetime import date
from typing import Literal
from fastapi import APIRouter, Depends, Query, HTTPException
from pydantic import ValidationError
from fastapi.responses import Response
from sqlalchemy.orm import Session
from sqlalchemy import text
from modules.organizations.education_classes_service import actor
from modules.organizations.education_program_policy import options
from core.db import get_db
from core.rbac import require_action
from license_manager import feature_gate, require_feature
from modules.organizations.education_access import require_education_principal
from modules.organizations.routes_education_classes import private_response
from modules.organizations.education_program_schemas import ReportFilters, ProgramMark, InstructorHours
from modules.organizations.education_program_reports import build_report
from modules.organizations.education_program_rendering import csv_report, pdf_report
from modules.organizations.education_program_records import records, mark, approve_hours
from modules.organizations.education_program_audit import audit

router = APIRouter(prefix='/education/program-reports', tags=['Education program reports'],
    dependencies=[Depends(feature_gate('program_reports')), Depends(private_response)])
read = require_education_principal()
write = require_education_principal(write=True)


def report_filters(start_date: date, end_date: date,
                   cost_center_id: int | None = Query(None, gt=0),
                   class_id: str | None = Query(None, min_length=1, max_length=36),
                   term: str | None = Query(None, min_length=1, max_length=100),
                   template: Literal['generic', 'perkins'] = 'generic'):
    # Pydantic model validators in Depends are not translated by FastAPI;
    # explicitly convert the cross-field window error at this HTTP boundary.
    try:
        return ReportFilters(start_date=start_date, end_date=end_date, cost_center_id=cost_center_id,
                             class_id=class_id, term=term, template=template)
    except ValidationError:
        raise HTTPException(422, 'Select a valid window of at most 367 days; Perkins requires a term or class') from None


@router.get('', dependencies=[Depends(require_action('education.classes_read'))])
def report(filters: ReportFilters = Depends(report_filters), format: Literal['json', 'csv', 'pdf'] = 'json',
           principal=Depends(read), db: Session = Depends(get_db)):
    if format != 'json':
        org, admin = actor(db, principal)
        if not admin and not db.execute(text("SELECT 1 FROM education_cost_center_grants "
            "WHERE org_id=:o AND user_id=:u AND role='manager' AND state='active'"),
            {'o': org, 'u': principal['id']}).first():
            raise HTTPException(403, 'School manager authority required for downloads')
    if format == 'csv':
        require_feature("csv_export")
    result = build_report(db, principal, filters)
    data = csv_report(result) if format == 'csv' else pdf_report(result) if format == 'pdf' else result
    if format == 'json':
        return data
    audit(db, principal, 'program.export', 'program_report', format, filters.cost_center_id,
        details={'start_date': filters.start_date.isoformat(), 'end_date': filters.end_date.isoformat(),
                 'class_id': filters.class_id, 'term_filtered': bool(filters.term), 'template': filters.template})
    db.commit()
    return Response(data, media_type='text/csv' if format == 'csv' else 'application/pdf',
        headers={'Cache-Control': 'private, no-store', 'X-Content-Type-Options': 'nosniff',
                 'Content-Disposition': 'attachment; filename="odin-program-report.' + format + '"'})


@router.get('/classes/{class_id}', dependencies=[Depends(require_action('education.classes_manage'))])
def class_records(class_id: str, activity_date: date | None = None, principal=Depends(read), db: Session = Depends(get_db)):
    return records(db, principal, class_id, activity_date)


@router.put('/classes/{class_id}/marks/{user_id}', dependencies=[Depends(require_action('education.classes_manage'))])
def save_mark(class_id: str, user_id: int, body: ProgramMark,
              principal=Depends(write), db: Session = Depends(get_db)):
    return mark(db, principal, class_id, user_id, body)


@router.put('/classes/{class_id}/instructor-hours', dependencies=[Depends(require_action('education.classes_manage'))])
def save_hours(class_id: str, body: InstructorHours, principal=Depends(write), db: Session = Depends(get_db)):
    return approve_hours(db, principal, class_id, body)


@router.get('/options', dependencies=[Depends(require_action('education.classes_read'))])
def report_options(principal=Depends(read), db: Session = Depends(get_db)):
    org, _ = actor(db, principal)
    return options(db, org)
