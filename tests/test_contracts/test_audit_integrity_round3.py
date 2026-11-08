"""Round-three regressions for recovery, missing secrets and cross-process SIEM."""
import json
import os
import shutil
import subprocess
import sys
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session
from core.audit_integrity import verify
from core.audit_keys import directory, saved_head, rotate
from core.audit_retention import retain
from modules.system.audit_integrity_job import check_daily, health
from tests.test_contracts.test_audit_integrity_e1 import engine, client, append, destination
from tests.test_contracts.test_audit_integrity_round4 import restored


def rewind(engine, seq):
    with engine.begin() as c:
        digest = c.execute(text('SELECT row_hash FROM audit_logs WHERE chain_seq=:s'), {'s': seq}).scalar_one()
        c.execute(text('DELETE FROM audit_logs WHERE chain_seq>:s'), {'s': seq})
        c.execute(text('UPDATE audit_chain_state SET head_seq=:s,head_hash=:h'), {'s': seq, 'h': digest})


def test_anchor_advances_after_restore_and_detects_later_tail_deletion(engine, tier_license):
    tier_license('enterprise'); append(engine, 3)
    check_daily(engine, force=True)
    old = saved_head()
    rewind(engine, 1)
    broken = check_daily(engine, force=True)
    assert any(b['reason'] == 'signed_head_missing' for b in broken['breaks'])
    assert saved_head()['rows'] == 1
    assert saved_head()['after_break'] >= 1
    assert any(h['head'] == old['head'] for h in saved_head()['history'])
    append(engine, 4)
    check_daily(engine, force=True)
    rewind(engine, 2)
    result = check_daily(engine, force=True)
    assert any(b['reason'] == 'signed_head_missing' and b['chain_seq'] == 5 and not b['known'] for b in result['breaks'])


def test_baseline_acceptance_is_interactive_superadmin_audited_and_preserves_history(client, engine):
    http, principal = client
    append(engine, 3); check_daily(engine, force=True); rewind(engine, 1)
    check_daily(engine, force=True)
    restored(engine)
    path = '/audit-logs/accept-restore-baseline'
    body = {'reason': 'Restored the verified backup after disk failure'}
    principal['group_id'] = 2
    assert http.post(path, json=body).status_code == 403
    principal['group_id'] = None; principal['_auth_kind'] = 'user_api_token'
    assert http.post(path, json=body).status_code == 403
    principal['_auth_kind'] = 'session_jwt'
    assert http.post(path, json=body).status_code == 200
    with engine.connect() as c:
        assert verify(c)['ok']
        record = json.loads(c.execute(text("SELECT details FROM audit_logs WHERE action='audit.restore_baseline_accepted'")).scalar_one())
        assert record['actor_user_id'] == 1 and record['reason'] == body['reason']
    assert saved_head()['history']
    assert body['reason'] not in (directory()/'.audit-signed-head.json').read_text()
    append(engine, 2); check_daily(engine, force=True); rewind(engine, 2)
    assert not check_daily(engine, force=True)['ok']


@pytest.mark.parametrize('evidence', ['daily', 'retention', 'retention_empty', 'erasure'])
def test_lost_key_directory_is_distinct_and_never_recreated(engine, tier_license, evidence):
    tier_license('enterprise'); append(engine, 3)
    if evidence == 'daily':
        check_daily(engine, force=True)
    else:
        with Session(engine) as db:
            if evidence.startswith('retention'):
                cutoff = '2000-01-01' if evidence == 'retention_empty' else '2099-01-01'
                retain(db, 'id=1 AND timestamp<:cutoff', cutoff)
            else:
                from core.audit_erasure import rewrite
                rewrite(db, 'UPDATE audit_logs SET ip_address=NULL WHERE id=:id', {'id': 1})
            db.commit()
    shutil.rmtree(directory())
    with engine.connect() as c:
        result = verify(c)
    assert result['status'] == 'audit_key_missing' and result['breaks'] == []
    assert health(engine)['status'] == 'audit_key_missing'
    assert check_daily(engine, force=True)['status'] == 'audit_key_missing'
    assert not (directory() / '.audit-hmac-keys.json').exists()


