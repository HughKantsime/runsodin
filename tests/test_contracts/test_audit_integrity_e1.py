"""E1 storage adversaries, bounded exports, erasure and durable delivery contracts."""
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import date
import hashlib
import importlib
import json
import io
import zipfile
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from core.schema import bootstrap_database
from core.audit import log_audit
from core.audit_integrity import verify, GENESIS


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.setenv('ODIN_AUDIT_KEY_DIR', str(tmp_path / 'audit-keys'))
    engine = create_engine('sqlite:///'+str(tmp_path/'audit.db'), connect_args={'timeout': 30, 'check_same_thread': False})
    bootstrap_database(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def client(engine, tier_license):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from core.db import get_db
    from core.dependencies import get_current_user
    from modules.system.routes_audit_integrity import router
    tier_license('enterprise')
    import license_manager as lm
    lm.get_installation_id(); lm.get_device_keypair()
    app = FastAPI(); app.include_router(router)
    principal = {'id': 1, 'role': 'admin', 'group_id': None, '_auth_kind': 'session_jwt'}
    app.dependency_overrides[get_current_user] = lambda: principal
    def db():
        with Session(engine) as session:
            yield session
    app.dependency_overrides[get_db] = db
    with TestClient(app) as client:
        yield client, principal


def append(engine, count=1):
    with Session(engine) as db:
        for _ in range(count):
            log_audit(db, 'test', 'job', 10, {'unicode': 'é:雪', 'null': None}, '192.0.2.1')
        db.commit()


def test_raw_and_orm_concurrent_inserts_form_one_chain(engine):
    def write(index):
        if index % 2:
            append(engine, 5)
        else:
            with engine.begin() as c:
                for _ in range(5):
                    c.execute(text("INSERT INTO audit_logs(action,details) VALUES('raw','null')"))
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write, range(16)))
    with engine.begin() as c:
        result = verify(c)
    assert result['ok'] and result['rows'] == 80 and result['genesis'] == GENESIS


@pytest.mark.parametrize('field,value', [('details', '"changed"'), ('action', 'bad'), ('ip_address','192.0.2.9'),
    ('timestamp','2025-01-01 00:00:00'), ('entity_type','user'), ('prev_hash','0'*64), ('row_hash','0'*64), ('chain_seq',8)])
def test_tamper_reports_first_row(engine, field, value):
    append(engine, 3)
    with engine.begin() as c:
        c.execute(text(f'UPDATE audit_logs SET {field}=:v WHERE id=2'), {'v': value})
        result = verify(c)
    assert not result['ok']
    # Altering append order is reported at the first missing link (next remaining row).
    assert result['first_break'] == (3 if field == 'chain_seq' else 2)


@pytest.mark.parametrize('target', ['middle','tail','all','head','genesis'])
def test_deletion_and_anchor_tampering_fail(engine, target):
    append(engine, 3)
    with engine.begin() as c:
        sql = {'middle':'DELETE FROM audit_logs WHERE id=2', 'tail':'DELETE FROM audit_logs WHERE id=3',
               'all':'DELETE FROM audit_logs', 'head':"UPDATE audit_chain_state SET head_hash='bad'",
               'genesis':"UPDATE audit_chain_state SET genesis='bad'"}[target]
        c.execute(text(sql))
        assert not verify(c)['ok']


def test_rollback_rolls_back_row_head_and_outbox(engine):
    destination(engine)
    with Session(engine) as db:
        log_audit(db, 'rollback'); db.flush(); db.rollback()
    with engine.begin() as c:
        assert verify(c)['rows'] == 0
        assert c.execute(text('SELECT count(*) FROM audit_siem_outbox')).scalar_one() == 0


def test_actor_stamp_is_sealed_before_each_next_insert(engine):
    from core.audit_actor import RequestAuditScope, bind_verified_actor
    with Session(engine) as db:
        scope = RequestAuditScope('192.0.2.20'); scope.register(db)
        bind_verified_actor(db, {'id': 99, 'username': 'retained', '_auth_kind': 'session_jwt'})
        for _ in range(4): log_audit(db, 'actor')
        db.commit()
    with engine.begin() as c:
        assert verify(c)['ok']
        assert c.execute(text('SELECT actor_username FROM audit_logs')).scalars().all() == ['retained']*4


