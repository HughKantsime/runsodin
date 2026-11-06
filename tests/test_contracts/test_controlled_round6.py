"""Round-six regressions for legacy reads, long streams and deleted-job policy."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import jwt
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import event, text
from tests.test_contracts.test_controlled_parts import database, actor, mark
from tests.test_contracts.test_controlled_guards import marked


def test_stream_checks_expiry_only_at_open(marked, monkeypatch, tmp_path):
    from core.auth import SECRET_KEY
    from core import controlled_logs
    from modules.system import routes_admin
    token = controlled_logs.issue_stream_token(actor(2), True)
    assert controlled_logs.stream_ack(token, actor(2))
    expired = jwt.encode({'sub': '2', 'purpose': 'controlled_log_stream', 'ack': True,
                          'exp': datetime.now(timezone.utc) - timedelta(seconds=1)}, SECRET_KEY, algorithm='HS256')
    with pytest.raises(HTTPException):
        controlled_logs.stream_ack(expired, actor(2))
    _stream_lines(marked, monkeypatch, tmp_path, token)


def test_unmarked_stream_without_token_then_new_mark_stops_it(database, monkeypatch, tmp_path, tier_license):
    tier_license('enterprise')
    _stream_lines(database, monkeypatch, tmp_path, '', mark_during_stream=True)


def _stream_lines(db, monkeypatch, tmp_path, token, mark_during_stream=False):
    from core import controlled_logs
    from modules.system import routes_admin
    path = tmp_path / 'stream.log'
    path.write_text('')
    monkeypatch.setattr(routes_admin, '_LOG_FILES', {'backend': str(path)})
    async def connected():
        with path.open('a') as file:
            file.write('[INFO] streamed line\n')
        return False
    async def consume():
        response = await routes_admin.stream_logs(SimpleNamespace(is_disconnected=connected),
            source='backend', level=None, search=None, user=actor(2), db=db, token=token)
        # A running stream must never decode its now-expired open-only token.
        monkeypatch.setattr(controlled_logs, 'stream_ack', lambda *a: pytest.fail('Token revalidated after open'))
        assert 'streamed line' in await anext(response.body_iterator)
        assert 'streamed line' in await anext(response.body_iterator)
        if mark_during_stream:
            from modules.organizations.routes_controlled_parts import change_mark
            change_mark(db, 'files', 1, mark(), actor())
        else:
            db.execute(text("UPDATE users SET role='viewer' WHERE id=2")); db.commit()
        with pytest.raises(HTTPException):
            await anext(response.body_iterator)
        await response.body_iterator.aclose()
    asyncio.run(consume())


@pytest.mark.parametrize('controlled', [False, True])
def test_legacy_gets_and_post_ack(database, tier_license, monkeypatch, tmp_path, controlled):
    from core.db import get_db
    from core.dependencies import get_current_user
    from modules.system import routes_admin, diagnostics
    from modules.organizations.routes_controlled_parts import change_mark
    tier_license('enterprise')
    if controlled:
        change_mark(database, 'files', 1, mark(), actor())
    path = tmp_path / 'read.log'; path.write_text('[INFO] sample log\n')
    monkeypatch.setattr(routes_admin, '_LOG_FILES', {'backend': str(path)})
    monkeypatch.setattr(diagnostics, '_read_tail', lambda *a: ['sample log'])
    app = FastAPI()
    app.include_router(routes_admin.router, prefix='/api')
    app.include_router(diagnostics.router, prefix='/api')
    app.dependency_overrides[get_db] = lambda: database
    app.dependency_overrides[get_current_user] = lambda: actor(2)
    with TestClient(app) as client:
        for route, field in (('/api/admin/logs', 'lines'), ('/api/system/diagnostics', 'recent_logs')):
            get = client.get(route + '?acknowledge_controlled=true', headers={'X-ODIN-Controlled-Ack': 'true'})
            assert get.status_code == 200, get.text
            if controlled:
                assert 'sample log' not in get.text
                assert 'POST ' + route in get.text
                assert 'controlled_part_acknowledgement_required' in get.text
            else:
                assert 'sample log' in str(get.json()[field])
            post = client.post(route, headers={'X-ODIN-Controlled-Ack': 'true'})
            assert post.status_code == 200, post.text
            assert 'sample log' in str(post.json()[field])


@pytest.mark.parametrize('ack_before', [False, True])
def test_deleted_media_clock_and_acknowledgement(database, tier_license, monkeypatch, ack_before):
    from core import controlled_policy_clock as clock
    from core.controlled_parts import policies
    from core.controlled_deletion import redact_deleted_job_history
    from core.controlled_retention import expired, preview_acknowledged, pending_notice
    from modules.organizations.routes_controlled_parts import change_mark
    from modules.organizations.routes_controlled_retention import preview, acknowledge, PurgeAcknowledgement
    tier_license('enterprise')
    origin = datetime(2020, 1, 1, tzinfo=timezone.utc)
    database.execute(text('UPDATE jobs SET created_at=:created WHERE id=1'), {'created': origin})
    now = origin
    monkeypatch.setattr(clock, 'datetime', SimpleNamespace(now=lambda *a: now))
    change_mark(database, 'files', 1, mark(retention_days=30), actor())
    now += timedelta(days=10)
    change_mark(database, 'files', 1, mark(retention_days=1), actor())
    if ack_before:
        acknowledge(PurgeAcknowledgement(files=[1]), actor(), database)
    original = policies(database, 'job', 1)[0]
    database.execute(text("INSERT INTO timelapses(id,printer_id,print_job_id,filename,status,completed_at,created_at) VALUES(1,1,1,'old.mp4','ready',:time,:time)"), {'time': origin})
    redact_deleted_job_history(database, 1)
    database.execute(text('DELETE FROM jobs WHERE id=1'))
    database.execute(text('DELETE FROM controlled_files')); database.commit()
    rule = policies(database, 'job', 1)[0]
    for field in ('purge_previewed_at', 'retention_initial_days', 'retention_changes'):
        assert rule[field] == original[field]
    assert clock.media_deadline(rule, origin) == origin + timedelta(days=17)
    assert expired(database, 1)
    assert preview_acknowledged(database, 1) == ack_before
    assert bool(pending_notice(database)) != ack_before
    if not ack_before:
        snapshots = [{'job_id': 1, 'audit_max_id': database.execute(text('SELECT audit_max_id FROM controlled_job_tombstones')).scalar_one()}]
        assert preview(actor(), database)['tombstones'] == snapshots
        assert preview(actor(2), database)['tombstones'] == []
        with pytest.raises(HTTPException):
            acknowledge(PurgeAcknowledgement(tombstones=snapshots), actor(2), database)
        database.rollback()
        acknowledge(PurgeAcknowledgement(tombstones=snapshots), actor(), database)
    assert preview_acknowledged(database, 1)
    assert pending_notice(database) == []
    assert preview(actor(), database)['tombstones'] == []


@pytest.mark.parametrize('late_job_id', [1, 2])
def test_history_fast_path_only_for_relevant_audits_and_request_cached(marked, late_job_id):
    from core.controlled_parts import any_read_restrictions
    from core.controlled_deletion import redact_deleted_job_history
    from core.audit import log_audit
    log_audit(marked, 'job.updated', 'job', 1, {'name': 'Controlled original'})
    marked.commit()
    redact_deleted_job_history(marked, 1)
    marked.execute(text('DELETE FROM jobs WHERE id=1'))
    marked.execute(text('DELETE FROM controlled_files')); marked.commit()
    assert any_read_restrictions(marked)
    bound = marked.execute(text("SELECT audit_max_id FROM controlled_job_tombstones")).scalar_one()
    # A late row for this durable ID remains relevant; another job does not.
    log_audit(marked, 'job.updated', 'job', late_job_id, {'name': 'Late observation'}); marked.commit()
    marked.execute(text("DELETE FROM audit_logs WHERE entity_type='job' AND entity_id=1 AND id<=:bound AND action NOT LIKE 'controlled_part.%'"), {'bound': bound}); marked.commit()
    marked.info.update(_controlled_request=True)
    queries = []
    def record(conn, cursor, statement, params, context, many):
        queries.append(statement)
    event.listen(marked.get_bind(), 'before_cursor_execute', record)
    try:
        for _ in range(10):
            assert any_read_restrictions(marked) is (late_job_id == 1)
    finally:
        event.remove(marked.get_bind(), 'before_cursor_execute', record)
    assert len(queries) == 2


def test_deletion_boundary_and_policy_rollback_with_job(marked):
    from core.controlled_deletion import redact_deleted_job_history
    redact_deleted_job_history(marked, 1)
    bound = marked.execute(text('SELECT audit_max_id FROM controlled_job_tombstones')).scalar_one()
    assert marked.execute(text("SELECT action FROM audit_logs WHERE id=:id"), {'id': bound}).scalar_one() == 'controlled_part.job_deleted'
    marked.execute(text('DELETE FROM jobs WHERE id=1'))
    marked.rollback()
    assert marked.execute(text('SELECT id FROM jobs WHERE id=1')).scalar_one() == 1
    assert marked.execute(text('SELECT count(*) FROM controlled_job_tombstones')).scalar_one() == 0
    assert marked.execute(text("SELECT count(*) FROM audit_logs WHERE action='controlled_part.job_deleted'")).scalar_one() == 0
