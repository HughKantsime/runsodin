"""Immutable deletion clocks and randomized SQL/Python retention parity."""
import importlib
import json
import random
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session

from core.controlled_deletion import redact_deleted_job_history
from core.controlled_parts import policies
from core.controlled_policy_clock import media_deadline
from core.controlled_retention import expired, preview_acknowledged
from core.controlled_retention_query import pending_count
from core.schema import bootstrap_database
from tests.test_contracts.test_controlled_postgres import native_pg

ORIGIN = datetime(2020, 1, 1, tzinfo=timezone.utc)


@pytest.fixture(params=['sqlite', 'postgresql'])
def clock_db(request, tmp_path):
    engine = (request.getfixturevalue('native_pg') if request.param == 'postgresql'
              else create_engine('sqlite:///' + str(tmp_path / 'clocks.db')))
    bootstrap_database(engine)
    migration = importlib.import_module('core.schema.migrations.055_controlled_history')
    with engine.begin() as connection:
        migration.validate(connection)
        connection.execute(text("INSERT INTO printers(id,name) VALUES(1,'Retention')"))
        connection.execute(text("INSERT INTO controlled_audiences(id,name) VALUES(1,'Secure')"))
    try:
        with Session(engine) as db:
            yield db
    finally:
        if request.param == 'sqlite':
            engine.dispose()


def add_job(db, identifier, file_id=None, *, created=ORIGIN - timedelta(days=40)):
    db.execute(text("INSERT INTO jobs(id,item_name,status,print_file_id,created_at) VALUES(:id,'Part','completed',:file,:created)"),
               {'id': identifier, 'file': file_id, 'created': created})


def new_job(db, *, created=ORIGIN):
    from modules.jobs.models import Job
    from core.base import JobStatus
    job = Job(item_name='Part', status=JobStatus.COMPLETED, created_at=created)
    db.add(job)
    db.flush()
    if created is None:
        db.execute(text('UPDATE jobs SET created_at=NULL WHERE id=:id'), {'id': job.id})
    return job.id


def add_rule(db, kind, identifier, *, marked=ORIGIN, days=30, changes=(), acknowledged=False):
    table, key = ('controlled_jobs', 'job_id') if kind == 'jobs' else ('controlled_files', 'file_id')
    history, previous = [], days
    for changed, new_days in changes:
        history.append({'changed_at': changed.isoformat(), 'previous_days': previous, 'days': new_days})
        previous = new_days
    values = {'id': identifier, 'marked': marked.isoformat(), 'days': previous, 'initial': days,
              'changes': json.dumps(history), 'kind': kind,
              'ack': marked.isoformat() if acknowledged else None}
    db.execute(text(f"""INSERT INTO {table}
        ({key},audience_id,label,printer_tag,retention_days,marked_at,reason,purge_previewed_at)
        VALUES(:id,1,'CUI','Secure',:days,:marked,'test',:ack)"""), values)
    db.execute(text("""INSERT INTO controlled_retention_clocks
        (resource_type,resource_id,marked_at,initial_days,changes)
        VALUES(:kind,:id,:marked,:initial,:changes)
        ON CONFLICT(resource_type,resource_id) DO UPDATE SET
            marked_at=excluded.marked_at,initial_days=excluded.initial_days,changes=excluded.changes"""), values)


def add_media(db, identifier, job_id, completed, *, status='ready', fallback=False, captured=None):
    nonce = None
    if captured is not None:
        nonce = 'capture-' + str(identifier)
        db.execute(text("""INSERT INTO timelapse_ownership_anchors
            (operation_id,timelapse_id,printer_id,scheduled_job_id,filename,owner_state,
             ownership_source,print_file_id,capture_printer_shared)
            VALUES(:nonce,:id,1,:job,'part.mp4','unresolved','capture_unresolved',:file,FALSE)"""),
                   {'nonce': nonce, 'id': identifier, 'job': job_id, 'file': captured})
    db.execute(text("""INSERT INTO timelapses
        (id,printer_id,print_job_id,filename,status,completed_at,created_at,ownership_operation_id)
        VALUES(:id,1,:job,'part.mp4',:status,:completed,:created,:nonce)"""),
               {'id': identifier, 'job': job_id, 'status': status, 'nonce': nonce,
                'completed': None if fallback else completed, 'created': completed})


def delete_job(db, identifier):
    redact_deleted_job_history(db, identifier)
    db.execute(text('DELETE FROM controlled_jobs WHERE job_id=:id'), {'id': identifier})
    db.execute(text('DELETE FROM jobs WHERE id=:id'), {'id': identifier})