def test_erasure_records_pre_hash_and_never_rehashes_history(engine):
    from core.audit_erasure import rewrite
    append(engine, 2)
    with Session(engine) as db:
        before = db.execute(text('SELECT row_hash FROM audit_logs WHERE id=1')).scalar_one()
        rewrite(db, 'UPDATE audit_logs SET details=:d,ip_address=NULL WHERE id=:id', {'d':'{"unicode":"[deleted]","null":null}', 'id':1})
        db.commit()
        assert verify(db.connection())['ok']
        assert db.execute(text('SELECT row_hash FROM audit_logs WHERE id=1')).scalar_one() == before
        receipt = json.loads(db.execute(text("SELECT evidence FROM audit_redactions WHERE row_id=1")).scalar_one())
        assert receipt['pre_erasure_row_hash'] == before
        assert set(receipt['changed_fields']) == {'details', 'ip_address'}
        rewrite(db, 'UPDATE audit_logs SET details=:d WHERE id=:id', {'d':'{"unicode":"[deleted-user]","null":null}','id':1})
        db.commit()
        assert verify(db.connection())['ok']
        db.execute(text("UPDATE audit_logs SET details='\"unrecorded\"' WHERE id=1"))
        assert verify(db.connection())['first_break'] == 1


def test_erasure_proceeds_but_does_not_launder_existing_tamper(engine):
    from core.audit_erasure import rewrite
    append(engine)
    with Session(engine) as db:
        db.execute(text("UPDATE audit_logs SET action='tampered' WHERE id=1"))
        rewrite(db, 'UPDATE audit_logs SET ip_address=NULL WHERE id=:id', {'id':1})
        db.commit()
        assert not verify(db.connection())['ok']
        assert db.execute(text('SELECT ip_address FROM audit_logs WHERE id=1')).scalar_one() is None


def test_receipt_tampering_invalidates_erased_row(engine):
    from core.audit_erasure import rewrite
    append(engine)
    with Session(engine) as db:
        rewrite(db, 'UPDATE audit_logs SET ip_address=NULL WHERE id=:id', {'id':1}); db.commit()
        db.execute(text("UPDATE audit_logs SET details='{}' WHERE action='audit.erasure'"))
        assert not verify(db.connection())['ok']


def destination(engine):
    from modules.system.audit_siem import licensed_destinations
    licensed_destinations(engine)
    with engine.begin() as c:
        c.execute(text("INSERT INTO audit_siem_destinations(id,name,kind,endpoint,enabled,tls) VALUES('owned','Fixture','syslog','syslog://192.0.2.10:514',1,0)"))


def test_outbox_retry_restart_and_scrubbed_payload(engine, tier_license):
    from core.audit_erasure import rewrite
    from modules.system.audit_siem import deliver_once
    tier_license('enterprise'); destination(engine); append(engine)
    def fail(*args): raise TimeoutError('secret must not be stored')
    assert deliver_once(engine, now=100, sender=fail)
    with engine.connect() as c:
        assert c.execute(text('SELECT attempts,next_attempt FROM audit_siem_outbox')).one() == (1,110)
        assert c.execute(text('SELECT last_error FROM audit_siem_destinations')).scalar_one() == 'TimeoutError'
    engine.dispose()  # durable retry, with entirely new connections
    with Session(engine) as db:
        rewrite(db, 'UPDATE audit_logs SET ip_address=NULL WHERE id=:id', {'id':1}); db.commit()
    sent=[]
    assert deliver_once(engine, now=111, sender=lambda d,b,i: sent.append((i,json.loads(b))))
    # Receipt has next_attempt=0 and may precede the retried original.
    assert deliver_once(engine, now=111, sender=lambda d,b,i: sent.append((i,json.loads(b))))
    original = next(r for i,r in sent if i=='1')
    assert original['ip_address'] is None
    with engine.connect() as c:
        assert c.execute(text('SELECT count(*) FROM audit_siem_outbox')).scalar_one() == 0


