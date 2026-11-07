"""Round-12 cases rewritten for durable job IDs and job-linked tombstones."""
from datetime import timedelta
import importlib

import pytest
from sqlalchemy import inspect, text

from core.audit import log_audit
from core.controlled_audit import audit_allowed
from core.controlled_parts import allowed, audit_visibility, policies
from core.controlled_redaction import redact_export
from core.controlled_retention import expired
from core.controlled_retention_query import pending_count
from core.timelapse_access import timelapse_visibility
from tests.test_contracts.test_controlled_parts import actor
from tests.test_contracts.test_controlled_round8 import (
    ORIGIN, clock_db, native_pg, add_job, add_rule, add_media, delete_job, new_job,
)


def members(db):
    db.execute(text("INSERT INTO users(id,username,password_hash,role,is_active) "
                    "VALUES(1,'member','unused','operator',:active),(2,'outsider','unused','admin',:active)"),
               {'active': True})
    db.execute(text('INSERT INTO controlled_audience_members(audience_id,user_id) VALUES(1,1)'))


@pytest.mark.parametrize('created', [None, ORIGIN])
def test_public_media_stays_public_and_late_controlled_media_is_covered(clock_db, created):
    db = clock_db
    members(db)
    first_id = new_job(db, created=created)
    add_media(db, 1, first_id, ORIGIN)
    db.execute(text('UPDATE timelapses SET created_at=NULL WHERE id=1'))
    delete_job(db, first_id)
    second_id = new_job(db, created=None)
    assert second_id > first_id
    add_rule(db, 'jobs', second_id, days=7)
    add_media(db, 2, second_id, ORIGIN)
    db.execute(text('UPDATE timelapses SET created_at=NULL WHERE id=2'))
    delete_job(db, second_id)
    # Identical NULL creation times cannot merge old media into the new snapshot.
    assert not expired(db, 1, now=ORIGIN + timedelta(days=8))
    assert expired(db, 2, now=ORIGIN + timedelta(days=8))
    add_media(db, 3, second_id, ORIGIN - timedelta(days=100))
    assert expired(db, 3, now=ORIGIN + timedelta(days=8))
    assert pending_count(db, now=ORIGIN + timedelta(days=8)) == 2
    # Changing timestamps after deletion cannot change restriction membership.
    db.execute(text('UPDATE timelapses SET created_at=:future WHERE id=2'),
               {'future': ORIGIN + timedelta(days=1000)})
    for who, expected in ((actor(), {1, 2, 3}), (actor(2), {1})):
        # Platform visibility isolates controlled membership from org tenancy.
        condition, values = timelapse_visibility(dict(who, role='admin', group_id=None))
        assert set(db.execute(text('SELECT id FROM timelapses WHERE ' + condition), values).scalars()) == expected
        rows = [{'id': tid, 'print_job_id': first_id if tid == 1 else second_id, 'job_name': 'name'} for tid in (1, 2, 3)]
        output = redact_export(db, who, {'timelapses': rows})['timelapses']
        assert [row['job_name'] for row in output] == ['name'] + (['name'] * 2 if who['id'] == 1 else ['[controlled]'] * 2)


def test_late_print_and_archive_are_restricted_by_deleted_job(clock_db):
    from modules.archives.access import archive_visibility
    db = clock_db
    members(db)
    add_job(db, 5)
    add_rule(db, 'jobs', 5, days=7)
    delete_job(db, 5)
    db.execute(text("INSERT INTO print_jobs(id,printer_id,scheduled_job_id,status,started_at) VALUES(50,1,5,'completed','2020-01-01')"))
    db.execute(text("INSERT INTO print_archives(id,job_id,print_name,status) VALUES(70,5,'secret','completed')"))
    for kind, identifier in (('print_job', 50), ('archive', 70)):
        assert allowed(db, kind, identifier, actor())
        assert not allowed(db, kind, identifier, actor(2))
    from core.controlled_parts import historical_camera_allowed
    assert historical_camera_allowed(db, 1, actor())
    assert not historical_camera_allowed(db, 1, actor(2))
    for who, expected in ((actor(), [70]), (actor(2), [])):
        condition, values = archive_visibility(dict(who, role='admin', group_id=None))
        assert db.execute(text('SELECT a.id FROM print_archives a WHERE ' + condition), values).scalars().all() == expected
        output = redact_export(db, who, {'print_archives': [{'id': 70, 'job_id': 5, 'print_name': 'secret'}]})
        assert output['print_archives'][0]['print_name'] == ('secret' if expected else '[controlled]')


def test_deleted_audits_ignore_timestamps_and_use_post_lock_maximum(clock_db):
    db = clock_db
    members(db)
    add_job(db, 5, created=ORIGIN)
    add_rule(db, 'jobs', 5, days=7)
    identifiers = []
    for when in (ORIGIN - timedelta(microseconds=1), ORIGIN, ORIGIN + timedelta(days=1)):
        row = log_audit(db, 'job.updated', 'job', 5, {'name': 'instance'})
        row.timestamp = when.replace(tzinfo=None)
        db.flush()
        identifiers.append(row.id)
    delete_job(db, 5)
    assert db.execute(text('SELECT audit_max_id FROM controlled_job_tombstones')).scalar_one() > identifiers[-1]
    db.execute(text('UPDATE audit_logs SET timestamp=NULL WHERE id=:id'), {'id': identifiers[1]})
    for who, expected in ((actor(), identifiers), (actor(2), [])):
        condition, values = audit_visibility(who, db)
        visible = db.execute(text("SELECT id FROM audit_logs WHERE action='job.updated' AND " + condition + ' ORDER BY id'), values).scalars().all()
        assert visible == expected
        for identifier in identifiers:
            assert audit_allowed(db, who, 'job', 5, identifier) == (identifier in expected)


def test_history_migration_removes_membership_shape(clock_db):
    migration = importlib.import_module('core.schema.migrations.055_controlled_history')
    migration.validate(clock_db.connection())
    inspector = inspect(clock_db.connection())
    columns = {c['name'] for c in inspector.get_columns('controlled_job_tombstones')}
    assert not {'audit_min_id', 'job_created_at'} & columns
    removed = {'controlled_job_snapshots', 'controlled_job_tombstone_media'}
    assert not removed & set(inspector.get_table_names())
    from modules.system.postgres_backup_service import canonical_tables
    assert not removed & canonical_tables()
    assert 'job_id_high_water' in canonical_tables()


def test_recreated_media_for_deleted_job_retains_its_policy(clock_db):
    db = clock_db
    add_job(db, 5)
    add_rule(db, 'jobs', 5, days=7)
    add_media(db, 1, 5, ORIGIN)
    delete_job(db, 5)
    assert expired(db, 1, now=ORIGIN + timedelta(days=8))
    db.execute(text('DELETE FROM timelapses WHERE id=1'))
    add_media(db, 1, 5, ORIGIN)
    assert policies(db, 'job', 5)
    assert expired(db, 1, now=ORIGIN + timedelta(days=8))
    assert pending_count(db, now=ORIGIN + timedelta(days=8)) == 1
