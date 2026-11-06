"""E1 round-one adversaries, bounded work and snapshot concurrency regressions."""
import json
import time
from datetime import date
from sqlalchemy import event, text
from sqlalchemy.orm import Session
import pytest
from core.audit_integrity import verify, row, content_hash, canonical_projection
from core.audit_erasure import rewrite, record_erasure
from tests.test_contracts.test_audit_integrity_e1 import engine, client, append, destination


def test_forged_erasure_receipt_cannot_authorize_a_rewrite(engine):
    append(engine)
    with engine.begin() as c:
        from core.audit_keys import keyring
        keyring(create=True, connection=c)
        original = dict(row(c, 1))
        c.execute(text("UPDATE audit_logs SET details='{}',ip_address=NULL,hash_salt=NULL WHERE id=1"))
        current = row(c, 1)
        forged = {'row_id': 1, 'record_id': 2, 'operation': 'forged', 'before_valid': True,
                  'pre_erasure_row_hash': original['row_hash'], 'after_content_hash': current['content_hash'],
                  'stable_hash': current['stable_hash'], 'changed_fields': ['details', 'ip_address'],
                  'signature': '0'*64}
        c.execute(text("INSERT INTO audit_logs(action,details) VALUES('audit.erasure',:d)"),
                  {'d': json.dumps({'operation': 'forged', 'receipts': [forged]})})
        c.execute(text('INSERT INTO audit_redactions VALUES(1,2,:d)'), {'d': json.dumps(forged)})
        assert verify(c)['first_break'] == 1


def test_authenticated_receipt_cannot_be_edited_to_match_changed_details(engine):
    append(engine)
    with Session(engine) as db:
        rewrite(db, 'UPDATE audit_logs SET ip_address=NULL WHERE id=:id', {'id': 1})
        db.commit()
        assert verify(db.connection())['ok']
        db.execute(text("UPDATE audit_logs SET details='{}' WHERE id=1"))
        receipt = json.loads(db.execute(text('SELECT evidence FROM audit_redactions')).scalar_one())
        receipt['after_content_hash'] = row(db.connection(), 1)['content_hash']
        db.execute(text('UPDATE audit_redactions SET evidence=:d'), {'d': json.dumps(receipt)})
        assert verify(db.connection())['first_break'] == 1


@pytest.mark.parametrize('details', ['null', '{}', '{"unicode":"invented","null":null}'])
def test_erasure_rejects_non_redaction_detail_replacement(engine, details):
    append(engine)
    with Session(engine) as db:
        with pytest.raises(RuntimeError, match='redact'):
            rewrite(db, 'UPDATE audit_logs SET details=:d WHERE id=:id', {'d': details, 'id': 1})
        db.rollback()
        assert verify(db.connection())['ok']


def test_salts_are_distinct_destroyed_and_never_copied_to_receipts(engine):
    append(engine, 2)
    with Session(engine) as db:
        salts = db.execute(text('SELECT hash_salt FROM audit_logs')).scalars().all()
        assert len(set(salts)) == 2 and all(len(s) == 64 for s in salts)
        rewrite(db, 'UPDATE audit_logs SET ip_address=NULL WHERE id>:id', {'id': 0})
        db.commit()
        assert db.execute(text('SELECT count(*) FROM audit_logs WHERE id IN (1,2) AND hash_salt IS NULL')).scalar_one() == 2
        evidence = db.execute(text('SELECT evidence FROM audit_redactions')).scalars().all()
        assert all(salt not in ''.join(evidence) for salt in salts)
        assert verify(db.connection())['ok']
        assert db.execute(text("SELECT count(*) FROM audit_logs WHERE action='audit.erasure'")).scalar_one() == 1


def test_erasure_work_is_target_bounded_on_100k_rows(engine, record_property):
    with engine.begin() as c:
        c.execute(text("INSERT INTO audit_logs(action,ip_address) VALUES('cost','192.0.2.1')"), [{}]*100000)
    statements = []
    def observe(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)
    event.listen(engine, 'before_cursor_execute', observe)
    start = time.monotonic()
    try:
        with Session(engine) as db:
            rewrite(db, 'UPDATE audit_logs SET ip_address=NULL WHERE id<=:id', {'id': 20})
            db.commit()
    finally:
        event.remove(engine, 'before_cursor_execute', observe)
    duration = time.monotonic()-start
    record_property('erasure_seconds_20_of_100000', duration)
    assert len(statements) < 30
    assert not any('ORDER BY chain_seq' in s for s in statements)
    with engine.connect() as c:
        assert verify(c)['ok']


