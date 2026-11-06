"""Regression evidence for the final D3 round-one decisions."""
import json
from datetime import date
import pytest
from fastapi import HTTPException
from sqlalchemy import text
from modules.organizations import education_program_reports as reports
from modules.organizations.education_program_records import mark, records, erase_program_records
from modules.organizations.education_program_schemas import ProgramMark, ReportFilters
from tests.test_contracts.test_education_program_reports import program, client, filters
from tests.test_contracts.test_education_classes import setup
from tests.test_contracts.test_education_review_workflow import review_db, _principal


def test_perkins_scope_setting_and_customer_wording(client):
    http, _, db, cls = client
    params = filters().model_dump(mode='json', exclude_none=True)
    assert http.get('/api/education/program-reports', params={**params, 'template':'perkins'}).status_code == 422
    params.update(template='perkins', class_id=cls['id'])
    assert http.get('/api/education/program-reports', params=params).status_code == 403
    assert http.get('/api/education/program-reports/options').json() == {'timezone':'UTC', 'perkins_enabled':False}
    db.execute(text("UPDATE groups SET settings_json=:s WHERE id=1"), {'s':json.dumps({'program_reports_perkins_enabled':True})}); db.commit()
    response = http.get('/api/education/program-reports', params=params)
    assert response.status_code == 200, response.text
    assert 'field definitions not yet confirmed' in response.json()['notice']
    assert 'NEEDS SHANE' not in response.text
    assert 'all-time marks' in response.json()['metric_labels']['participants']
    params['term'] = cls['term']
    assert 'all-time' not in http.get('/api/education/program-reports', params=params).json()['metric_labels']['participants']


@pytest.mark.parametrize('decoded_audit', [False, True])
def test_orphaned_mark_can_be_cleared_but_not_set(program, decoded_audit):
    db, cls, _ = program
    mark(db, _principal(2), cls['id'], 1, ProgramMark(participant=True, concentrator=True))
    db.execute(text("UPDATE education_cost_center_grants SET state='revoked' WHERE user_id=1")); db.commit()
    assert records(db, _principal(2), cls['id'])['marks'][0]['participant']
    from core.errors import OdinError
    with pytest.raises(OdinError):
        mark(db, _principal(2), cls['id'], 1, ProgramMark(participant=True, concentrator=False, revision=1))
    db.rollback()
    mark(db, _principal(2), cls['id'], 1, ProgramMark(participant=False, concentrator=False, revision=1))
    assert records(db, _principal(2), cls['id'])['marks'][0]['participant'] is False
    assert reports.build_report(db, _principal(2), filters())['summary']['participants'] == 0
    raw = db.execute(text("SELECT details FROM audit_logs WHERE action='program.mark'")).scalars().all()
    if decoded_audit:
        # Exercise PostgreSQL's decoded JSON shape even in the SQLite fixture.
        raw = [json.loads(v) if isinstance(v, str) else v for v in raw]
    public = [v if isinstance(v, dict) else json.loads(v) for v in raw]
    assert all('after' not in r and 'before' not in r for r in public)
    assert 'concentrator' in db.execute(text("SELECT details_json FROM education_audit_events WHERE action='program.mark' LIMIT 1")).scalar()


def test_json_reads_no_audit_and_downloads_require_manager(client):
    from core.dependencies import get_current_user
    http, app, db, _ = client
    before = [db.execute(text('SELECT COUNT(*) FROM ' + table)).scalar() for table in ('audit_logs', 'education_audit_events')]
    params = filters().model_dump(mode='json', exclude_none=True)
    assert http.get('/api/education/program-reports', params=params).status_code == 200
    assert before == [db.execute(text('SELECT COUNT(*) FROM ' + table)).scalar() for table in ('audit_logs', 'education_audit_events')]
    # A viewer-role teacher has positive read, record, mark and export authority.
    assert http.get('/api/education/program-reports', params={**params, 'format':'csv'}).status_code == 200
    app.dependency_overrides[get_current_user] = lambda: _principal(1)
    assert http.get('/api/education/program-reports', params={**params, 'format':'csv'}).status_code == 403


def test_school_timezone_dst_and_fallback(program):
    db, _, _ = program
    db.execute(text('UPDATE groups SET settings_json=:s WHERE id=1'), {'s':json.dumps({'timezone':'America/New_York'})})
    one = ReportFilters(start_date=date(2026,11,1), end_date=date(2026,11,1))
    for moment, expected in [('2026-11-01 03:59:59',0), ('2026-11-01 04:00:00',1),
                             ('2026-11-02 04:59:59',1), ('2026-11-02 05:00:00',0)]:
        db.execute(text('UPDATE education_submissions SET created_at=:d'), {'d':moment}); db.commit()
        report = reports.build_report(db, _principal(2), one)
        assert report['summary']['jobs'] == expected
        assert 'America/New_York' in report['basis']['jobs']
    db.execute(text('UPDATE groups SET settings_json=:s WHERE id=1'), {'s':json.dumps({'timezone':'invalid'})}); db.commit()
    assert reports.build_report(db, _principal(2), one)['timezone'] == 'UTC'


