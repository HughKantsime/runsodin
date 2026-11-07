"""Standing SIEM trust, historical export gates and authenticated roster erasure."""
import json
import pytest
from tests.test_contracts.test_cert_G_harness import run_probe
from tests.test_contracts.test_controlled_round8 import clock_db, native_pg


@pytest.mark.parametrize('history', ['deleted_job', 'released_file'])
def test_retained_history_requires_each_export_ack(tmp_path, history):
    result = run_probe('controlled-r20-exports', r'''
install_license('enterprise')
sid=seed_user('super','admin'); uid=seed_user('member','operator')
c=login('super'); member=login('member')
import license_manager as lm
lm.get_installation_id(); lm.get_device_keypair()
sql("INSERT INTO controlled_audiences(id,name) VALUES(1,'Restricted')")
sql('INSERT INTO controlled_audience_members(audience_id,user_id) VALUES(1,:uid)',uid=uid)
sql("INSERT INTO print_files(id,filename) VALUES(1,'private.gcode')")
sql("INSERT INTO jobs(id,item_name,status,print_file_id) VALUES(1,'secret','completed',1)")
body=dict(audience_id=1,printer_tag='Secure',retention_days=7,reason='restriction')
kind='jobs' if HISTORY=='deleted_job' else 'files'
assert member.put('/api/controlled-parts/'+kind+'/1',json=body).status_code==200
from core.db import SessionLocal
from core.controlled_deletion import redact_deleted_job_history
from core.audit import log_audit
from sqlalchemy import text
with SessionLocal() as db:
 log_audit(db,'job.updated','job',1,{'name':'secret'}); db.commit()
 redact_deleted_job_history(db,1)
 # Exercise the deletion snapshot directly: direct job marks are otherwise deletion-protected.
 # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- fixed fixture deletion without external input
 db.execute(text('DELETE FROM controlled_jobs WHERE job_id=1'))
 # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- fixed fixture deletion without external input
 db.execute(text('DELETE FROM jobs WHERE id=1')); db.commit()
if HISTORY=='released_file':
 assert member.put('/api/controlled-parts/files/1',json={**body,'controlled':False}).status_code==200
assert not sql('SELECT 1 FROM controlled_files UNION ALL SELECT 1 FROM controlled_jobs')
from modules.system import routes_backup as routes
from types import SimpleNamespace
backup=WORK/'odin_backup_owned.db'; backup.write_bytes(b'owned')
routes._safe_backup_path=lambda name:backup
routes.stage_restore=lambda *a,**k:{'candidate_sha256':'accepted'}
routes.stage_postgres_restore=lambda *a,**k:{'candidate_sha256':'accepted'}
routes._postgres_maintenance_config=lambda:('owned-maintenance',None,None)
headers={'X-ODIN-Controlled-Ack':'true'}
query='?date_from=2000-01-01&date_to=2099-01-01'
checks=[
 ('backup','/api/backups/odin_backup_owned.db','/api/backups/odin_backup_owned.db/acknowledge',{}),
 ('support_bundle','/api/admin/support-bundle','/api/admin/support-bundle/acknowledge',{}),
 ('audit_jsonl','/api/export/audit-logs/jsonl'+query,'/api/export/audit-logs/jsonl/acknowledge'+query,{}),
 ('restore',None,'/api/backups/restore',{'files':{'file':('owned.db',b'owned')}}),
 ('restore_stage',None,'/api/backups/odin_backup_owned.db/stage',{}),
]
for kind,plain,accepted,kwargs in checks:
 if kind=='restore_stage': routes.sql=SimpleNamespace(is_postgres=True)
 r=c.get(plain) if plain else c.post(accepted,**kwargs)
 assert r.status_code==403,(kind,r.status_code,r.text)
 assert r.json()['detail']['code']=='controlled_part_acknowledgement_required'
 if not plain: accepted+='?acknowledge_controlled=true'
 r=c.post(accepted,headers=headers,**kwargs)
 assert r.status_code==200,(kind,r.status_code,r.text[:500])
 assert any(json.loads(row[0])['kind']==kind for row in sql("SELECT details FROM audit_logs WHERE action='controlled_part.privileged_export'"))
# Retained history also gates receiver enablement after the last live mark.
dest=dict(name='SOC',kind='syslog',endpoint='syslog://soc.example:6514',enabled=True)
assert c.put('/api/settings/siem/new',json=dest).status_code==403
r=c.put('/api/settings/siem/new',json={**dest,'enabled':False}); assert r.status_code==200,r.text
did=r.json()['id']
assert c.post('/api/settings/siem/'+did+'/acknowledge',headers=headers).status_code==200
assert c.put('/api/settings/siem/'+did,json=dest).status_code==200
OUT['ok']=True
'''.replace('HISTORY', repr(history)), tmp_path)
    assert result['ok'] and result['_net_attempts'] == []