def test_wal_snapshot_allows_concurrent_append_and_keeps_head_consistent(engine):
    from core.audit_snapshot import snapshot
    with engine.connect() as c:
        c.exec_driver_sql('PRAGMA journal_mode=WAL')
    append(engine, 3)
    with snapshot(engine) as c:
        expected = verify(c)
        append(engine)  # completes before the reader closes
        assert verify(c) == expected
        with pytest.raises(Exception, match='readonly'):
            c.execute(text("INSERT INTO audit_logs(action) VALUES('forbidden')"))
    with engine.connect() as c:
        assert verify(c)['rows'] == 4


def test_daily_due_check_does_not_write_and_incremental_then_weekly_full(engine, tier_license):
    from modules.system.audit_integrity_job import check_daily
    tier_license('enterprise'); append(engine, 2)
    first = check_daily(engine, now=1000000)
    assert not first['incremental']
    writes = []
    def observe(c, cursor, statement, params, context, many):
        if statement.lstrip().upper().startswith(('UPDATE', 'INSERT', 'DELETE')):
            writes.append(statement)
    event.listen(engine, 'before_cursor_execute', observe)
    try:
        assert check_daily(engine, now=1000001) == first
        assert writes == []
    finally:
        event.remove(engine, 'before_cursor_execute', observe)
    append(engine)
    assert check_daily(engine, now=1086401)['incremental']
    assert not check_daily(engine, now=1700000)['incremental']


@pytest.mark.parametrize('tier', [None, 'pro', 'education', 'enterprise'])
def test_retention_reanchors_arbitrary_expired_ranges_on_every_tier(engine, tier_license, tier):
    from core.audit_retention import retain
    tier_license(tier)
    with engine.begin() as c:
        for timestamp in ('2000-01-01', '2099-01-01', '2000-01-01'):
            c.execute(text("INSERT INTO audit_logs(timestamp,action) VALUES(:t,'retained')"), {'t': timestamp+' 00:00:00'})
    with Session(engine) as db:
        assert retain(db, 'timestamp<:cutoff', '2020-01-01') == 2
        db.commit()
        assert verify(db.connection())['ok']
        evidence = [json.loads(d) for d in db.execute(text('SELECT evidence FROM audit_retention_checkpoints')).scalars()]
        assert [d['first_seq'] for d in evidence] == [1, 3]
        db.execute(text("UPDATE audit_retention_checkpoints SET evidence='{}' WHERE first_seq=1"))
        assert not verify(db.connection())['ok']


def test_export_has_recomputable_canonical_fields_and_existing_identity(client, engine):
    append(engine)
    response = client[0].get('/export/audit-logs/jsonl?date_from=2000-01-01&date_to=2099-01-01')
    assert response.status_code == 200
    record = response.json()
    assert isinstance(record['details'], str) and len(record['timestamp']) == 26
    assert content_hash(record) == record['row_hash']
    import base64
    manifest = json.loads(base64.b64decode(response.headers['X-ODIN-Audit-Manifest']))
    assert manifest['installation_id'] and len(manifest['key_fingerprint']) == 64


def test_export_does_not_create_missing_device_key(client, engine):
    import license_manager as lm
    from pathlib import Path
    key = Path(lm.LICENSE_DIR)/lm.DEVICE_PRIVATE_KEY_FILENAME
    key.unlink()
    response = client[0].get('/export/audit-logs/jsonl?date_from=2000-01-01&date_to=2099-01-01')
    assert response.status_code == 409
    assert not key.exists()


def test_siem_polling_does_not_append_audit_rows(client, engine):
    for _ in range(3):
        assert client[0].get('/settings/siem').status_code == 200
    with engine.connect() as c:
        assert verify(c)['rows'] == 0


def test_failed_destination_backs_off_all_events_while_other_receiver_runs(engine, tier_license):
    from modules.system.audit_siem import deliver_once
    tier_license('enterprise'); destination(engine); append(engine, 2)
    with engine.begin() as c:
        c.execute(text("INSERT INTO audit_siem_destinations(id,name,kind,endpoint,enabled,tls) VALUES('other','Other','syslog','syslog://192.0.2.11',1,0)"))
    append(engine)
    def fail(*args):
        raise TimeoutError()
    assert deliver_once(engine, now=100, destination_id='owned', sender=fail)
    assert not deliver_once(engine, now=101, destination_id='owned', sender=lambda *a: pytest.fail('no retry yet'))
    sent = []
    assert deliver_once(engine, now=101, destination_id='other', sender=lambda *a: sent.append(a))
    assert sent


