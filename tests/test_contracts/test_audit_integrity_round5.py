"""Round-five regressions for authenticated authority and honest gap evidence."""
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from core.audit_integrity import verify
from core.audit_keys import directory, saved_head, save_head
from core.audit_receipts import authentic
from core.audit_retention import retain
from modules.system.audit_integrity_job import check_daily
from tests.test_contracts.test_audit_integrity_e1 import engine, client, append, destination
from tests.test_contracts.test_audit_integrity_round4 import restored


@pytest.mark.parametrize('writer', ['sqlite', 'postgres'])
def test_restore_writers_seal_authority(engine, tier_license, monkeypatch, writer):
    tier_license('enterprise'); append(engine); check_daily(engine, force=True)
    details = {'restored_at': datetime.now(timezone.utc).isoformat()}
    if writer == 'sqlite':
        from modules.system.backup_service import _insert_restore_audit
        _insert_restore_audit(Path(engine.url.database), 'restore_completed', details)
    else:
        from modules.system import postgres_backup_service as service
        monkeypatch.setattr(service, 'create_database_engine', lambda *a, **k: engine)
        service._insert_postgres_restore_audit('owned', None, 'restore_completed', details)
    with engine.connect() as c:
        marker = json.loads(c.execute(text("SELECT details FROM audit_logs WHERE action='restore_completed'")).scalar_one())
        assert authentic(marker)
        assert verify(c)['pending_restore'] is not None


@pytest.mark.parametrize('forgery', ['unsigned', 'modified'])
def test_forged_restore_marker_cannot_authorize_acceptance(client, engine, forgery):
    append(engine); check_daily(engine, force=True)
    details = {'restored_at': datetime.now(timezone.utc).isoformat()}
    if forgery == 'modified':
        restored(engine)
        with engine.connect() as c:
            details = json.loads(c.execute(text("SELECT details FROM audit_logs WHERE action='restore_completed'")).scalar_one())
        details['restored_at'] = '2099-01-01T00:00:00+00:00'
    with engine.begin() as c:
        c.execute(text("INSERT INTO audit_logs(action,details) VALUES('restore_completed',:d)"), {'d': json.dumps(details)})
        assert verify(c)['pending_restore'] is None
    assert client[0].post('/audit-logs/accept-restore-baseline', json={'reason': 'Forged marker must be refused'}).status_code == 409


@pytest.mark.parametrize('operation', ['erasure', 'retention'])
@pytest.mark.parametrize('recover_keys', [False, True])
def test_unsealed_evidence_is_explicitly_unverified(engine, tier_license, operation, recover_keys):
    tier_license('enterprise'); append(engine, 2); check_daily(engine, force=True)
    keys = (directory()/'.audit-hmac-keys.json').read_bytes()
    shutil.rmtree(directory())
    with Session(engine) as db:
        if operation == 'erasure':
            from core.audit_erasure import rewrite
            rewrite(db, 'UPDATE audit_logs SET ip_address=NULL WHERE id=:id', {'id': 1})
        else:
            retain(db, 'id=1 AND timestamp<:cutoff', '2099-01-01')
        db.commit()
    if recover_keys:
        directory().mkdir(exist_ok=True)
        (directory()/'.audit-hmac-keys.json').write_bytes(keys)
    with engine.connect() as c:
        result = verify(c)
    reason = 'unverified_redaction' if operation == 'erasure' else 'unverified_deletion'
    assert not result['ok']
    evidence = next(b for b in result['breaks'] if b['reason'] == reason)
    assert "can't be told apart from tampering" in evidence['message']
    if recover_keys:
        assert result.get('status') != 'audit_key_missing'
        assert all(b['reason'] != 'audit_key_missing' for b in result['breaks'])
    else:
        assert result['status'] == 'audit_key_missing'


def test_gap_close_is_chained_and_queued_with_totals(engine, tier_license):
    from modules.system.audit_siem import licensed_destinations
    tier_license('enterprise'); destination(engine)
    with engine.begin() as c:
        c.execute(text('UPDATE audit_chain_state SET siem_until=0'))
    append(engine, 3)
    with engine.connect() as c:
        gap = dict(c.execute(text('SELECT * FROM audit_siem_gaps')).mappings().one())
    licensed_destinations(engine); append(engine, 2)
    with engine.connect() as c:
        close = c.execute(text("SELECT id,chain_seq,details FROM audit_logs WHERE action='siem.gap_closed'")).mappings().one()
        assert json.loads(close['details']) == {
            'gap_id': gap['gap_id'], 'first_id': gap['first_id'], 'last_id': gap['last_id'],
            'skipped': 3, 'reason': 'license_unavailable', 'state': 'closed'}
        assert close['chain_seq'] > 0 and verify(c)['ok']
        assert c.execute(text('SELECT 1 FROM audit_siem_outbox WHERE audit_id=:id'), {'id': close['id']}).first()


def test_history_compaction_preserves_every_acceptance(engine, tier_license):
    tier_license('enterprise'); append(engine); check_daily(engine, force=True)
    # More acceptances than the ordinary-history cap; also same-day collisions.
    expected = set(range(220))
    for index in expected:
        save_head({'head': 'a'*64, 'rows': index, 'checked_at': float(index),
                   'accepted_restore_baseline': {'record_id': index, 'restore_row_hash': str(index)}})
    for day in range(220):
        save_head({'head': 'b'*64, 'rows': 1000+day, 'checked_at': float((day+100)*86400)})
    head = saved_head()
    acceptances = [h for h in head['history'] if h.get('accepted_restore_baseline')]
    ordinary = [h for h in head['history'] if not h.get('accepted_restore_baseline')]
    assert {h['accepted_restore_baseline']['record_id'] for h in acceptances} == expected
    assert len(ordinary) <= 210
    assert authentic(head) and all(authentic(h) for h in head['history'])