def test_standing_ack_multiple_receivers_changes_tokens_and_deletion(tmp_path):
    result = run_probe('controlled-r20-siem', r'''
install_license('enterprise')
sid=seed_user('super','admin'); uid=seed_user('member','operator')
c=login('super'); member=login('member')
headers={'X-ODIN-Controlled-Ack':'true'}
sql("INSERT INTO controlled_audiences(id,name) VALUES(1,'Restricted')")
sql('INSERT INTO controlled_audience_members(audience_id,user_id) VALUES(1,:uid)',uid=uid)
sql("INSERT INTO print_files(id,filename) VALUES(1,'private.gcode')")
body=dict(audience_id=1,printer_tag='Secure',retention_days=7,reason='restriction')
dest=dict(name='SOC',kind='syslog',endpoint='syslog://soc.example:6514',enabled=True)
ids=[c.put('/api/settings/siem/new',json={**dest,'name':name}).json()['id'] for name in ('SOC','Other SOC')]
path='/api/settings/siem/'+ids[0]
assert c.post(path+'/acknowledge',headers=headers).status_code==200
assert member.put('/api/controlled-parts/files/1',json=body).json()['detail']['destination_id']==ids[1]
assert c.post('/api/settings/siem/'+ids[1]+'/acknowledge',headers=headers).status_code==200
assert member.put('/api/controlled-parts/files/1',json=body).status_code==200
# Token auth cannot grant trust even with CSRF header and full scopes.
from core.auth import hash_password
raw='odin_round20_test_token_only'
sql("INSERT INTO api_tokens(user_id,name,token_hash,token_prefix,scopes) VALUES(:u,'test',:h,:p,:s)",u=sid,h=hash_password(raw),p=raw[:10],s='["admin"]')
from fastapi.testclient import TestClient
token=TestClient(app,raise_server_exceptions=False)
r=token.post(path+'/acknowledge',headers={**headers,'X-API-Key':raw})
assert r.status_code==403,(r.status_code,r.text)
# Editing name/enablement preserves trust; retargeting while enabled cannot reuse it.
assert c.put(path,json={**dest,'name':'Renamed'}).status_code==200
sql('DELETE FROM audit_siem_outbox')
assert c.put(path,json={**dest,'endpoint':'syslog://new.example:6514'}).status_code==403
# The refusal itself is queued for the enabled receiver: disable, then drain before editing.
assert c.put(path,json={**dest,'enabled':False}).status_code==200
sql('DELETE FROM audit_siem_outbox')
assert c.put(path,json={**dest,'endpoint':'syslog://new.example:6514','enabled':False}).status_code==200
rows=c.get('/api/settings/siem').json()
assert next(row for row in rows if row['id']==ids[0])['controlled_acknowledgement'] is None
assert all('secret' not in row for row in rows)
assert not sql('SELECT 1 FROM system_config WHERE key=:key',key='controlled:siem_ack:'+ids[0])
assert c.post(path+'/acknowledge',headers=headers).status_code==200
# Restoring the old target also clears trust: old acknowledgements cannot resurrect.
assert c.put(path,json={**dest,'enabled':False}).status_code==200
assert not sql('SELECT 1 FROM system_config WHERE key=:key',key='controlled:siem_ack:'+ids[0])
assert c.delete('/api/settings/siem/'+ids[1]).status_code==200
assert not sql('SELECT 1 FROM system_config WHERE key=:key',key='controlled:siem_ack:'+ids[1])
assert c.post('/api/settings/siem/'+ids[1]+'/acknowledge',headers=headers).status_code==404
OUT['ok']=True
''', tmp_path)
    assert result['ok'] and result['_net_attempts'] == []