def test_abandoned_lease_replays_and_downgrade_suspends(engine, tier_license):
    from modules.system.audit_siem import deliver_once
    tier_license('enterprise'); destination(engine); append(engine)
    tier_license('pro')
    assert not deliver_once(engine, now=100, sender=lambda *a: pytest.fail('unlicensed send'))
    tier_license('enterprise')
    with engine.begin() as c:
        c.execute(text("UPDATE audit_siem_outbox SET lease_until=200,lease_token='dead'"))
    assert not deliver_once(engine, now=199, sender=lambda *a: None)
    assert deliver_once(engine, now=201, sender=lambda *a: None)


@pytest.mark.parametrize('role,group', [('viewer',None), ('operator',None), ('admin',1)])
@pytest.mark.parametrize('path', ['/audit-logs/verify','/settings/siem','/export/audit-logs/jsonl?date_from=2026-01-01&date_to=2026-12-31'])
def test_read_routes_superadmin_only(client, role, group, path):
    http, principal = client; principal.update(role=role, group_id=group)
    assert http.get(path).status_code == 403


@pytest.mark.parametrize('tier', [None,'pro','education'])
@pytest.mark.parametrize('method,path', [('GET','/audit-logs/verify'),('GET','/settings/siem'),
    ('GET','/export/audit-logs/jsonl?date_from=2026-01-01&date_to=2026-12-31'),
    ('PUT','/settings/siem/new'),('DELETE','/settings/siem/owned')])
def test_all_routes_license_refusal(client, tier_license, tier, method, path):
    http, _ = client; tier_license(tier)
    response = http.request(method,path,json={'name':'test','kind':'syslog','endpoint':'syslog://example.test'})
    assert response.status_code == 403
    assert 'feature' in response.text.lower()


def test_export_over_5000_signature_and_date_boundaries(client, engine):
    http, _ = client
    with engine.begin() as c:
        c.execute(text("INSERT INTO audit_logs(timestamp,action) VALUES('2026-01-01 23:59:59.999999','export')"), [{} for _ in range(5010)])
        c.execute(text("INSERT INTO audit_logs(timestamp,action) VALUES('2026-01-02 00:00:00','excluded')"))
    response = http.get('/export/audit-logs/jsonl?date_from=2026-01-01&date_to=2026-01-01')
    assert response.status_code == 200
    assert len(response.content.splitlines()) == 5010
    manifest = base64.b64decode(response.headers['X-ODIN-Audit-Manifest'])
    decoded = json.loads(manifest)
    assert decoded['sha256'] == hashlib.sha256(response.content).hexdigest()
    assert decoded['chain_rows'] == 5011
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    key = Ed25519PublicKey.from_public_bytes(base64.b64decode(response.headers['X-ODIN-Audit-Public-Key']))
    key.verify(base64.b64decode(response.headers['X-ODIN-Audit-Signature']), manifest)


@pytest.mark.parametrize('query', ['', '?date_from=2026-01-02&date_to=2026-01-01', '?date_from=2026-01-01&date_to=9999-12-31'])
def test_export_requires_valid_bounds(client, query):
    assert client[0].get('/export/audit-logs/jsonl'+query).status_code == 422


def test_config_disabled_default_secrets_and_deletion(client, engine, monkeypatch):
    from cryptography.fernet import Fernet
    from core import crypto
    monkeypatch.setattr(crypto, '_fernet', Fernet(Fernet.generate_key()), raising=False)
    http,_=client
    body={'name':'Owned','kind':'syslog','endpoint':'syslog://192.0.2.1:6514'}
    response=http.put('/settings/siem/new',json=body);assert response.status_code==200,response.text
    row=http.get('/settings/siem').json()[0]
    assert row['enabled']==0 and row['pending']==0 and 'secret' not in row
    assert http.delete('/settings/siem/'+row['id']).status_code==200
    assert http.get('/settings/siem').json()==[]
    with engine.begin() as c: assert verify(c)['ok']