def test_license_lapse_stops_filling_outbox(engine, tier_license):
    tier_license('enterprise'); destination(engine); append(engine)
    tier_license('pro')
    from modules.system.audit_siem import licensed_destinations
    licensed_destinations(engine)  # same refresh the API worker performs every ten seconds
    append(engine, 5)
    with engine.connect() as c:
        assert c.execute(text('SELECT count(*) FROM audit_siem_outbox')).scalar_one() == 1


def test_outbox_cap_drops_oldest_and_audits_gap_without_breaking_actor_seal(engine, tier_license):
    from core.audit_actor import RequestAuditScope, bind_verified_actor
    from core.audit import log_audit
    from core.schema.audit_integrity_version import MODULE
    import importlib
    limit = importlib.import_module(MODULE).OUTBOX_LIMIT
    tier_license('enterprise'); destination(engine)
    with engine.begin() as c:
        # References can be seeded directly to exercise the cap without 10k deliveries.
        c.execute(text("INSERT INTO audit_siem_outbox(destination_id,audit_id) VALUES('owned',:id)"), [{'id': -i-1} for i in range(limit)])
    with Session(engine) as db:
        scope = RequestAuditScope('192.0.2.1'); scope.register(db)
        bind_verified_actor(db, {'id': 99, 'username': 'owned', '_auth_kind': 'session_jwt'})
        log_audit(db, 'cap'); db.commit()
    with engine.connect() as c:
        assert c.execute(text('SELECT count(*) FROM audit_siem_outbox')).scalar_one() == limit
        assert c.execute(text('SELECT min(audit_id) FROM audit_siem_outbox')).scalar_one() == -limit+1
        assert c.execute(text("SELECT count(*) FROM audit_logs WHERE action='siem.gap'")).scalar_one() == 1
        assert verify(c)['ok']


def test_forged_daily_cursor_cannot_skip_a_corrupt_prefix(engine, tier_license):
    from modules.system.audit_integrity_job import check_daily
    tier_license('enterprise'); append(engine, 2)
    check_daily(engine, now=1000000)
    with engine.begin() as c:
        c.execute(text("UPDATE audit_logs SET action='corrupted' WHERE id=1"))
        result = json.loads(c.execute(text('SELECT result FROM audit_integrity_status')).scalar_one())
        result['full_checked_at'] = 9999999999
        c.execute(text('UPDATE audit_integrity_status SET result=:r'), {'r': json.dumps(result)})
    assert check_daily(engine, now=1000001)['first_break'] == 1


def test_retention_does_not_launder_a_corrupt_deleted_row(engine):
    from core.audit_retention import retain
    append(engine)
    with Session(engine) as db:
        db.execute(text("UPDATE audit_logs SET action='tampered' WHERE id=1"))
        assert retain(db, 'timestamp<:cutoff', '2099-01-01') == 1
        db.commit()
        assert not verify(db.connection())['ok']


@pytest.mark.parametrize('private_status', ['critical', 'not_licensed'])
def test_public_health_exposes_only_ok_or_attention(engine, monkeypatch, private_status):
    import asyncio
    from modules.system import routes_health, audit_integrity_job
    from core import db as core_db
    monkeypatch.setattr(routes_health, 'get_spoolman_url', lambda: None)
    monkeypatch.setattr(routes_health, 'guardian_health', lambda _: {})
    monkeypatch.setattr(core_db, 'current_engine', lambda: engine)
    monkeypatch.setattr(audit_integrity_job, 'health', lambda _: {'status': private_status, 'first_break': 42, 'licensed': True})
    result = asyncio.run(routes_health.health_check())
    assert result.audit_integrity == {'status': 'attention' if private_status == 'critical' else 'ok'}


def test_known_user_scrub_handles_corrupted_non_json_details(engine):
    with engine.begin() as c:
        c.execute(text("INSERT INTO audit_logs(action,entity_type,entity_id,details) VALUES('old','user',42,'{}')"))
        c.execute(text("UPDATE audit_logs SET details='broken personal plaintext' WHERE id=1"))
    with Session(engine) as db:
        rewrite(db, 'UPDATE audit_logs SET details=:d WHERE id=:id', {'d': '{"anonymized_user_id":42}', 'id': 1})
        db.commit()
        assert 'personal plaintext' not in db.execute(text('SELECT CAST(details AS TEXT) FROM audit_logs WHERE id=1')).scalar_one()
        assert verify(db.connection())['first_break'] == 1
