"""Released E4 exports obey E3 audience policy, including retained records."""
import asyncio
import io
import json
import zipfile
from datetime import datetime, timezone
from sqlalchemy import text
from sqlalchemy.orm import Session
from tests.test_contracts.test_traceability import world


def mark_file(engine):
    with engine.begin() as c:
        c.execute(text("INSERT INTO controlled_audiences(id,name) VALUES(1,'Restricted')"))
        c.execute(text('INSERT INTO controlled_audience_members(audience_id,user_id) VALUES(1,2)'))
        c.execute(text("INSERT INTO controlled_files(file_id,label,audience_id,printer_tag,retention_days,reason,marked_at) "
                       "VALUES(1,'CUI',1,'QA',30,'restricted','2026-11-01')"))


def test_retained_traceability_and_certificate_recheck_controlled_audience(world):
    client, engine, *_ = world
    assert client.get('/api/jobs/1/traceability').status_code == 200
    mark_file(engine)
    for path in ('traceability', 'certificate'):
        response = client.get('/api/jobs/1/' + path)
        assert response.status_code == 403
        assert response.json()['detail']['code'] == 'controlled_part_group_required'
        assert 'widget.gcode' not in response.text


def test_batch_filters_controlled_jobs_before_loader_and_manifest(world):
    from modules.jobs.traceability_batch import export_batch
    _, engine, user, *_ = world
    mark_file(engine)
    def forbidden_load(*args, **kwargs):
        raise AssertionError('Controlled job reached the traceability loader')
    with Session(engine) as db:
        response = export_batch(db, user, datetime(2026, 11, 1, tzinfo=timezone.utc),
                                datetime(2026, 11, 2, tzinfo=timezone.utc), forbidden_load)
        async def collect():
            return b''.join([chunk async for chunk in response.body_iterator])
        with zipfile.ZipFile(io.BytesIO(asyncio.run(collect()))) as archive:
            manifest = json.loads(archive.read('manifest.json'))
            assert manifest['job_ids'] == [] and manifest['skipped'] == []
