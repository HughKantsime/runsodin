"""D3 real-schema accounting, tenancy, formats, marks and privacy contracts."""
from datetime import date, datetime
import json
import pytest
from sqlalchemy import text
from fastapi import FastAPI
from fastapi.testclient import TestClient
from core.errors import OdinError
from modules.organizations import education_program_reports as reports
from modules.organizations.education_program_schemas import ReportFilters, ProgramMark, InstructorHours
from modules.organizations.education_program_records import mark, approve_hours, records, export_program_records, erase_program_records
from tests.test_contracts.test_education_classes import setup, attach
from tests.test_contracts.test_education_review_workflow import review_db, _principal


@pytest.fixture
def program(setup):
    from core.economic_identity import _add_created
    from core.base import JobStatus
    from modules.jobs.models import Job
    db, cls, assignment = setup
    attach(db, assignment)
    job = _add_created(db, Job(item_name='Private student title', model_id=12, print_file_id=11,
        status=JobStatus.COMPLETED, submitted_by=1, charged_to_org_id=1, charged_to_user_id=1,
        estimated_cost=7.25, actual_start=datetime(2026, 10, 1, 10), actual_end=datetime(2026, 10, 1, 12)),
        'job', owner_org_id=1)
    db.execute(text("UPDATE education_submissions SET job_id=:j,created_at='2026-10-01 00:00:00' WHERE id=14"), {'j': job.id})
    db.execute(text("UPDATE education_upload_operations SET final_path='/opaque/upload-1.3mf' WHERE operation_id='upload-1'"))
    db.execute(text('INSERT INTO print_archives(job_id,filament_used_grams,actual_duration_seconds) VALUES(:j,20,5400),(:j,5,1800)'), {'j': job.id})
    db.commit()
    return db, cls, job.id


def filters(**kw):
    return ReportFilters(start_date='2026-10-01', end_date='2026-10-31', **kw)


def test_totals_attempts_and_no_identity_leaks(program):
    db, cls, _ = program
    result = reports.build_report(db, _principal(2), filters(class_id=cls['id']))
    s = result['summary']
    assert (s['jobs'], s['students_served'], s['print_hours'], s['material_grams'], s['cost'], s['success_rate']) == (1, 1, 2, 25, 7.25, 100)
    assert s['unknown_cost_jobs'] == s['excluded_unproved_jobs'] == 0
    assert 'Private student title' not in json.dumps(result) and 'student@example.test' not in json.dumps(result)
    assert result['classes'][0]['jobs'] == 1
    assert 'estimated' in result['basis']['cost']


@pytest.mark.parametrize('scope', ['class', 'program', 'term'])
def test_program_class_term_filters(program, scope):
    db, cls, _ = program
    chosen = {'class_id': cls['id']} if scope == 'class' else {'cost_center_id': 7} if scope == 'program' else {'term': cls['term']}
    assert reports.build_report(db, _principal(2), filters(**chosen))['summary']['jobs'] == 1
    assert reports.build_report(db, _principal(2), filters(term='different'))['summary']['jobs'] == 0


def test_unknown_failed_no_terminal_and_unproved(program):
    db, cls, jid = program
    db.execute(text("UPDATE jobs SET status='failed',actual_start=NULL,estimated_cost=NULL WHERE id=:j"), {'j': jid})
    db.execute(text('UPDATE print_archives SET filament_used_grams=NULL,actual_duration_seconds=NULL'))
    db.commit()
    result = reports.build_report(db, _principal(2), filters())['summary']
    assert result['success_rate'] == 0
    assert all(result[k] is None for k in ('cost', 'material_grams', 'print_hours'))
    assert all(result[k] == 1 for k in ('unknown_cost_jobs', 'unknown_material_jobs', 'unknown_hours_jobs'))
    db.execute(text("UPDATE jobs SET status='pending' WHERE id=:j"), {'j': jid}); db.commit()
    assert reports.build_report(db, _principal(2), filters())['summary']['success_rate'] is None
    db.execute(text("UPDATE education_upload_operations SET final_path='/wrong' WHERE operation_id='upload-1'")); db.commit()
    result = reports.build_report(db, _principal(2), filters())['summary']
    assert result['excluded_unproved_jobs'] == 1 and result['jobs'] == result['students_served'] == 0


