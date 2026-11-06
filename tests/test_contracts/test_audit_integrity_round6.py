"""Round-six keyring creation and explicit recovery authority regressions."""
import json
import shutil

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from core.audit_integrity import verify
from core.audit_keys import directory, keyring, sealed_evidence
from core.audit_retention import retain
from modules.system.audit_integrity_job import check_daily
from tests.test_contracts.test_audit_integrity_e1 import engine, client, append


@pytest.mark.parametrize('action', ['audit.erasure', 'audit.retention', 'audit.restore_baseline_accepted', 'restore_completed'])
@pytest.mark.parametrize('details', [{}, {'signature': 'forged'}, {'key_id': 'forged'},
                                     {'message': 'key_id and signature are not signed evidence'}])
def test_forged_unsigned_row_does_not_block_keyring_creation(engine, action, details):
    with engine.begin() as c:
        c.execute(text('INSERT INTO audit_logs(action,details) VALUES(:a,:d)'), {'a': action, 'd': json.dumps(details)})
        assert not sealed_evidence(c)
        assert keyring(create=True, connection=c)['active']


def test_unsigned_side_evidence_does_not_block_creation(engine):
    with engine.begin() as c:
        c.execute(text("INSERT INTO audit_redactions VALUES(1,2,'{}')"))
        c.execute(text("INSERT INTO audit_retention_checkpoints VALUES(1,'{}')"))
        c.execute(text("INSERT INTO audit_integrity_status VALUES(1,0,'{}')"))
        assert not sealed_evidence(c)
        assert keyring(create=True, connection=c)['active']


def lose_keys(engine):
    append(engine, 2)
    check_daily(engine, force=True)
    old = keyring()['active']
    shutil.rmtree(directory())
    with Session(engine) as db:
        retain(db, 'id=1 AND timestamp<:cutoff', '2099-01-01')
        db.commit()
    return old


def test_initialize_is_interactive_superadmin_audited_and_alerting(client, engine):
    http, principal = client
    old = lose_keys(engine)
    with engine.begin() as c:
        c.execute(text("INSERT INTO users(id,username,password_hash,role,is_active) VALUES(1,'owner','unused','admin',1)"))
    path = '/audit-logs/initialize-keyring'
    body = {'reason': 'Original signing keys lost with failed disk'}
    for role, group, auth in [('viewer', None, 'session_jwt'), ('operator', None, 'session_jwt'),
                              ('admin', 2, 'session_jwt'), ('admin', None, 'user_api_token'),
                              ('admin', None, 'legacy_global_api_key')]:
        principal.update(role=role, group_id=group, _auth_kind=auth)
        assert http.post(path, json=body).status_code == 403
        assert keyring() is None
    principal.update(role='admin', group_id=None, _auth_kind='session_jwt')
    assert http.post(path, json={'reason': ' ' * 12}).status_code == 422
    assert http.post(path, json=body).status_code == 200
    ring = keyring()
    assert ring['active'] != old and old not in ring['keys']
    assert http.post(path, json=body).status_code == 409
    with engine.connect() as c:
        row = c.execute(text("SELECT id,details,chain_seq,row_hash FROM audit_logs WHERE action='audit.keyring_initialized'")).mappings().one()
        details = json.loads(row['details'])
        assert details['actor_user_id'] == 1 and details['reason'] == body['reason']
        assert details['key_id'] == ring['active'] and details['earlier_evidence'] == 'unverified'
        assert row['chain_seq'] > 0 and row['row_hash']
        alert = c.execute(text("SELECT user_id,severity FROM alerts WHERE title='Audit keyring initialized after key loss'")).one()
        assert alert[0] == 1 and alert[1].lower() == 'critical'
        result = verify(c)
        assert result.get('status') != 'audit_key_missing' and not result['ok']
        assert any(b['reason'] == 'unverified_deletion' for b in result['breaks'])
        assert not any(b['reason'] in ('content', 'link') and b['row_id'] == row['id'] for b in result['breaks'])
    with Session(engine) as db:
        retain(db, 'timestamp<:cutoff', '2099-01-01'); db.commit()
        assert db.execute(text("SELECT 1 FROM audit_logs WHERE action='audit.keyring_initialized'")).first()


def test_initialize_requires_key_loss_and_enterprise(client, engine, tier_license):
    http, _ = client
    path = '/audit-logs/initialize-keyring'
    body = {'reason': 'Original signing keys unavailable'}
    assert http.post(path, json=body).status_code == 409
    assert keyring() is None
    lose_keys(engine)
    tier_license('community')
    assert http.post(path, json=body).status_code == 403
    assert keyring() is None


def test_failed_audit_commit_never_publishes_replacement_keys(client, engine, monkeypatch):
    from modules.system.audit_keyring_recovery import initialize_keyring
    lose_keys(engine)
    with Session(engine) as db:
        def fail():
            raise RuntimeError('commit failed')
        monkeypatch.setattr(db, 'commit', fail)
        with pytest.raises(RuntimeError, match='commit failed'):
            initialize_keyring(db, {'id': 1}, 'Original signing keys unavailable')
        db.rollback()
    assert keyring() is None
    with engine.connect() as c:
        assert not c.execute(text("SELECT 1 FROM audit_logs WHERE action='audit.keyring_initialized'")).first()