def test_rotation_refuses_wrong_directory_owner(engine, tier_license, monkeypatch):
    tier_license('enterprise'); check_daily(engine, force=True)
    before = (directory() / '.audit-hmac-keys.json').read_bytes()
    monkeypatch.setattr(os, 'geteuid', lambda: directory().stat().st_uid+1)
    with pytest.raises(PermissionError, match='owner'):
        rotate()
    assert (directory() / '.audit-hmac-keys.json').read_bytes() == before


def test_sqlite_monitor_without_license_cache_enqueues(engine, tier_license):
    tier_license('enterprise'); destination(engine)
    from modules.system.audit_siem import licensed_destinations
    licensed_destinations(engine)
    code = """import sqlite3,sys
from core.audit_integrity import install_sqlite
import license_manager
assert license_manager._cached_license is None
c=sqlite3.connect(sys.argv[1]); install_sqlite(c)
c.execute("INSERT INTO audit_logs(action) VALUES('monitor')"); c.commit()
"""
    subprocess.run([sys.executable, '-c', code, engine.url.database], check=True,
                   env={**os.environ, 'PYTHONPATH': os.path.abspath('backend')})
    with engine.connect() as c:
        assert c.execute(text('SELECT count(*) FROM audit_siem_outbox')).scalar_one() == 1
        assert verify(c)['ok']


def test_unlicensed_collection_records_chained_gap(engine, tier_license):
    tier_license('enterprise'); destination(engine)
    with engine.begin() as c:
        c.execute(text('UPDATE audit_chain_state SET siem_until=0'))
    append(engine)
    with engine.connect() as c:
        gap = c.execute(text("SELECT details FROM audit_logs WHERE action='siem.gap'")).scalar_one()
        assert json.loads(gap)['reason'] == 'license_unavailable'
        assert verify(c)['ok']
        assert c.execute(text('SELECT count(*) FROM audit_siem_outbox')).scalar_one() == 0


def test_retention_emits_one_summary_for_multiple_pages_and_empty_run(engine):
    append(engine, 600)
    with Session(engine) as db:
        assert retain(db, 'timestamp<:cutoff', '2099-01-01') == 600
        db.commit()
        records = db.execute(text("SELECT details FROM audit_logs WHERE action='audit.retention'")).scalars().all()
        assert len(records) == 1 and json.loads(records[0])['deleted'] == 600
        assert verify(db.connection())['ok']
        assert retain(db, 'timestamp<:cutoff', '2000-01-01') == 0
        db.commit()
        assert db.execute(text("SELECT count(*) FROM audit_logs WHERE action='audit.retention'")).scalar_one() == 2


def test_native_backup_companion_and_download_preserve_both_files(engine, client, monkeypatch, tmp_path):
    import io
    import zipfile
    from modules.system.backup_service import create_online_backup
    from modules.system import routes_backup
    append(engine); check_daily(engine, force=True)
    backup, metadata = create_online_backup(str(engine.url))
    from modules.system.audit_backup import companion
    assert companion(backup).stat().st_mode & 0o777 == 0o600
    with zipfile.ZipFile(companion(backup)) as archive:
        assert archive.read('.audit-hmac-keys.json') == (directory()/'.audit-hmac-keys.json').read_bytes()
        assert archive.read('.audit-signed-head.json') == (directory()/'.audit-signed-head.json').read_bytes()
        assert json.loads(archive.read('manifest.json'))['database_sha256'] == metadata['sha256']
    http, _ = client
    http.app.include_router(routes_backup.router)
    monkeypatch.setattr(routes_backup, '_safe_backup_path', lambda filename: backup)
    response = http.get('/backups/'+backup.name+'?bundle=true')
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        # Privacy export rebuilds SQLite pages; logical contents and audit
        # material are preserved, and the companion names the exported digest.
        import hashlib
        import sqlite3
        exported = tmp_path / 'exported.db'
        data = archive.read(backup.name)
        exported.write_bytes(data)
        with sqlite3.connect(exported) as redacted, sqlite3.connect(backup) as original:
            assert list(redacted.iterdump()) == list(original.iterdump())
        assert json.loads(archive.read('audit-keys/manifest.json'))['database_sha256'] == hashlib.sha256(data).hexdigest()
        assert metadata['sha256'] == hashlib.sha256(backup.read_bytes()).hexdigest()
        assert archive.read('audit-keys/.audit-hmac-keys.json') == (directory()/'.audit-hmac-keys.json').read_bytes()