def test_date_window_is_inclusive_and_uses_submission_time(program):
    db, cls, _ = program
    one = ReportFilters(start_date='2026-10-01', end_date='2026-10-01')
    assert reports.build_report(db, _principal(2), one)['summary']['jobs'] == 1
    db.execute(text("UPDATE education_submissions SET created_at='2026-10-01 23:59:59'")); db.commit()
    assert reports.build_report(db, _principal(2), one)['summary']['jobs'] == 1
    db.execute(text("UPDATE education_submissions SET created_at='2026-10-02 00:00:00'")); db.commit()
    assert reports.build_report(db, _principal(2), one)['summary']['jobs'] == 0


@pytest.mark.parametrize('uid,role,org', [(1,'viewer',1), (4,'viewer',1), (5,'admin',2)])
def test_scope_refuses_student_outsider_and_foreign(program, uid, role, org):
    db, cls, _ = program
    principal = _principal(uid, role, org)
    with pytest.raises(OdinError):
        reports.build_report(db, principal, filters(class_id=cls['id']))
    assert reports.build_report(db, principal, filters())['classes'] == []
    with pytest.raises(OdinError):
        records(db, principal, cls['id'])


def test_revoked_manager_loses_read_and_write(program):
    db, cls, _ = program
    db.execute(text("UPDATE education_cost_center_grants SET state='revoked' WHERE user_id=2")); db.commit()
    with pytest.raises(OdinError):
        mark(db, _principal(2), cls['id'], 1, ProgramMark(participant=True, concentrator=False))
    db.rollback()
    assert reports.build_report(db, _principal(2), filters())['summary']['jobs'] == 0


def test_class_marks_admin_hours_revisions_and_audit(program):
    db, cls, _ = program
    mark(db, _principal(2), cls['id'], 1, ProgramMark(participant=True, concentrator=True))
    with pytest.raises(OdinError, match='reload'):
        mark(db, _principal(2), cls['id'], 1, ProgramMark(participant=False, concentrator=False))
    db.rollback()
    hours = InstructorHours(instructor_id=2, activity_date='2026-10-02', hours=1.5)
    with pytest.raises(OdinError, match='admin'):
        approve_hours(db, _principal(2), cls['id'], hours)
    approve_hours(db, _principal(3, 'admin'), cls['id'], hours)
    with pytest.raises(OdinError, match='reload'):
        approve_hours(db, _principal(3, 'admin'), cls['id'], hours)
    db.rollback()
    result = reports.build_report(db, _principal(2), filters(template='generic'))
    assert result['summary']['participants'] == result['summary']['concentrators'] == 1
    assert result['summary']['instructor_entered_teaching_hours'] == 1.5
    assert 'NEEDS SHANE' not in result['notice']
    assert db.execute(text("SELECT COUNT(*) FROM education_audit_events WHERE action LIKE 'program.%'")).scalar() == 2
    assert db.execute(text("SELECT COUNT(*) FROM audit_logs WHERE action LIKE 'program.%'")).scalar() == 2
    saved = records(db, _principal(3, 'admin'), cls['id'])
    assert saved['can_approve_hours'] and saved['marks'][0]['participant'] is True
    assert saved['marks'][0]['concentrator'] is True
    receipt = json.loads(db.execute(text("SELECT details_json FROM education_audit_events WHERE action='program.hours_approved'")).scalar())
    assert receipt['before_hours'] is None and receipt['after_hours'] == 1.5


def test_people_deduplicated_across_classes_and_marks_not_print_inferred(program):
    from modules.organizations.education_classes_service import create_class
    from modules.organizations.education_classes_schemas import ClassCreate
    db, cls, _ = program
    second = create_class(db, _principal(2), ClassCreate(cost_center_id=7, title='Robotics', term=cls['term']))
    for cid in (cls['id'], second['id']):
        mark(db, _principal(2), cid, 1, ProgramMark(participant=True, concentrator=True))
    result = reports.build_report(db, _principal(2), filters())
    assert result['summary']['participants'] == result['summary']['concentrators'] == 1
    assert sum(c['participants'] for c in result['classes']) == 2
    assert next(c for c in result['classes'] if c['id'] == second['id'])['jobs'] == 0


def test_privacy_retains_school_counts_but_removes_approver(program):
    db, cls, _ = program
    mark(db, _principal(2), cls['id'], 1, ProgramMark(participant=True, concentrator=True))
    approve_hours(db, _principal(3, 'admin'), cls['id'], InstructorHours(instructor_id=2, activity_date='2026-10-02', hours=2))
    assert len(export_program_records(db, 1)['marks']) == 1
    assert export_program_records(db, 2)['marks'] == []
    assert 'concentrator' not in str(export_program_records(db, 2))
    erase_program_records(db, 2)
    erase_program_records(db, 3)
    db.commit()
    assert db.execute(text('SELECT marked_by FROM education_program_marks')).scalar() is None
    assert db.execute(text('SELECT approved_by FROM education_instructor_hours')).scalar() is None
    assert reports.build_report(db, _principal(3, 'admin'), filters())['summary']['instructor_entered_teaching_hours'] == 2


