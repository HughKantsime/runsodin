"""Round-seven performance, audit lifetime and access-log regressions."""
import logging
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event, text
from tests.test_contracts.test_controlled_parts import database, actor, mark
from tests.test_contracts.test_cert_G_harness import run_probe


LIST_PROBE = r'''
seed_user('reader', 'admin')
reader = login('reader')
for i in range(1,51):
 sql("INSERT INTO printers(id,name,is_active,slot_count,tags,timelapse_enabled,shared) VALUES(:i,:name,1,4,'[]',0,0)", i=i,name='Printer '+str(i))
 sql("INSERT INTO jobs(id,item_name,status,printer_id) VALUES(:i,:name,'pending',:i)", i=i,name='Job '+str(i))
 sql("INSERT INTO timelapses(id,printer_id,print_job_id,filename,status,created_at) VALUES(:i,:i,:i,:name,'ready','2020-01-01')", i=i,name=str(i)+'.mp4')
from sqlalchemy import event
queries=[]
def record(conn,cursor,statement,params,context,many): queries.append(statement)
event.listen(engine,'before_cursor_execute',record)
for route in ('jobs','printers','timelapses'):
 queries.clear()
 response=reader.get('/api/'+route)
 assert response.status_code==200, response.text
 payload=response.json()
 assert len(payload['timelapses'] if route=='timelapses' else payload)==50
 OUT[route]=len(queries)
# Same seed/app on v2.0.44: jobs=57, printers=57, timelapses=54.
# Allow at most three request-wide controlled existence queries per endpoint.
OUT['budgets']={'jobs':60,'printers':60,'timelapses':57}
'''


def test_unmarked_list_statement_budgets(tmp_path):
    result = run_probe('controlled-list-counts', LIST_PROBE, tmp_path)
    for route, budget in result['budgets'].items():
        assert result[route] <= budget, result


def test_deleted_controlled_audit_alone_does_not_keep_read_restrictions(database, tier_license):
    from core.controlled_parts import any_read_restrictions
    from core.controlled_deletion import redact_deleted_job_history
    from modules.organizations.routes_controlled_parts import change_mark
    tier_license('enterprise')
    change_mark(database, 'files', 1, mark(), actor())
    redact_deleted_job_history(database, 1)
    database.execute(text('DELETE FROM jobs WHERE id=1'))
    database.execute(text('DELETE FROM controlled_files')); database.commit()
    assert not any_read_restrictions(database)
    # Even calls naming the deleted id use the request-wide fast path once
    # only the deletion's own controlled audit record remains.
    from core.controlled_parts import policies
    database.info['_controlled_request'] = True
    queries=[]
    def record(conn,cursor,statement,params,context,many): queries.append(statement)
    event.listen(database.get_bind(), 'before_cursor_execute', record)
    try:
        for _ in range(50):
            for kind in ('job', 'printer_names', 'file'):
                assert policies(database, kind, 1) == []
    finally:
        event.remove(database.get_bind(), 'before_cursor_execute', record)
    assert len(queries) == 2
    assert database.execute(text("SELECT count(*) FROM audit_logs WHERE action='controlled_part.job_deleted'")).scalar_one() == 1


def test_reused_job_tombstone_preserves_each_policy(database, tier_license):
    from core.controlled_deletion import redact_deleted_job_history
    from modules.organizations.routes_controlled_parts import change_mark
    tier_license('enterprise')
    change_mark(database, 'files', 1, mark(), actor())
    redact_deleted_job_history(database, 1)
    original = dict(database.execute(text('SELECT * FROM controlled_job_tombstones WHERE job_id=1')).mappings().one())
    database.execute(text('DELETE FROM jobs WHERE id=1')); database.commit()
    database.execute(text("INSERT INTO jobs(id,item_name,print_file_id,status) VALUES(1,'Replacement',1,'pending')"))
    database.commit()
    change_mark(database, 'files', 1, mark(label='Replacement', printer_tag='Other', retention_days=1), actor())
    database.execute(text("UPDATE controlled_files SET marked_at='2021-01-01',purge_previewed_at='2021-02-01'"))
    database.execute(text("UPDATE controlled_retention_clocks SET marked_at='2021-01-01'")); database.commit()
    from core.controlled_parts import policies
    expected = policies(database, 'job', 1)[0]
    redact_deleted_job_history(database, 1)
    rows = database.execute(text('SELECT * FROM controlled_job_tombstones WHERE job_id=1 ORDER BY audit_max_id')).mappings().all()
    assert len(rows) == 2
    assert dict(rows[0]) == original
    actual = rows[1]
    for field, value in expected.items():
        assert actual[field] == value, field


@pytest.mark.parametrize('path', [
    '/api/admin/logs/stream?source=backend&token=private-stream-ack',
    '/api/v1/admin/logs/stream?source=backend&token=private-stream-ack',
    '/ws?token=private-websocket-token&printer=1',
    '/api/overlay/1/camera?token=private-overlay-token&quality=high',
])
def test_uvicorn_stream_handler_redacts_token_urls(path):
    from io import StringIO
    from core.log_redaction import install_log_redaction
    from uvicorn.logging import AccessFormatter
    install_log_redaction()
    output = StringIO()
    handler = logging.StreamHandler(output)
    handler.setFormatter(AccessFormatter('%(client_addr)s - "%(request_line)s" %(status_code)s', use_colors=False))
    logger = logging.getLogger('uvicorn.access')
    logger.addHandler(handler)
    try:
        logger.warning('%s - "%s %s HTTP/%s" %d', '127.0.0.1', 'GET', path, '1.1', 200)
    finally:
        logger.removeHandler(handler)
        handler.close()
    rendered = output.getvalue()
    assert 'private-' not in rendered
    assert 'token=[REDACTED]' in rendered
    assert path.split('?')[0] in rendered
    assert ('source=backend' if 'stream' in path else 'printer=1' if '/ws?' in path else 'quality=high') in rendered


