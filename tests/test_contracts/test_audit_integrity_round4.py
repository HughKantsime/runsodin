"""Round-four regressions: lawful erasure, restore authority, and bounded evidence."""
import json
import shutil
from pathlib import Path
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session
from core.audit_integrity import verify
from core.audit_keys import directory, saved_head
from core.audit_receipts import authentic
from core.audit_retention import retain
from modules.system.audit_integrity_job import check_daily
from tests.test_contracts.test_audit_integrity_e1 import engine, client, append, destination


def restored(engine, restored_at=None):
    from datetime import datetime, timezone
    from modules.system.backup_service import _insert_restore_audit
    _insert_restore_audit(Path(engine.url.database), 'restore_completed', {
        'restored_at': restored_at or datetime.now(timezone.utc).isoformat()})


@pytest.mark.parametrize('operation', ['erasure', 'retention'])
def test_key_loss_never_blocks_required_deletion(engine, tier_license, operation):
    tier_license('enterprise'); append(engine, 3); check_daily(engine, force=True)
    keys = (directory()/'.audit-hmac-keys.json').read_bytes()
    shutil.rmtree(directory())
    with Session(engine) as db:
        if operation == 'erasure':
            from core.audit_erasure import rewrite
            rewrite(db, 'UPDATE audit_logs SET ip_address=NULL WHERE id=:id', {'id': 1})
            assert db.execute(text('SELECT ip_address FROM audit_logs WHERE id=1')).scalar_one() is None
            raw = db.execute(text('SELECT evidence FROM audit_redactions')).scalar_one()
        else:
            assert retain(db, 'id=1 AND timestamp<:cutoff', '2099-01-01') == 1
            assert not db.execute(text('SELECT 1 FROM audit_logs WHERE id=1')).first()
            raw = db.execute(text('SELECT evidence FROM audit_retention_checkpoints')).scalar_one()
        evidence = json.loads(raw)
        assert evidence['unsealed'] is True and evidence['reason'] == 'audit_key_missing'
        assert not authentic(evidence)
        db.commit()
        assert verify(db.connection())['status'] == 'audit_key_missing'
    assert not (directory()/'.audit-hmac-keys.json').exists()
    directory().mkdir(exist_ok=True)
    (directory()/'.audit-hmac-keys.json').write_bytes(keys)
    with engine.connect() as c:
        result = verify(c)
        assert not result['ok']
        reason = 'unverified_redaction' if operation == 'erasure' else 'unverified_deletion'
        assert any(b['reason'] == reason for b in result['breaks'])


def test_baseline_requires_fresh_restore_and_is_single_use(client, engine):
    http, _ = client
    append(engine); check_daily(engine, force=True)
    path = '/audit-logs/accept-restore-baseline'
    body = {'reason': 'Reviewed native restore evidence'}
    assert http.post(path, json=body).status_code == 409
    restored(engine, '2000-01-01T00:00:00+00:00')
    assert http.post(path, json=body).status_code == 409
    restored(engine)
    # A scheduled verification must not consume the restore's acceptance window.
    check_daily(engine, force=True)
    assert http.post(path, json=body).status_code == 200
    assert http.post(path, json=body).status_code == 409
    restored(engine)
    assert http.post(path, json=body).status_code == 200


def test_restore_markers_and_acceptances_survive_retention(client, engine):
    append(engine); check_daily(engine, force=True); restored(engine)
    assert client[0].post('/audit-logs/accept-restore-baseline', json={'reason': 'Reviewed native restore evidence'}).status_code == 200
    with Session(engine) as db:
        retain(db, 'timestamp<:cutoff', '2099-01-01'); db.commit()
        actions = db.execute(text('SELECT action FROM audit_logs')).scalars().all()
        assert 'restore_completed' in actions and 'audit.restore_baseline_accepted' in actions
        assert verify(db.connection())['ok']


def test_lapsed_siem_opens_extends_and_closes_one_gap(engine, tier_license):
    tier_license('enterprise'); destination(engine)
    with engine.begin() as c:
        c.execute(text('UPDATE audit_chain_state SET siem_until=0'))
    append(engine, 300)
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM audit_logs WHERE action='siem.gap'")).scalar_one() == 1
        gap = c.execute(text('SELECT * FROM audit_siem_gaps')).mappings().one()
        assert gap['skipped'] == 300 and gap['last_id'] > gap['first_id'] and gap['closed'] == 0
        assert verify(c)['ok']
    from modules.system.audit_siem import licensed_destinations
    licensed_destinations(engine); append(engine)
    with engine.begin() as c:
        assert c.execute(text('SELECT closed FROM audit_siem_gaps')).scalar_one() == 1
        c.execute(text('UPDATE audit_chain_state SET siem_until=0'))
    append(engine, 2)
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM audit_logs WHERE action='siem.gap'")).scalar_one() == 2
        assert c.execute(text('SELECT skipped FROM audit_siem_gaps ORDER BY gap_id')).scalars().all() == [300, 2]
        assert verify(c)['ok']


def test_companion_failure_preserves_native_backup_and_alerts(tmp_path, monkeypatch):
    from modules.system import audit_backup, backup_failure_alerts
    backup = tmp_path/'backup.dump'; backup.write_bytes(b'valid native database backup')
    alerts = []
    def fail(*args):
        raise OSError('simulated capture failure')
    monkeypatch.setattr(audit_backup, 'capture_material', fail)
    monkeypatch.setattr(backup_failure_alerts, 'publish_backup_failure', alerts.append)
    result = (backup, {'sha256': 'owned-test'})
    assert audit_backup.complete_backup(result) == result
    assert backup.read_bytes() == b'valid native database backup'
    assert len(alerts) == 1
    assert backup_failure_alerts.backup_failure_reason(alerts[0])[0] == 'audit_companion'
    assert result[1]['audit_companion'] == 'failed'