def test_formats_escape_formulas_html_and_unicode(program):
    from modules.organizations.education_program_rendering import csv_report, pdf_report, email_report
    db, cls, _ = program
    db.execute(text("UPDATE education_classes SET title=:v"), {'v': '=evil<script>🛠'}); db.commit()
    report = reports.build_report(db, _principal(2), filters(template='generic'))
    csv = csv_report(report).decode('utf-8-sig')
    assert "'=evil" in csv and 'Suppressed (<5)' in csv
    assert '<script>' not in email_report(report) and '&lt;script&gt;' in email_report(report)
    from io import BytesIO
    from pypdf import PdfReader
    pdf = pdf_report(report)
    assert pdf.startswith(b'%PDF-')
    pages = PdfReader(BytesIO(pdf)).pages
    content = '\n'.join(page.extract_text() for page in pages)
    assert 'NEEDS SHANE' not in content and 'success rate: Suppressed (<5)' in content
    assert 'material grams: Suppressed (<5)' in content and 'instructor-entered teaching hours' in content
    assert 'Private student title' not in content


def test_report_caps_fail_instead_of_truncating(program, monkeypatch):
    db, _, _ = program
    monkeypatch.setattr(reports, 'MAX_CLASSES', 0)
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as exc:
        reports.build_report(db, _principal(2), filters())
    assert exc.value.status_code == 413


@pytest.fixture
def client(program):
    from core.db import get_db
    from core.dependencies import get_current_user
    from core.app_errors import register_error_handlers
    from modules.organizations.routes_education_program_reports import router
    db, cls, jid = program
    app = FastAPI(); register_error_handlers(app); app.include_router(router, prefix='/api')
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: _principal(2)
    with TestClient(app) as http:
        yield http, app, db, cls


@pytest.mark.parametrize('tier,code', [('community',403), ('pro',403), ('education',200), ('enterprise',200)])
def test_http_license_every_route(client, tier_license, tier, code):
    http, _, _, cls = client
    tier_license(None if tier == 'community' else tier)
    result = http.get('/api/education/program-reports', params=filters().model_dump(mode='json', exclude_none=True))
    assert result.status_code == code, result.text
    if code == 403:
        assert 'program_reports' in result.text
        assert http.get('/api/education/program-reports/classes/' + cls['id']).status_code == 403
        assert http.put(f'/api/education/program-reports/classes/{cls["id"]}/marks/1', json={'participant':True,'concentrator':False}).status_code == 403
        assert http.put(f'/api/education/program-reports/classes/{cls["id"]}/instructor-hours', json={'instructor_id':2,'activity_date':'2026-10-01','hours':1}).status_code == 403
    else:
        assert result.headers['cache-control'] == 'private, no-store'


@pytest.mark.parametrize('format,media', [('csv','text/csv'), ('pdf','application/pdf')])
def test_http_download_and_audit(client, format, media):
    http, _, db, _ = client
    result = http.get('/api/education/program-reports', params={**filters().model_dump(mode='json', exclude_none=True), 'format':format})
    assert result.status_code == 200, result.text
    assert result.headers['content-type'].startswith(media)
    assert 'attachment' in result.headers['content-disposition']
    assert db.execute(text("SELECT COUNT(*) FROM education_audit_events WHERE action='program.export'")).scalar() == 1


def test_http_token_scope_and_live_auth(client):
    from core.dependencies import get_current_user
    http, app, db, cls = client
    app.dependency_overrides[get_current_user] = lambda: {**_principal(2), '_auth_kind':'user_api_token', '_token_scopes':['read:education']}
    assert http.get('/api/education/program-reports', params=filters().model_dump(mode='json', exclude_none=True)).status_code == 200
    assert http.put(f'/api/education/program-reports/classes/{cls["id"]}/marks/1', json={'participant':True,'concentrator':False}).status_code == 403
    app.dependency_overrides[get_current_user] = lambda: {**_principal(2), '_auth_kind':'global_api_key'}
    assert http.get('/api/education/program-reports', params=filters().model_dump(mode='json', exclude_none=True)).status_code == 403