def test_deletion_keeps_same_audience_rules_and_distinct_job_clocks(clock_db):
    db = clock_db
    db.execute(text("INSERT INTO print_files(id,filename) VALUES(1,'part.3mf')"))
    add_job(db, 1, 1)
    add_job(db, 2, 1)  # propagated job rule, sharing the selected file
    add_rule(db, 'jobs', 1, days=30, changes=[(ORIGIN + timedelta(days=25), 1)])
    add_rule(db, 'files', 1, marked=ORIGIN - timedelta(days=10), days=20)
    add_rule(db, 'jobs', 2, days=60, acknowledged=True)
    add_media(db, 1, 1, ORIGIN)
    before = policies(db, 'job', 1)
    assert len(before) == 3
    delete_job(db, 1)
    original = [dict(r) for r in db.execute(text('SELECT * FROM controlled_job_tombstones')).mappings()]
    assert len(original) == 3
    assert min(media_deadline(r, ORIGIN) for r in before) == ORIGIN + timedelta(days=20)
    # A later job has its own identity and cannot replace any previous clock.
    add_job(db, 3)
    add_rule(db, 'jobs', 3, marked=ORIGIN + timedelta(days=100), days=90)
    delete_job(db, 3)
    db.commit()
    retained = [dict(r) for r in db.execute(text('SELECT * FROM controlled_job_tombstones')).mappings()]
    assert len(retained) == 4
    assert all(row in retained for row in original)
    assert len({r['audit_max_id'] for r in retained}) == 2
    rules = policies(db, 'job', 1)
    assert len(rules) == 3
    assert min(media_deadline(r, ORIGIN) for r in rules) == ORIGIN + timedelta(days=20)
    assert min(r['marked_at'] for r in rules) == (ORIGIN - timedelta(days=10)).isoformat()
    assert expired(db, 1, now=ORIGIN + timedelta(days=20))
    assert not preview_acknowledged(db, 1)
    assert pending_count(db, now=ORIGIN + timedelta(days=20)) == 1


@pytest.mark.parametrize('seed', [8, 48, 2044])
def test_randomized_notice_sql_python_parity(clock_db, seed):
    db, rng = clock_db, random.Random(seed)
    identifiers, boundaries = [], set()
    for case in range(36):
        identifier = 100 + case * 10
        identifiers.append(identifier)
        source = case % 6  # direct, file, propagated, mixed, tombstone, captured alias
        file_id = identifier if source != 0 else None
        if file_id is not None:
            db.execute(text("INSERT INTO print_files(id,filename,stored_path) VALUES(:id,'part.3mf',:path)"),
                       {'id': file_id, 'path': '/part-' + str(file_id)})
        add_job(db, identifier, file_id if source != 5 else None)
        targets = ([('jobs', identifier)] if source == 0 else
                   [('files', file_id)] if source in (1, 5) else [])
        if source in (2, 3, 4):
            add_job(db, identifier + 1, file_id)
            targets.append(('jobs', identifier + 1))
        if source in (3, 4):
            targets += [('jobs', identifier), ('files', file_id)]
        for rule_number, (kind, resource) in enumerate(targets):
            marked = ORIGIN + timedelta(days=rng.randint(-20, 10), seconds=rng.randint(0, 60),
                                        microseconds=rng.choice([0, 123456, 999999]))
            marked = marked.astimezone(timezone(timedelta(hours=rng.choice([-5, 0, 5]))))
            sequences = [[30, 29, 30, 29], [30, 1, 60, 2], [1, 90, 1], [90],
                         [rng.randint(1, 90) for _ in range(rng.randint(2, 8))]]
            sequence = sequences[(case + rule_number) % len(sequences)]
            changed = marked
            changes = []
            for days in sequence[1:]:
                changed += timedelta(days=rng.randint(1, 15))
                changes.append((changed, days))
            add_rule(db, kind, resource, marked=marked, days=sequence[0], changes=changes,
                     acknowledged=rng.choice([True, False]))
        captured = None
        if source == 5:
            captured = identifier + 2
            db.execute(text("INSERT INTO print_files(id,filename,stored_path) VALUES(:id,'alias.3mf',:path)"),
                       {'id': captured, 'path': '/part-' + str(file_id)})
        completed = ORIGIN + timedelta(days=rng.randint(-30, 100), seconds=rng.randint(0, 60),
                                       microseconds=rng.choice([0, 123456, 999999]))
        status = ['ready', 'failed', 'deleting', 'capturing'][case % 4]
        add_media(db, identifier, identifier, completed, status=status,
                  fallback=case % 3 == 0, captured=captured)
        if source == 4:
            delete_job(db, identifier)
            # Only tombstones remain as the source of this media's policies.
            db.execute(text('DELETE FROM controlled_jobs WHERE job_id=:id'), {'id': identifier + 1})
            db.execute(text('DELETE FROM controlled_files WHERE file_id=:id'), {'id': file_id})
        rules = policies(db, 'job', identifier) + policies(db, 'file', captured)
        if rules and status != 'capturing':
            boundaries.add(min(media_deadline(r, completed) for r in rules))
    db.commit()
    # Exercise both sides and the exact boundary, as well as ordinary polling
    # dates. The oracle invokes the real Python retention/acknowledgement path.
    moments = {ORIGIN + timedelta(days=day) for day in (-40, 0, 25, 60, 120, 250)}
    for boundary in sorted(boundaries)[::3]:
        moments.update(boundary + timedelta(microseconds=offset) for offset in (-1, 0, 1))
    for now in sorted(moments):
        expected = sum(expired(db, tid, now=now) and not preview_acknowledged(db, tid)
                       for tid in identifiers)
        queries = []
        def record(conn, cursor, statement, params, context, many):
            queries.append(statement)
        event.listen(db.get_bind(), 'before_cursor_execute', record)
        try:
            actual = pending_count(db, now=now)
        finally:
            event.remove(db.get_bind(), 'before_cursor_execute', record)
        assert actual == expected, (seed, now, actual, expected)
        assert len(queries) == 1