def test_daily_check_persists_critical_alert_and_health(engine,tier_license):
    from modules.system.audit_integrity_job import check_daily,health
    tier_license('enterprise');append(engine)
    with engine.begin() as c:
        c.execute(text("INSERT INTO users(id,username,password_hash,role,is_active) VALUES(99,'owned','x','admin',TRUE)"))
        c.execute(text("UPDATE audit_logs SET action='bad' WHERE id=1"))
    import time
    now=time.time()
    assert not check_daily(engine,now=now)['ok']
    assert health(engine)['status']=='critical'
    check_daily(engine,now=now+86401)
    with engine.connect() as c:
        assert c.execute(text("SELECT count(*) FROM alerts WHERE severity='critical'")).scalar_one()==1


def test_full_gdpr_export_and_erase_keep_chain_and_pending_payloads(client,engine):
    from modules.organizations.routes_sessions import router
    http,_=client;http.app.include_router(router)
    destination(engine)
    with engine.begin() as c:
        c.execute(text("INSERT INTO users(id,username,email,password_hash,role,is_active) VALUES(1,'admin-owned','admin@example.test','x','admin',TRUE),(2,'alice-owned','alice@example.test','x','operator',TRUE)"))
        c.execute(text("INSERT INTO audit_logs(action,entity_type,entity_id,details,ip_address,actor_user_id,actor_username,actor_auth_kind) VALUES('test','user',2,:d,'192.0.2.7',2,'alice-owned','session_jwt')"),{'d':json.dumps({'name':'alice-owned','email':'alice@example.test'})})
        c.execute(text("INSERT INTO audit_logs(action,entity_type,details,ip_address) VALUES('login_failed','user',:d,'192.0.2.7')"),{'d':json.dumps('Failed login: alice-owned from 192.0.2.7')})
    exported=http.get('/users/2/export')
    assert exported.status_code==200,exported.text
    assert any(r['action']=='test' for r in exported.json()['audit_log_entries'])
    erased=http.delete('/users/2/erase')
    assert erased.status_code==200,erased.text
    with engine.begin() as c:
        result=verify(c);assert result['ok'],result
        assert c.execute(text("SELECT count(*) FROM audit_logs WHERE actor_username='alice-owned'")).scalar_one()==0
        for row in c.execute(text("SELECT details,ip_address FROM audit_logs WHERE id IN (1,2)")):
            assert 'alice-owned' not in str(row) and 'alice@example.test' not in str(row)
        assert c.execute(text('SELECT count(*) FROM audit_siem_outbox')).scalar_one()>0


def test_signed_bundle_contains_verifiable_detached_artifacts(client,engine):
    append(engine)
    response=client[0].get('/export/audit-logs/jsonl?date_from=2000-01-01&date_to=2099-12-31&bundle=true')
    assert response.status_code==200
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    with zipfile.ZipFile(io.BytesIO(response.content)) as z:
        manifest=z.read('manifest.json')
        assert json.loads(manifest)['sha256']==hashlib.sha256(z.read('audit-logs.jsonl')).hexdigest()
        Ed25519PublicKey.from_public_bytes(base64.b64decode(z.read('public-key.ed25519.b64'))).verify(base64.b64decode(z.read('signature.ed25519.b64')),manifest)


def test_failed_destination_does_not_hold_audit_writer_transaction(engine,tier_license):
    from modules.system.audit_siem import deliver_once
    tier_license('enterprise');destination(engine);append(engine)
    def receiver(*args):
        append(engine)  # distinct writer must finish while the network send is in progress
        raise TimeoutError()
    assert deliver_once(engine,sender=receiver)
    with engine.begin() as c: assert verify(c)['rows']==2


@pytest.mark.parametrize('role,group,kind', [('operator',None,'session_jwt'),('admin',3,'session_jwt'),('admin',None,'user_api_token')])
def test_writes_require_interactive_superadmin(client,role,group,kind):
    http,principal=client;principal.update(role=role,group_id=group,_auth_kind=kind)
    body={'name':'Owned','kind':'syslog','endpoint':'syslog://192.0.2.1'}
    assert http.put('/settings/siem/new',json=body).status_code==403
    assert http.delete('/settings/siem/'+'a'*32).status_code==403
