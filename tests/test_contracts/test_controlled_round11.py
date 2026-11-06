"""NULL starts retain audit bounds; new live clocks cannot expire old media."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from core.audit import log_audit
from core.controlled_audit import audit_allowed
from core.controlled_parts import audit_visibility
from core.controlled_redaction import redact_export
from core.controlled_retention import expired
from core.controlled_retention_query import pending_count
from tests.test_contracts.test_controlled_parts import actor
from tests.test_contracts.test_controlled_round8 import (
    ORIGIN, clock_db, native_pg, add_job, add_rule, add_media, delete_job, new_job,
)


def seed_deleted(db):
    db.execute(text("INSERT INTO users(id,username,password_hash,role,is_active) "
                    "VALUES(1,'member','unused','operator',:active),(2,'outsider','unused','admin',:active)"), {'active': True})
    db.execute(text('INSERT INTO controlled_audience_members(audience_id,user_id) VALUES(1,1)'))
    add_job(db, 5, created=None)
    add_rule(db, 'jobs', 5, days=7)
    entry = log_audit(db, 'job.updated', 'job', 5, {'name': 'secret'})
    entry.timestamp = ORIGIN.replace(tzinfo=None)
    db.flush()
    identifier = entry.id
    add_media(db, 1, 5, ORIGIN)
    delete_job(db, 5)
    db.commit()
    return identifier


def test_null_instance_start_redacts_and_purges_on_schedule(clock_db, tier_license, tmp_path, monkeypatch):
    from modules.archives import controlled_retention, controlled_legacy_retention
    from modules.organizations.routes_controlled_retention import acknowledge, PurgeAcknowledgement
    db = clock_db
    tier_license('enterprise')
    identifier = seed_deleted(db)
    assert audit_allowed(db, actor(), 'job', 5, identifier)
    assert not audit_allowed(db, actor(2), 'job', 5, identifier)
    condition, params = audit_visibility(actor(2), db)
    assert db.execute(text('SELECT id FROM audit_logs WHERE id=:id AND ' + condition), {**params, 'id': identifier}).first() is None
    rows = [{'id': identifier, 'entity_type': 'job', 'entity_id': 5, 'details': 'secret'}]
    assert redact_export(db, actor(2), rows)[0]['details'] == '[controlled]'
    for offset in (-1, 0, 1):
        now = ORIGIN + timedelta(days=7, microseconds=offset)
        assert expired(db, 1, now=now) == (offset >= 0)
        assert pending_count(db, now=now) == int(offset >= 0)

    snapshot = dict(db.execute(text('SELECT job_id,audit_max_id FROM controlled_job_tombstones')).mappings().one())
    acknowledge(PurgeAcknowledgement(tombstones=[snapshot]), actor(), db)
    media = tmp_path / 'part.mp4'
    media.write_bytes(b'controlled media')
    # Exercise the real worker and filesystem at the exact retention boundary.
    now = ORIGIN + timedelta(days=7, microseconds=-1)
    def at_clock(session, media_id):
        return expired(session, media_id, now=now)
    monkeypatch.setattr(controlled_retention, 'expired', at_clock)
    monkeypatch.setattr(controlled_legacy_retention, 'expired', at_clock)
    assert controlled_retention.purge_due(db.get_bind(), tmp_path) == 0
    assert media.exists()
    now += timedelta(microseconds=1)
    assert controlled_retention.purge_due(db.get_bind(), tmp_path) == 1
    assert not media.exists()
    assert db.execute(text('SELECT id FROM timelapses WHERE id=1')).first() is None
    assert db.execute(text("SELECT count(*) FROM audit_logs WHERE action='controlled_part.timelapse_purged'")).scalar_one() == 1


@pytest.mark.parametrize('timestamp', [None, 'after_deletion'])
def test_deleted_audit_membership_survives_timestamp_changes(clock_db, timestamp):
    db = clock_db
    identifier = seed_deleted(db)
    value = None if timestamp is None else datetime.now(timezone.utc) + timedelta(days=1)
    db.execute(text('UPDATE audit_logs SET timestamp=:value WHERE id=:id'), {'value': value, 'id': identifier})
    db.commit()
    # Only stored id bounds matter; entitled members retain their own rows.
    for who, visible in ((actor(), True), (actor(2), False)):
        assert audit_allowed(db, who, 'job', 5, identifier) == visible
        condition, params = audit_visibility(who, db)
        found = db.execute(text('SELECT id FROM audit_logs WHERE id=:id AND ' + condition),
                           {**params, 'id': identifier}).first()
        assert bool(found) == visible
        rows = [{'id': identifier, 'entity_type': 'job', 'entity_id': 5, 'details': 'secret'}]
        assert redact_export(db, who, rows)[0]['details'] == ('secret' if visible else '[controlled]')


def test_shorter_live_replacement_does_not_change_earlier_notice(clock_db):
    db = clock_db
    first_id = new_job(db)
    add_rule(db, 'jobs', first_id, days=30)
    add_media(db, 1, first_id, ORIGIN)
    delete_job(db, first_id)
    db.execute(text('UPDATE controlled_job_tombstones SET created_at=:at'), {'at': (ORIGIN + timedelta(days=1)).isoformat()})
    now = ORIGIN + timedelta(days=10)
    assert pending_count(db, now=now) == 0
    assert not expired(db, 1, now=now)
    second_id = new_job(db, created=ORIGIN + timedelta(days=2))
    assert second_id > first_id
    add_rule(db, 'jobs', second_id, marked=ORIGIN + timedelta(days=2), days=1)
    assert pending_count(db, now=now) == 0
    assert not expired(db, 1, now=now)
    add_media(db, 2, second_id, ORIGIN + timedelta(days=2))
    assert pending_count(db, now=now) == 1
    assert expired(db, 2, now=now)
    for offset in (-1, 0, 1):
        now = ORIGIN + timedelta(days=30, microseconds=offset)
        assert expired(db, 1, now=now) == (offset >= 0)
        assert pending_count(db, now=now) == 1 + int(offset >= 0)