def test_gap_close_rolls_back_and_is_delivered_after_retry(engine, tier_license):
    from modules.system.audit_siem import licensed_destinations, deliver_once
    tier_license('enterprise'); destination(engine)
    with engine.begin() as c:
        c.execute(text('UPDATE audit_chain_state SET siem_until=0'))
    append(engine, 2)
    licensed_destinations(engine)
    with Session(engine) as db:
        db.execute(text("INSERT INTO audit_logs(action) VALUES('rolled_back')"))
        assert db.execute(text("SELECT count(*) FROM audit_logs WHERE action='siem.gap_closed'")).scalar_one() == 1
        db.rollback()
    with engine.connect() as c:
        assert c.execute(text('SELECT closed FROM audit_siem_gaps')).scalar_one() == 0
        assert c.execute(text("SELECT count(*) FROM audit_logs WHERE action='siem.gap_closed'")).scalar_one() == 0
    append(engine)
    sent = []
    def sender(destination, body, event_id):
        sent.append(json.loads(body))
    while deliver_once(engine, now=100, sender=sender):
        pass
    close = [row for row in sent if row['action'] == 'siem.gap_closed']
    assert len(close) == 1 and json.loads(close[0]['details'])['skipped'] == 2
    with engine.connect() as c:
        assert verify(c)['ok']


@pytest.mark.parametrize('recursive', [False, True])
def test_gap_close_with_full_outbox_keeps_all_rows_chained(engine, tier_license, recursive):
    from modules.system.audit_siem import licensed_destinations
    tier_license('enterprise'); destination(engine)
    with engine.begin() as c:
        c.exec_driver_sql('PRAGMA recursive_triggers='+str(int(recursive)))
    append(engine)
    with engine.begin() as c:
        c.execute(text('DELETE FROM audit_siem_outbox'))
        c.execute(text('INSERT INTO audit_siem_outbox(destination_id,audit_id) VALUES(:destination,1)'),
                  [{'destination': str(i)} for i in range(10000)])
        c.execute(text('UPDATE audit_chain_state SET siem_until=0'))
    append(engine, 2)
    licensed_destinations(engine); append(engine)
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM audit_logs WHERE action='siem.gap_closed'")).scalar_one() == 1
        assert c.execute(text('SELECT count(*) FROM audit_siem_outbox')).scalar_one() <= 10000
        assert verify(c)['ok']


def test_disabled_destination_close_still_preserves_totals(engine, tier_license):
    tier_license('enterprise'); destination(engine)
    with engine.begin() as c:
        c.execute(text('UPDATE audit_chain_state SET siem_until=0'))
    append(engine, 2)
    with engine.begin() as c:
        c.execute(text('UPDATE audit_siem_destinations SET enabled=0'))
    append(engine)
    with engine.connect() as c:
        details = json.loads(c.execute(text("SELECT details FROM audit_logs WHERE action='siem.gap_closed'")).scalar_one())
        assert details['skipped'] == 2
        assert verify(c)['ok']


@pytest.mark.parametrize('operation', ['erasure', 'retention'])
def test_forged_unsealed_claim_with_existing_key_is_suspect(engine, tier_license, operation):
    tier_license('enterprise'); append(engine, 2); check_daily(engine, force=True)
    with Session(engine) as db:
        if operation == 'erasure':
            from core.audit_erasure import rewrite
            rewrite(db, 'UPDATE audit_logs SET ip_address=NULL WHERE id=:id', {'id': 1})
            table = 'audit_redactions'
        else:
            retain(db, 'id=1 AND timestamp<:cutoff', '2099-01-01')
            table = 'audit_retention_checkpoints'
        evidence = json.loads(db.execute(text('SELECT evidence FROM '+table)).scalar_one())
        evidence.pop('signature'); evidence.pop('key_id')
        evidence.update(unsealed=True, reason='audit_key_missing')
        db.execute(text('UPDATE '+table+' SET evidence=:e'), {'e': json.dumps(evidence)})
        db.commit()
        result = verify(db.connection())
    expected = 'unverified_redaction' if operation == 'erasure' else 'unverified_deletion'
    assert not result['ok'] and result.get('status') != 'audit_key_missing'
    assert any(b['reason'] == expected for b in result['breaks'])


def test_signed_restore_marker_prevents_replacement_key_creation(engine):
    from core.audit_keys import missing_key, keyring, AuditKeyMissing
    restored(engine)
    shutil.rmtree(directory())
    with engine.connect() as c:
        assert missing_key(c)
        with pytest.raises(AuditKeyMissing):
            keyring(create=True, connection=c)


def test_legacy_unsigned_restore_marker_is_not_sealed_evidence(engine):
    from core.audit_keys import missing_key, keyring
    with engine.begin() as c:
        c.execute(text("INSERT INTO audit_logs(action,details) VALUES('restore_completed','{}')"))
        assert not missing_key(c)
        assert keyring(create=True, connection=c)


@pytest.mark.parametrize('malformed', [[], None, {
    'unsealed': True, 'reason': 'audit_key_missing', 'first_seq': 1, 'last_seq': 1}])
def test_malformed_deletion_evidence_is_a_break_not_a_walk_crash(engine, malformed):
    append(engine)
    with engine.begin() as c:
        c.execute(text('INSERT INTO audit_retention_checkpoints(first_seq,evidence) VALUES(1,:e)'),
                  {'e': json.dumps(malformed)})
        result = verify(c)
        assert not result['ok']
        assert any(b['reason'] == 'checkpoint' for b in result['breaks'])