def test_failed_then_reprinted_attempts_and_approval_evidence(program):
    db, _, jid = program
    # The final job interval is only 2h; attempts total 3h, including a failed run.
    db.execute(text("UPDATE print_archives SET actual_duration_seconds=5400, "
        "status=CASE WHEN id=(SELECT MIN(id) FROM print_archives) THEN 'failed' ELSE 'completed' END"))
    result = reports.build_report(db, _principal(2), filters())['summary']
    assert result['print_hours'] == 3
    assert result['print_hours_approved_by_instructors'] is None
    db.execute(text("INSERT INTO education_audit_events(event_id,org_id,actor_kind,actor_id,action,command_id,request_hash,resource_type,resource_id,cost_center_id,lifecycle_revision,details_json,result_json) "
        "VALUES('approval',1,'user','2','submission.approve','approval','hash','submission','14',7,2,'{}','{}')"))
    db.execute(text('INSERT INTO print_archives(job_id,actual_duration_seconds) VALUES(:j,NULL)'), {'j':jid}); db.commit()
    result = reports.build_report(db, _principal(2), filters())['summary']
    assert result['print_hours'] == result['print_hours_approved_by_instructors'] == 3
    assert result['unknown_hours_jobs'] == result['unknown_approved_hours_jobs'] == 1
    assert result['jobs'] == result['completed_jobs'] == 1
    assert result['instructor_entered_teaching_hours'] == 0


def test_bounded_65536_limit_and_runtime_override(review_db, monkeypatch):
    query = 'WITH RECURSIVE n(v) AS (SELECT 1 UNION ALL SELECT v+1 FROM n WHERE v<:count) SELECT v FROM n'
    assert len(reports._bounded(review_db, query, {'count':65536})) == 65536
    with pytest.raises(HTTPException) as error:
        reports._bounded(review_db, query, {'count':65537})
    assert error.value.status_code == 413
    monkeypatch.setattr(reports, 'MAX_ROWS', 1)
    with pytest.raises(HTTPException):
        reports._bounded(review_db, query, {'count':2})


def test_exports_suppress_small_cells_without_mutating_json(program):
    from modules.organizations.education_program_rendering import csv_report, pdf_report, email_report
    from pypdf import PdfReader
    from io import BytesIO
    db, _, _ = program
    report = reports.build_report(db, _principal(2), filters())
    assert 'Suppressed (<5)' in csv_report(report).decode('utf-8-sig')
    assert 'Suppressed (&lt;5)' in email_report(report)
    assert 'Suppressed (<5)' in ''.join(p.extract_text() for p in PdfReader(BytesIO(pdf_report(report))).pages)
    assert report['summary']['jobs'] == 1
    # Large cohorts still suppress individual small cells.
    report['summary']['students_served'] = 5
    assert 'Suppressed (<5)' in csv_report(report).decode('utf-8-sig')


def test_job_totals_deduplicated_across_class_join_rows(program, monkeypatch):
    from modules.organizations.education_classes_service import create_class
    from modules.organizations.education_classes_schemas import ClassCreate
    db, cls, _ = program
    second = create_class(db, _principal(2), ClassCreate(cost_center_id=7, title='Robotics', term=cls['term']))
    bounded = reports._bounded
    def duplicate(db, query, params, limit=None):
        rows = bounded(db, query, params, limit)
        if 'AS job_id' in query:
            return [*rows, {**rows[0], 'class_id':second['id']}]
        return rows
    # D2's unique link normally prevents this; protect aggregate semantics if a
    # joined source contains the same proved job under more than one class.
    monkeypatch.setattr(reports, '_bounded', duplicate)
    report = reports.build_report(db, _principal(2), filters())
    assert report['summary']['jobs'] == report['programs'][0]['jobs'] == 1
    assert report['summary']['print_hours'] == 2
    assert sum(c['jobs'] for c in report['classes']) == 2


def test_positive_manager_http_records_and_mark(client):
    http, _, _, cls = client
    base = '/api/education/program-reports/classes/' + cls['id']
    assert http.get(base).status_code == 200
    assert http.put(base + '/marks/1', json={'participant':True,'concentrator':False}).status_code == 200
    assert http.get(base).json()['marks'][0]['participant'] is True
    assert http.put(base + '/instructor-hours', json={'instructor_id':2,'activity_date':'2026-10-01','hours':1}).status_code == 403


def test_school_settings_persist_and_validate(program):
    import asyncio
    from modules.organizations.routes import update_org_settings
    from modules.organizations.education_program_policy import options
    db, _, _ = program
    admin = _principal(3, 'admin')
    for body in ({'timezone':'invalid'}, {'program_reports_perkins_enabled':'false'}):
        with pytest.raises(HTTPException) as error:
            asyncio.run(update_org_settings(1, body, admin, db))
        assert error.value.status_code == 422
    asyncio.run(update_org_settings(1, {'timezone':'America/New_York','program_reports_perkins_enabled':True}, admin, db))
    assert options(db, 1) == {'timezone':'America/New_York','perkins_enabled':True}