def test_audience_history_erasure_and_chain(tmp_path):
    result = run_probe('controlled-r20-erasure', r'''
install_license('enterprise')
sid=seed_user('super','admin'); uid=seed_user('member','operator'); other=seed_user('other','operator')
c=login('super')
body={'name':'Restricted','user_ids':[uid,other]}
r=c.post('/api/controlled-parts/audiences',json=body); assert r.status_code==200,r.text
aid=r.json()['id']
assert c.put('/api/controlled-parts/audiences/'+str(aid),json=body).status_code==200
assert c.delete('/api/controlled-parts/audiences/'+str(aid)).status_code==200
assert c.delete(f'/api/users/{uid}/erase').status_code==200
rows=[json.loads(row[0]) for row in sql("SELECT details FROM audit_logs WHERE action IN ('controlled_audience.saved','controlled_audience.deleted') ORDER BY id")]
assert rows==[{'before':[],'after':[other],'self_added':False},
 {'before':[other],'after':[other],'self_added':False}, {'before':[other],'after':[],'self_added':False}],rows
from core.audit_integrity import verify
from core.db import engine
with engine.connect() as connection:
 verdict=verify(connection); assert verdict['ok'],verdict
OUT['ok']=True
''', tmp_path)
    assert result['ok'] and result['_net_attempts'] == []


def test_roster_receipts_reject_other_changes():
    from core.audit_receipts import redaction
    for action in ('controlled_audience.saved', 'controlled_audience.deleted'):
        original={'before':[1,2,3],'after':[2,3],'self_added':False}
        before=dict(actor_user_id=4,actor_username='admin',actor_auth_kind='session_jwt',
                    ip_address=None,action=action,entity_type='controlled_audience',entity_id=1,
                    details=json.dumps(original))
        nullable = {**before, 'details': json.dumps({**original, 'before': None})}
        assert redaction(nullable, {**nullable, 'details': json.dumps({**original, 'before': None, 'after': [3]})})
        erased={**original,'before':[1,3],'after':[3]}
        assert redaction(before,{**before,'details':json.dumps(erased)})
        for bad in ({**erased,'after':[4]}, {**erased,'self_added':True},
                    {**erased,'before':[1]}, {**erased,'before':[3,1]}):
            assert not redaction(before,{**before,'details':json.dumps(bad)})


def test_standing_ack_survives_session_restart_and_binds_all_target_fields(clock_db, tier_license, monkeypatch):
    from sqlalchemy.orm import Session
    from modules.system.routes_audit_integrity import Destination, save_destination, acknowledge_destination, destinations
    from core.controlled_siem import acknowledgement, first_mark, target
    from fastapi import HTTPException
    tier_license('enterprise')
    admin = {'id': 7, 'role': 'admin', 'group_id': None}
    body = Destination(name='SOC', kind='syslog', endpoint='syslog://soc.example:6514', enabled=True)
    did = save_destination('new', body, admin, clock_db)['id']
    with pytest.raises(HTTPException):
        first_mark(clock_db)
    from modules.system import routes_audit_integrity as routes
    def failed_audit(*args, **kwargs):
        raise RuntimeError('Owned audit failure')
    with monkeypatch.context() as patch:
        patch.setattr(routes, 'log_audit', failed_audit)
        with pytest.raises(RuntimeError, match='Owned audit failure'):
            acknowledge_destination(did, admin, clock_db)
    clock_db.rollback()
    assert destinations(admin, clock_db)[0]['controlled_acknowledgement'] is None
    saved = acknowledge_destination(did, admin, clock_db)
    assert saved['user_id'] == 7
    engine = clock_db.get_bind()
    clock_db.rollback()
    with Session(engine) as db:
        first_mark(db)
        row = destinations(admin, db)[0]
        assert row['controlled_acknowledgement'] == saved
        assert 'secret' not in row
        original = {**row, 'secret': None}
        for field, value in [('endpoint', 'syslog://new.example:6514'), ('kind', 'webhook'),
                             ('tls', 0), ('secret', 'changed'), ('ca_bundle', 'changed')]:
            changed = {**original, field: value}
            assert target(changed) != target(original)
            assert acknowledgement(db, changed) is None