@pytest.mark.parametrize('body', [dict(start_date='2026-11-01',end_date='2026-01-01'),dict(start_date='2020-01-01',end_date='2026-01-01')])
def test_http_invalid_dates_are_422(client, body):
    assert client[0].get('/api/education/program-reports', params=body).status_code == 422


@pytest.mark.parametrize('method,suffix,body', [
    ('GET', '', None), ('GET', '/classes/{c}', None),
    ('PUT', '/classes/{c}/marks/1', {'participant':True,'concentrator':False}),
    ('PUT', '/classes/{c}/instructor-hours', {'instructor_id':2,'activity_date':'2026-10-01','hours':1}),
])
def test_all_d3_routes_obey_permission_matrix_override(client, method, suffix, body):
    http, _, db, cls = client
    db.execute(text("INSERT INTO system_config(key,value) VALUES('rbac_permissions',:v) ON CONFLICT(key) DO UPDATE SET value=excluded.value"),
        {'v':json.dumps({'action_access':{'education.classes_read':['admin'],'education.classes_manage':['admin']}})})
    db.commit()
    response = http.request(method, '/api/education/program-reports' + suffix.format(c=cls['id']),
        params=filters().model_dump(mode='json',exclude_none=True) if suffix == '' else {}, json=body)
    assert response.status_code == 403, response.text
    assert 'Insufficient permissions' in response.text


def test_existing_gdpr_routes_export_and_erase_d3_records(program):
    import asyncio
    from modules.organizations.routes_sessions import export_user_data, erase_user_data
    db, cls, _ = program
    mark(db, _principal(2), cls['id'], 1, ProgramMark(participant=True, concentrator=True))
    approve_hours(db, _principal(3, 'admin'), cls['id'], InstructorHours(instructor_id=2, activity_date='2026-10-02', hours=2))
    exported = asyncio.run(export_user_data(1, _principal(1), db))
    assert exported['education_program_reports']['marks'][0]['concentrator']
    asyncio.run(erase_user_data(2, _principal(3, 'admin'), db))
    assert db.execute(text('SELECT marked_by FROM education_program_marks')).scalar() is None
    assert db.execute(text('SELECT hours FROM education_instructor_hours')).scalar() == 2
    assert not db.execute(text('SELECT is_active FROM users WHERE id=2')).scalar()


def test_archived_classes_preserve_marks_and_exact_date_hour_correction(program):
    db, cls, _ = program
    admin = _principal(3, 'admin')
    db.execute(text("UPDATE education_classes SET state='archived'")); db.commit()
    approve_hours(db, admin, cls['id'], InstructorHours(instructor_id=2, activity_date='2026-09-01', hours=2))
    approve_hours(db, admin, cls['id'], InstructorHours(instructor_id=2, activity_date='2026-10-02', hours=3))
    older = records(db, admin, cls['id'], date(2026, 9, 1))['hours']
    assert len(older) == 1 and older[0]['activity_date'] == '2026-09-01'
    approve_hours(db, admin, cls['id'], InstructorHours(instructor_id=2, activity_date='2026-09-01', hours=0, revision=older[0]['revision']))
    assert reports.build_report(db, admin, filters())['summary']['instructor_entered_teaching_hours'] == 3


def test_program_includes_unassigned_history_but_class_term_do_not(program):
    db, cls, _ = program
    # An unassigned submission existed before D2: program attribution is retained.
    db.execute(text('DELETE FROM education_assignment_submissions WHERE submission_id=14')); db.commit()
    result = reports.build_report(db, _principal(2), filters(cost_center_id=7))
    assert result['summary']['jobs'] == result['summary']['unassigned_jobs'] == 1
    assert result['programs'][0]['print_hours'] == 2
    assert result['classes'][0]['jobs'] == 0
    assert reports.build_report(db, _principal(2), filters(class_id=cls['id']))['summary']['jobs'] == 0
    assert reports.build_report(db, _principal(2), filters(term=cls['term']))['summary']['jobs'] == 0
    # A program does not need any class to report its retained history.
    db.execute(text('DELETE FROM education_assignments')); db.execute(text('DELETE FROM education_classes')); db.commit()
    result = reports.build_report(db, _principal(2), filters(cost_center_id=7))
    assert len(result['classes']) == 1 and result['classes'][0]['title'] == 'Unassigned'
    assert result['classes'][0]['jobs'] == result['programs'][0]['jobs'] == 1