def test_backup_bundle_download_audit_names_bundle(engine, monkeypatch):
    from modules.system import routes_backup, audit_backup
    backup = Path(engine.url.database)
    monkeypatch.setattr(routes_backup, '_safe_backup_path', lambda _: backup)
    monkeypatch.setattr(audit_backup, 'download_bundle', lambda _, **kwargs: 'owned-response')
    with Session(engine) as db:
        assert routes_backup.download_backup('owned', bundle=True, current_user={}, db=db) == 'owned-response'
        record = json.loads(db.execute(text("SELECT details FROM audit_logs WHERE action='backup.downloaded'")).scalar_one())
        assert record['bundle'] is True


def test_head_history_is_bounded_and_only_small_objects_are_sealed(engine, tier_license, monkeypatch):
    from core.audit_keys import save_head
    from core import audit_receipts
    tier_license('enterprise'); append(engine); check_daily(engine, force=True)
    real_seal = audit_receipts.seal
    sizes = []
    def measured(value, connection=None):
        sizes.append(len(json.dumps(value)))
        return real_seal(value, connection)
    monkeypatch.setattr(audit_receipts, 'seal', measured)
    for day in range(1000):
        save_head({'head': 'a'*64, 'rows': day, 'checked_at': day*86400.0})
    head = saved_head()
    assert len(head['history']) <= 210
    assert authentic(head) and all(authentic(h) for h in head['history'])
    assert max(sizes) < 4096
    head['history'][0]['rows'] = -1
    assert not authentic(head)


def test_postgres_restore_writer_creates_acceptable_marker(client, engine, monkeypatch):
    # Exercise the production PostgreSQL writer with its owned engine dependency;
    # native PostgreSQL execution remains the separate database-parity proof.
    from datetime import datetime, timezone
    from modules.system import postgres_backup_service
    append(engine); check_daily(engine, force=True)
    monkeypatch.setattr(postgres_backup_service, 'create_database_engine', lambda *a, **k: engine)
    postgres_backup_service._insert_postgres_restore_audit('owned-url', None, 'restore_completed', {
        'restored_at': datetime.now(timezone.utc).isoformat()})
    assert client[0].post('/audit-logs/accept-restore-baseline', json={
        'reason': 'Reviewed PostgreSQL restore marker'}).status_code == 200


def test_retention_preserves_restore_authority_even_without_integrity_tables(engine):
    # Old/pre-migration fallback must preserve the same authority records.
    with engine.begin() as c:
        c.exec_driver_sql('DROP TRIGGER odin_audit_append')
        c.exec_driver_sql('DROP TABLE audit_chain_state')
        c.execute(text("INSERT INTO audit_logs(action) VALUES('restore_completed'),('audit.restore_baseline_accepted'),('old')"))
    with Session(engine) as db:
        assert retain(db, 'timestamp<:cutoff', '2099-01-01') == 1
        assert set(db.execute(text('SELECT action FROM audit_logs')).scalars()) == {
            'restore_completed', 'audit.restore_baseline_accepted'}


def test_siem_gap_updates_rollback_and_disabled_destination_ends_stretch(engine, tier_license):
    tier_license('enterprise'); destination(engine)
    with engine.begin() as c:
        c.execute(text('UPDATE audit_chain_state SET siem_until=0'))
    append(engine)
    with Session(engine) as db:
        db.execute(text("INSERT INTO audit_logs(action) VALUES('rolled_back')"))
        db.rollback()
    with engine.begin() as c:
        assert c.execute(text('SELECT skipped FROM audit_siem_gaps')).scalar_one() == 1
        c.execute(text('UPDATE audit_siem_destinations SET enabled=0'))
    append(engine)
    with engine.begin() as c:
        assert c.execute(text('SELECT closed FROM audit_siem_gaps')).scalar_one() == 1
        c.execute(text('UPDATE audit_siem_destinations SET enabled=1'))
    append(engine)
    with engine.connect() as c:
        assert c.execute(text('SELECT count(*) FROM audit_siem_gaps')).scalar_one() == 2
        assert verify(c)['ok']


def test_unsealed_deletion_is_atomic_with_caller_rollback(engine, tier_license):
    from core.audit_erasure import rewrite
    tier_license('enterprise'); append(engine, 2); check_daily(engine, force=True)
    shutil.rmtree(directory())
    with Session(engine) as db:
        rewrite(db, 'UPDATE audit_logs SET ip_address=NULL WHERE id=:id', {'id': 1})
        assert retain(db, 'id=2 AND timestamp<:cutoff', '2099-01-01') == 1
        db.rollback()
    with engine.connect() as c:
        assert c.execute(text('SELECT count(*) FROM audit_logs WHERE ip_address IS NOT NULL')).scalar_one() == 2
        assert c.execute(text('SELECT count(*) FROM audit_redactions')).scalar_one() == 0
        assert c.execute(text('SELECT count(*) FROM audit_retention_checkpoints')).scalar_one() == 0


def test_retention_does_not_split_an_open_siem_gap(engine, tier_license):
    tier_license('enterprise'); destination(engine)
    with engine.begin() as c:
        c.execute(text('UPDATE audit_chain_state SET siem_until=0'))
    append(engine, 3)
    with Session(engine) as db:
        assert retain(db, 'timestamp<:cutoff', '2099-01-01') == 3
        db.commit()
        assert db.execute(text("SELECT count(*) FROM audit_logs WHERE action='siem.gap'")).scalar_one() == 1
        assert db.execute(text('SELECT skipped FROM audit_siem_gaps')).scalar_one() == 4
        assert verify(db.connection())['ok']
