"""Print names use scheduled jobs and reader-aware naming on both HTTP reads."""
import pytest
from sqlalchemy import event, text

from tests.test_contracts.test_timelapse_content_audience import (
    capture_education, capture_plain, media_app, owned_runtime, review_db,
)


def same_recording(client, tid):
    listing = client.get('/timelapses')
    detail = client.get(f'/timelapses/{tid}')
    assert listing.status_code == detail.status_code == 200
    assert listing.json()['timelapses'] == [detail.json()]
    return detail


@pytest.mark.parametrize('education', [False, True])
def test_visible_job_display_name_agrees_in_list_and_detail(media_app, review_db, education):
    client, current, _, _ = media_app
    if education:
        tid = capture_education(review_db)
        current['principal'].update(id=1, role='viewer')
        expected = 'Class Gear'
    else:
        tid = capture_plain(review_db, org=1)
        review_db.execute(text("UPDATE jobs SET item_name='plain.3mf' WHERE id=444"))
        review_db.commit()
        expected = 'plain'
    nonce = review_db.execute(text('SELECT ownership_operation_id FROM timelapses WHERE id=:id'), {'id': tid}).scalar_one()
    response = same_recording(client, tid)
    assert response.json()['print_name'] == expected
    assert response.headers['etag'] == '"' + nonce + '"'


@pytest.mark.parametrize('missing_job', [False, True])
def test_no_job_or_deleted_job_has_null_name(media_app, review_db, missing_job):
    client, current, _, _ = media_app
    if missing_job:
        tid = capture_plain(review_db, org=1)
        review_db.execute(text('DELETE FROM jobs WHERE id=444'))
    else:
        tid = 199
        review_db.execute(text("INSERT INTO timelapses(id,printer_id,filename,status) VALUES(199,9,'unknown.mp4','ready')"))
        current['principal'].update(group_id=None)
    review_db.commit()
    response = same_recording(client, tid)
    assert response.json()['print_name'] is None
    if not missing_job:
        assert 'etag' not in response.headers


@pytest.mark.parametrize('education', [False, True])
def test_visible_recording_with_denied_job_name_returns_null(media_app, review_db, monkeypatch, education):
    # Real controlled restrictions hide the whole recording (tested below).
    # Inject only the name denial to exercise the defensive null projection
    # while preserving the actual timelapse audience and Education identity.
    from modules.printers import camera_routes
    client, current, _, _ = media_app
    tid = capture_education(review_db) if education else capture_plain(review_db, org=1)
    if education:
        current['principal'].update(id=1, role='viewer')
    expected_job = 13 if education else 444
    calls = []
    def deny(db, kind, job_id, reader):
        calls.append((kind, job_id, reader['id']))
        return False
    monkeypatch.setattr(camera_routes, 'allowed', deny)
    assert same_recording(client, tid).json()['print_name'] is None
    assert calls == [('job', expected_job, current['principal']['id'])] * 2


@pytest.mark.parametrize('education', [False, True])
def test_real_controlled_job_still_hides_recording_from_nonmembers(media_app, review_db, education):
    from core.controlled_parts import allowed
    client, current, _, _ = media_app
    tid = capture_education(review_db) if education else capture_plain(review_db, org=1)
    job_id = 13 if education else 444
    if education:
        current['principal'].update(id=1, role='viewer')
    review_db.execute(text("INSERT INTO controlled_audiences(id,name) VALUES(1,'Restricted')"))
    review_db.execute(text("INSERT INTO controlled_jobs(job_id,label,audience_id,printer_tag,retention_days,reason,marked_at) VALUES(:job,'CUI',1,'Restricted',30,'test',CURRENT_TIMESTAMP)"), {'job': job_id})
    review_db.commit()
    assert not allowed(review_db, 'job', job_id, current['principal'])
    assert client.get('/timelapses').json()['timelapses'] == []
    assert client.get(f'/timelapses/{tid}').status_code == 404
    review_db.execute(text('INSERT INTO controlled_audience_members(audience_id,user_id) VALUES(1,:user)'), {'user': current['principal']['id']})
    review_db.commit()
    assert same_recording(client, tid).json()['print_name'] == ('Class Gear' if education else 'Old print')


def test_full_page_batches_names_without_per_row_name_queries(media_app, review_db, monkeypatch):
    from modules.printers import camera_routes
    client, current, _, _ = media_app
    current['principal'].update(group_id=None)
    jobs = [{'id': i, 'name': f'Print {i}'} for i in range(1000, 1200)]
    review_db.execute(text("INSERT INTO jobs(id,item_name,model_id,print_file_id,charged_to_org_id,status) VALUES(:id,:name,12,11,1,'completed')"), jobs)
    review_db.execute(text("INSERT INTO timelapses(id,printer_id,print_job_id,filename,status) VALUES(:id,9,:id,:name,'ready')"), jobs)
    review_db.commit()
    calls = []
    original = camera_routes.display_names
    def names(db, kind, ids, user):
        calls.append((kind, list(ids)))
        return original(db, kind, ids, user)
    monkeypatch.setattr(camera_routes, 'display_names', names)
    statements = []
    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    engine = review_db.get_bind()
    event.listen(engine, 'before_cursor_execute', record)
    try:
        assert client.get('/timelapses?limit=1').status_code == 200
        small = list(statements)
        statements.clear()
        calls.clear()
        response = client.get('/timelapses?limit=200')
    finally:
        event.remove(engine, 'before_cursor_execute', record)
    assert response.status_code == 200
    rows = response.json()['timelapses']
    assert len(rows) == response.json()['total'] == 200
    assert all(row['print_name'] == f"Print {row['print_job_id']}" for row in rows)
    assert calls == [('job', [row['print_job_id'] for row in rows])]
    # Existing visible_printer_name performs one query per recording. Apart
    # from those unchanged reads, a full unmarked page costs the same as one row.
    def non_printer_queries(queries):
        return [query for query in queries if not query.startswith('SELECT name,org_id,shared FROM printers')]
    assert len(non_printer_queries(statements)) == len(non_printer_queries(small))
    assert len(non_printer_queries(statements)) < 25
    assert sum('j.item_name AS raw_name' in query for query in statements) == 1


def test_empty_page_does_not_query_job_names(media_app, review_db):
    client, _, _, _ = media_app
    statements = []
    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    engine = review_db.get_bind()
    event.listen(engine, 'before_cursor_execute', record)
    try:
        assert client.get('/timelapses').json()['timelapses'] == []
    finally:
        event.remove(engine, 'before_cursor_execute', record)
    assert not any('j.item_name AS raw_name' in query for query in statements)