def test_unmarked_notice_has_constant_query_count(database):
    from core.controlled_retention import pending_notice
    for i in range(1,51):
        database.execute(text("INSERT INTO timelapses(id,printer_id,filename,status) VALUES(:i,1,'public.mp4','ready')"), {'i': i})
    database.commit()
    queries=[]
    def record(conn,cursor,statement,params,context,many): queries.append(statement)
    event.listen(database.get_bind(), 'before_cursor_execute', record)
    try:
        assert pending_notice(database) == []
    finally:
        event.remove(database.get_bind(), 'before_cursor_execute', record)
    assert len(queries) == 2
    assert all('WITH RECURSIVE' not in q for q in queries)


@pytest.mark.parametrize('source', ['files', 'jobs', 'tombstone'])
def test_notice_sql_matches_runtime_clocks(database, tier_license, monkeypatch, source):
    from core import controlled_policy_clock as clock
    from core.controlled_deletion import redact_deleted_job_history
    from core.controlled_retention import expired, preview_acknowledged
    from core.controlled_retention_query import pending_count
    from modules.organizations.routes_controlled_parts import change_mark
    from types import SimpleNamespace
    tier_license('enterprise')
    origin = datetime(2020, 1, 1, tzinfo=timezone.utc)
    database.execute(text('UPDATE jobs SET created_at=:created WHERE id=1'), {'created': origin})
    now = origin
    monkeypatch.setattr(clock, 'datetime', SimpleNamespace(now=lambda *a: now))
    for days in (30, 29, 30, 1, 60, 2):
        change_mark(database, 'jobs' if source == 'jobs' else 'files', 1, mark(retention_days=days), actor())
        now += timedelta(days=10)
    for i in range(1,51):
        database.execute(text("INSERT INTO timelapses(id,printer_id,print_job_id,filename,status,completed_at,created_at) VALUES(:i,1,1,'private.mp4','ready',:time,:time)"), {'i': i,'time': origin+timedelta(days=i*2)})
    if source == 'tombstone':
        redact_deleted_job_history(database, 1)
        database.execute(text('DELETE FROM jobs WHERE id=1'))
        database.execute(text('DELETE FROM controlled_files'))
    database.commit()
    for day in (1, 25, 31, 70, 110, 200):
        at = origin + timedelta(days=day)
        expected = sum(expired(database, i, now=at) and not preview_acknowledged(database, i) for i in range(1,51))
        queries=[]
        def record(conn,cursor,statement,params,context,many): queries.append(statement)
        event.listen(database.get_bind(), 'before_cursor_execute', record)
        try:
            assert pending_count(database, now=at) == expected
        finally:
            event.remove(database.get_bind(), 'before_cursor_execute', record)
        assert len(queries) == 1


def test_notice_captured_alias_and_mixed_acknowledgements(database, tier_license):
    from core.controlled_retention import expired, preview_acknowledged
    from core.controlled_retention_query import pending_count
    from modules.organizations.routes_controlled_parts import change_mark
    tier_license('enterprise')
    database.execute(text("UPDATE print_files SET stored_path='/same.3mf' WHERE id=1"))
    database.execute(text("INSERT INTO print_files(id,filename,stored_path) VALUES(3,'alias.3mf','/same.3mf')"))
    change_mark(database, 'files', 1, mark(retention_days=30), actor())
    database.execute(text('UPDATE jobs SET charged_to_org_id=1 WHERE id=2'))
    change_mark(database, 'jobs', 2, mark(retention_days=1), actor())
    database.execute(text("UPDATE controlled_files SET marked_at='2020-01-01',purge_previewed_at=NULL"))
    database.execute(text("UPDATE controlled_jobs SET marked_at='2020-01-01',purge_previewed_at='2020-01-01'"))
    database.execute(text("""INSERT INTO timelapse_ownership_anchors
        (operation_id,timelapse_id,printer_id,scheduled_job_id,filename,owner_state,
         ownership_source,print_file_id,capture_printer_shared)
        VALUES ('captured',1,1,2,'captured.mp4','unresolved','capture_unresolved',3,FALSE)"""))
    database.execute(text("""INSERT INTO timelapses
        (id,printer_id,print_job_id,ownership_operation_id,filename,status,created_at)
        VALUES (1,1,2,'captured','captured.mp4','failed','2020-01-01')"""))
    database.commit()
    now = datetime(2020, 1, 3, tzinfo=timezone.utc)
    assert expired(database, 1, now=now)
    assert not preview_acknowledged(database, 1)
    # An acknowledged due job rule + an unacknowledged, not-yet-due captured
    # file rule must still produce exactly one notice for this media item.
    assert pending_count(database, now=now) == 1
    database.execute(text("UPDATE controlled_files SET purge_previewed_at='2020-01-01'"))
    assert pending_count(database, now=now) == 0
