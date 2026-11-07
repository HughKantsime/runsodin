"""Durable job identities isolate media, audits and acknowledgements."""
from datetime import timedelta

import pytest
from sqlalchemy import text

from core.audit import log_audit
from core.controlled_audit import audit_allowed
from core.controlled_parts import audit_visibility, policies
from core.controlled_retention import expired, preview_acknowledged
from core.controlled_retention_query import pending_count
from tests.test_contracts.test_controlled_parts import actor
from tests.test_contracts.test_controlled_round8 import (
    ORIGIN, clock_db, native_pg, add_job, add_rule, add_media, delete_job, new_job,
)


@pytest.mark.parametrize('same_audience', [False, True])
def test_distinct_ids_scope_media_audit_and_acknowledgement(clock_db, tier_license, same_audience):
    from modules.organizations.routes_controlled_retention import (
        preview, acknowledge, PurgeAcknowledgement,
    )
    db = clock_db
    tier_license('enterprise')
    db.execute(text("INSERT INTO controlled_audiences(id,name) VALUES(2,'Second')"))
    db.execute(text("INSERT INTO users(id,username,password_hash,role,is_active) "
                    "VALUES(1,'first','unused','operator',:active),(2,'second','unused','admin',:active)"), {'active': True})
    db.execute(text('INSERT INTO controlled_audience_members(audience_id,user_id) VALUES(1,1),(:audience,2)'),
               {'audience': 1 if same_audience else 2})
    def audit(job_id, created):
        entry = log_audit(db, 'job.updated', 'job', job_id, {'name': 'instance'})
        entry.timestamp = created.replace(tzinfo=None)
        db.flush()
        return entry.id

    def snapshot(job_id, deleted):
        delete_job(db, job_id)
        bound = db.execute(text('SELECT max(audit_max_id) FROM controlled_job_tombstones WHERE job_id=:id'), {'id': job_id}).scalar_one()
        db.execute(text('UPDATE controlled_job_tombstones SET created_at=:created WHERE audit_max_id=:bound'),
                   {'created': deleted.isoformat(), 'bound': bound})
        db.execute(text('UPDATE audit_logs SET timestamp=:created WHERE id=:bound'),
                   {'created': deleted, 'bound': bound})
        return {'job_id': job_id, 'audit_max_id': bound}

    first_id = new_job(db)
    add_rule(db, 'jobs', first_id, days=7)
    first_audit = audit(first_id, ORIGIN)
    add_media(db, 1, first_id, ORIGIN)
    first = snapshot(first_id, ORIGIN + timedelta(days=1))
    second_id = new_job(db, created=ORIGIN + timedelta(days=2))
    assert second_id > first_id
    add_rule(db, 'jobs', second_id, marked=ORIGIN + timedelta(days=2), days=30)
    db.execute(text('UPDATE controlled_jobs SET audience_id=:audience WHERE job_id=:job'),
               {'audience': 1 if same_audience else 2, 'job': second_id})
    second_audit = audit(second_id, ORIGIN + timedelta(days=2))
    add_media(db, 2, second_id, ORIGIN + timedelta(days=2))
    # Old media/audits must also avoid the replacement job's LIVE policy.
    assert expired(db, 1, now=ORIGIN + timedelta(days=10))
    assert not expired(db, 2, now=ORIGIN + timedelta(days=10))
    assert audit_allowed(db, actor(), 'job', first_id, first_audit)
    condition, params = audit_visibility(actor(), db)
    assert db.execute(text('SELECT id FROM audit_logs WHERE id=:id AND ' + condition),
                      {**params, 'id': first_audit}).scalar_one() == first_audit
    second = snapshot(second_id, ORIGIN + timedelta(days=3))
    db.commit()
    assert [r['retention_days'] for r in policies(db, 'job', first_id)] == [7]
    assert [r['retention_days'] for r in policies(db, 'job', second_id)] == [30]

    for media, days in ((1, 7), (2, 30)):
        created = ORIGIN + timedelta(days=0 if media == 1 else 2)
        assert [r['retention_days'] for r in policies(db, 'job', first_id if media == 1 else second_id)] == [days]
        assert not expired(db, media, now=created + timedelta(days=days, microseconds=-1))
        assert expired(db, media, now=created + timedelta(days=days))
    assert pending_count(db, now=ORIGIN + timedelta(days=10)) == 1
    assert pending_count(db, now=ORIGIN + timedelta(days=32)) == 2
    job_for = {first_audit: first_id, second_audit: second_id}

    for who, own, other in ((actor(), first_audit, second_audit), (actor(2), second_audit, first_audit)):
        assert audit_allowed(db, who, 'job', job_for[own], own)
        assert audit_allowed(db, who, 'job', job_for[other], other) == same_audience
        condition, params = audit_visibility(who, db)
        visible = db.execute(text('SELECT id FROM audit_logs WHERE action=\'job.updated\' AND ' + condition), params).scalars().all()
        assert set(visible) == ({first_audit, second_audit} if same_audience else {own})
        from core.controlled_redaction import redact_export
        rows = [{'id': identifier, 'entity_type': 'job', 'entity_id': job_for[identifier], 'details': 'instance'}
                for identifier in (own, other)]
        redacted = redact_export(db, who, rows)
        assert redacted[0]['details'] == 'instance'
        assert redacted[1]['details'] == ('instance' if same_audience else '[controlled]')
        # The deletion audit itself is included in its own snapshot, not the next.
        own_bound = first['audit_max_id'] if own == first_audit else second['audit_max_id']
        assert audit_allowed(db, who, 'job', job_for[own], own_bound)
    if not same_audience:
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as error:
            acknowledge(PurgeAcknowledgement(tombstones=[second]), actor(), db)
        assert error.value.status_code == 404
        db.rollback()
    assert preview(actor(), db)['tombstones'] == ([first, second] if same_audience else [first])
    acknowledge(PurgeAcknowledgement(tombstones=[first]), actor(), db)
    assert preview_acknowledged(db, 1)
    assert not preview_acknowledged(db, 2)
    assert pending_count(db, now=ORIGIN + timedelta(days=10)) == 0
    assert pending_count(db, now=ORIGIN + timedelta(days=32)) == 1
    assert preview(actor(2), db)['tombstones'] == [second]
    acknowledge(PurgeAcknowledgement(tombstones=[second]), actor(2), db)
    assert preview_acknowledged(db, 2)
    assert pending_count(db, now=ORIGIN + timedelta(days=32)) == 0


def test_acknowledge_requires_snapshot_identity():
    from pydantic import ValidationError
    from modules.organizations.routes_controlled_retention import PurgeAcknowledgement
    with pytest.raises(ValidationError):
        PurgeAcknowledgement(tombstones=[5])