def test_common_backup_completion_copies_keys_for_postgres_archive(engine, tier_license, tmp_path):
    from modules.system.backup_failure_alerts import report_backup_failure
    from modules.system.audit_backup import companion
    tier_license('enterprise'); check_daily(engine, force=True)
    target = tmp_path/'owned.dump'
    target.write_bytes(b'owned archive fixture')
    @report_backup_failure
    def completed():
        return target, {'sha256': 'fixture'}
    completed()
    assert companion(target).is_file()


def test_key_loss_blocks_export_but_allows_unsealed_erasure(client, engine):
    from core.audit_erasure import rewrite
    append(engine); check_daily(engine, force=True)
    shutil.rmtree(directory())
    response = client[0].get('/export/audit-logs/jsonl?date_from=2000-01-01&date_to=2099-01-01')
    assert response.status_code == 409 and 'audit_key_missing' in response.text
    with Session(engine) as db:
        rewrite(db, 'UPDATE audit_logs SET ip_address=NULL WHERE id=:id', {'id': 1})
        db.commit()
        assert db.execute(text('SELECT ip_address FROM audit_logs WHERE id=1')).scalar_one() is None
        receipt = json.loads(db.execute(text('SELECT evidence FROM audit_redactions WHERE row_id=1')).scalar_one())
        assert receipt['unsealed'] and receipt['reason'] == 'audit_key_missing'


def test_acceptance_cannot_hide_row_corruption(client, engine):
    append(engine); check_daily(engine, force=True); restored(engine)
    with engine.begin() as c:
        c.execute(text("UPDATE audit_logs SET action='altered' WHERE id=1"))
    response = client[0].post('/audit-logs/accept-restore-baseline', json={'reason': 'Reviewed database restore'})
    assert response.status_code == 409
    with engine.connect() as c:
        assert not c.execute(text("SELECT 1 FROM audit_logs WHERE action='audit.restore_baseline_accepted'")).first()


def test_scheduled_backup_moves_and_prunes_audit_companion(engine, tier_license, monkeypatch):
    from datetime import datetime, timezone
    from pathlib import Path
    from modules.system import backup_schedule
    from modules.system.audit_backup import companion
    tier_license('enterprise'); check_daily(engine, force=True)
    monkeypatch.setattr(backup_schedule, 'rehearse_restore', lambda *_: None)
    result = backup_schedule.create_scheduled_backup(str(engine.url), now=datetime.now(timezone.utc))
    backup = Path(engine.url.database).parent/'backups'/result['filename']
    assert companion(backup).is_file()
    assert not list(backup.parent.glob(backup_schedule.PARTIAL_PREFIX+'*.audit.zip'))
    monkeypatch.setattr(backup_schedule, 'retained_backups', lambda _: set())
    assert backup_schedule.prune_scheduled_backups(backup.parent) == [backup.name]
    assert not companion(backup).exists()


def test_history_entries_remain_authentic_and_malformed_head_does_not_stop_advancement(engine, tier_license):
    from core.audit_receipts import authentic
    tier_license('enterprise'); append(engine)
    check_daily(engine, force=True)
    append(engine); check_daily(engine, force=True)
    assert all(authentic(entry) for entry in saved_head()['history'])
    (directory()/'.audit-signed-head.json').write_text('broken head bytes')
    append(engine)
    result = check_daily(engine, force=True)
    assert not result['ok'] and saved_head()['rows'] == 3
    assert saved_head()['history'][0]['untrusted_prior']['unreadable_head_sha256']
    rewind(engine, 1)
    assert any(b['reason'] == 'signed_head_missing' and b['chain_seq'] == 3
               for b in check_daily(engine, force=True)['breaks'])


def test_failed_baseline_persistence_cannot_publish_healthy_status(client, engine, monkeypatch):
    from core import audit_heads
    append(engine, 3); check_daily(engine, force=True); rewind(engine, 1)
    check_daily(engine, force=True)
    def unavailable(value):
        raise OSError('owned disk failure')
    restored(engine)
    monkeypatch.setattr(audit_heads, 'save_head', unavailable)
    with pytest.raises(OSError, match='owned disk failure'):
        client[0].post('/audit-logs/accept-restore-baseline', json={'reason': 'Reviewed restored database'})
    assert health(engine)['status'] == 'critical'
    with engine.connect() as c:
        assert not verify(c)['ok']
