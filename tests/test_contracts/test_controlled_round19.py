"""Round 19: real HTTP privacy/export boundaries and transactional SIEM trust."""
import json
import pytest
from fastapi import HTTPException
from sqlalchemy import event
from tests.test_contracts.test_cert_G_harness import run_probe
from tests.test_contracts.test_controlled_parts import database, actor, mark


def test_gdpr_traceability_export_and_policy_erasure(tmp_path):
    result = run_probe('controlled-r19-gdpr', r'''
install_license('enterprise')
sql("INSERT INTO groups(id,name,is_org) VALUES(1,'Org',1)")
uid = seed_user('member', 'operator', 1)
aid = seed_user('outsider', 'admin', 1)
sid = seed_user('super', 'admin')
member = login('member'); outsider = login('outsider'); superadmin = login('super')
sql("INSERT INTO controlled_audiences(id,name) VALUES(1,'Restricted')")
sql('INSERT INTO controlled_audience_members(audience_id,user_id) VALUES(1,:uid)', uid=uid)
sql("INSERT INTO print_files(id,filename,org_id) VALUES(1,'private.gcode',1)")
sql("INSERT INTO jobs(id,item_name,status,print_file_id,charged_to_org_id,submitted_by) VALUES(1,'private','completed',1,1,:uid),(2,'public','completed',NULL,1,:uid)", uid=uid)
for job in (1,2):
 sql("INSERT INTO traceability_records(job_id,attempt,snapshot,snapshot_sha256,captured_at,inspection_notes,inspected_by) VALUES(:job,1,:snap,:hash,'2026-11-01',:notes,:uid)", job=job, snap=json.dumps({'file':{'name':f'snapshot-secret-{job}'}}), hash='a'*64, notes=f'inspection-secret-{job}', uid=uid)
body = dict(audience_id=1,printer_tag='Secure',retention_days=7,reason='personal free text without username')
assert member.put('/api/controlled-parts/files/1',json=body).status_code == 200
# A second author captures the erased author and reason in the before snapshot.
sql('INSERT INTO controlled_audience_members(audience_id,user_id) VALUES(1,:uid)',uid=sid)
body['reason']='other author reason'
assert superadmin.put('/api/controlled-parts/files/1',json=body).status_code == 200
r = outsider.get(f'/api/users/{uid}/export'); assert r.status_code == 200, r.text
assert 'snapshot-secret-1' not in r.text and 'inspection-secret-1' not in r.text
assert 'snapshot-secret-2' in r.text and 'inspection-secret-2' in r.text
r = member.get(f'/api/users/{uid}/export'); assert r.status_code == 200, r.text
assert 'snapshot-secret-1' in r.text and 'inspection-secret-1' in r.text
r = outsider.delete(f'/api/users/{uid}/erase'); assert r.status_code == 200, r.text
entries = sql("SELECT details FROM audit_logs WHERE action='controlled_part.changed' ORDER BY id")
first, second = [json.loads(row[0]) for row in entries]
assert first['after']['reason']=='[deleted]'
assert second['before']['changed_by'] is None and second['before']['reason']=='[deleted]'
assert second['after']['reason']=='other author reason'
from core.audit_integrity import verify
from core.db import engine
with engine.connect() as c:
 verdict=verify(c)
 assert verdict['ok'], verdict
OUT['ok'] = True
''', tmp_path)
    assert result['ok'] and result['_net_attempts'] == []


def test_signed_export_and_siem_both_directions(tmp_path):
    result = run_probe('controlled-r19-siem', r'''
install_license('enterprise')
sid=seed_user('super','admin'); uid=seed_user('member','operator')
superadmin=login('super'); member=login('member')
import license_manager as lm
lm.get_installation_id(); lm.get_device_keypair()
sql("INSERT INTO controlled_audiences(id,name) VALUES(1,'Restricted')")
for user in (sid,uid):sql('INSERT INTO controlled_audience_members(audience_id,user_id) VALUES(1,:uid)',uid=user)
sql("INSERT INTO print_files(id,filename) VALUES(1,'private.gcode')")
body=dict(audience_id=1,printer_tag='Secure',retention_days=7,reason='secret-mark')
destination=dict(name='SOC',kind='syslog',endpoint='syslog://soc.example:6514',enabled=True)
# Destination first: marking must require acknowledgement and superadmin authority.
r=superadmin.put('/api/settings/siem/new',json=destination); assert r.status_code==200,r.text
did=r.json()['id']
assert member.put('/api/controlled-parts/files/1',json=body).json()['detail']['code']=='controlled_part_siem_acknowledgement_required'
assert member.put('/api/controlled-parts/files/1',json={**body,'acknowledge_siem':True}).json()['detail']['code']=='controlled_part_superadmin_required'
assert not sql('SELECT 1 FROM controlled_files')
r=superadmin.put('/api/controlled-parts/files/1',json={**body,'acknowledge_siem':True}); assert r.status_code==200,r.text
# Marks first: creating/enabling/repointing receivers also requires acknowledgement.
r=superadmin.put('/api/settings/siem/new',json=destination); assert r.status_code==403,r.text
r=superadmin.put('/api/settings/siem/'+did,json={**destination,'enabled':False}); assert r.status_code==200,r.text
r=superadmin.put('/api/settings/siem/'+did,json=destination); assert r.status_code==403,r.text
r=superadmin.put('/api/settings/siem/'+did,json={**destination,'acknowledge_controlled':True}); assert r.status_code==200,r.text
assert len(sql("SELECT id FROM audit_logs WHERE action='controlled_part.siem_acknowledged'"))==2
# Even a superadmin outside the audience can export only by explicit POST ack.
sql('DELETE FROM controlled_audience_members WHERE user_id=:uid',uid=sid)
query='?date_from=2000-01-01&date_to=2099-01-01'
path='/api/export/audit-logs/jsonl'
assert superadmin.get(path+query).status_code==403
assert superadmin.get(path+query+'&acknowledge_controlled=true').status_code==403
assert superadmin.post(path+'/acknowledge'+query).status_code==403
r=superadmin.post(path+'/acknowledge'+query,headers={'X-ODIN-Controlled-Ack':'true'})
assert r.status_code==200,r.text
assert 'secret-mark' in r.text
assert any(json.loads(row[0]).get('kind')=='audit_jsonl' for row in sql("SELECT details FROM audit_logs WHERE action='controlled_part.privileged_export'"))
OUT['ok']=True
''', tmp_path)
    assert result['ok'] and result['_net_attempts'] == []


def test_traceability_refuses_before_any_mutation_lock(database, tier_license, monkeypatch):
    from modules.organizations.routes_controlled_parts import change_mark
    from core.traceability import owned_job
    from core.registry import registry
    from unittest.mock import Mock
    monkeypatch.setattr(registry, 'get_provider', lambda name: Mock(spec=['assert_generic_job_mutation_allowed']))
    tier_license('enterprise')
    change_mark(database, 'files', 1, mark(), actor())
    writes = []
    def capture(conn, cursor, statement, *args):
        if statement.upper().startswith('UPDATE'):
            writes.append(statement)
    event.listen(database.get_bind(), 'before_cursor_execute', capture)
    try:
        with pytest.raises(HTTPException) as error:
            owned_job(database, 1, actor(2), mutate=True)
        assert error.value.detail['code'] == 'controlled_part_group_required'
        assert not any(s.startswith(('UPDATE users ', 'UPDATE jobs ', 'UPDATE groups ')) for s in writes)
    finally:
        event.remove(database.get_bind(), 'before_cursor_execute', capture)


def test_unmarked_exports_and_rejected_uploads_never_audit_acceptance(tmp_path):
    result = run_probe('controlled-r19-restore', r'''
install_license('enterprise')
sid=seed_user('super','admin'); c=login('super')
from modules.system import routes_backup as routes
backup=WORK/'odin_backup_owned.db'; backup.write_bytes(b'owned')
routes._safe_backup_path=lambda name:backup
assert c.get('/api/backups/odin_backup_owned.db').status_code==200
assert not sql("SELECT 1 FROM audit_logs WHERE action='controlled_part.privileged_export'")
sql("INSERT INTO controlled_audiences(id,name) VALUES(1,'Restricted')")
sql("INSERT INTO print_files(id,filename) VALUES(1,'private.gcode')")
sql("INSERT INTO controlled_files(file_id,label,audience_id,printer_tag,retention_days,reason,marked_at) VALUES(1,'CUI',1,'Secure',7,'private','2026-11-01')")
headers={'X-ODIN-Controlled-Ack':'true'}
for name in ('invalid.txt','invalid.db'):
 r=c.post('/api/backups/restore?acknowledge_controlled=true',files={'file':(name,b'invalid')},headers=headers)
 assert r.status_code==400,r.text
assert not sql("SELECT 1 FROM audit_logs WHERE action='controlled_part.privileged_export'")
routes.stage_restore=lambda *a,**k:{'candidate_sha256':'accepted'}
r=c.post('/api/backups/restore?acknowledge_controlled=true',files={'file':('owned.db',b'owned')},headers=headers)
assert r.status_code==200,r.text
assert [json.loads(r[0])['kind'] for r in sql("SELECT details FROM audit_logs WHERE action='controlled_part.privileged_export'")]==['restore']
# Listed PostgreSQL staging uses the same post-validation acceptance boundary.
from types import SimpleNamespace
routes.sql=SimpleNamespace(is_postgres=True)
routes._postgres_maintenance_config=lambda:('owned-maintenance',None,None)
def refused_stage(*a,**k): raise routes.BackupValidationError('owned validation refusal')
routes.stage_postgres_restore=refused_stage
r=c.post('/api/backups/odin_backup_owned.db/stage?acknowledge_controlled=true',headers=headers)
assert r.status_code==400,r.text
assert len(sql("SELECT 1 FROM audit_logs WHERE action='controlled_part.privileged_export'"))==1
routes.stage_postgres_restore=lambda *a,**k:{'candidate_sha256':'accepted'}
r=c.post('/api/backups/odin_backup_owned.db/stage?acknowledge_controlled=true',headers=headers)
assert r.status_code==200,r.text
assert [json.loads(r[0])['kind'] for r in sql("SELECT details FROM audit_logs WHERE action='controlled_part.privileged_export' ORDER BY id")]==['restore','restore_stage']
OUT['ok']=True
''', tmp_path)
    assert result['ok'] and result['_net_attempts'] == []


def test_siem_policy_mutex_keeps_job_before_audit_lock_order(database, tier_license):
    from modules.organizations.routes_controlled_parts import change_mark
    from modules.system.routes_audit_integrity import save_destination, Destination
    tier_license('enterprise')
    statements = []
    def capture(conn, cursor, statement, *args):
        statements.append(statement)
    event.listen(database.get_bind(), 'before_cursor_execute', capture)
    try:
        change_mark(database, 'files', 1, mark(), actor())
        policy = next(i for i, s in enumerate(statements) if s.startswith('UPDATE system_config'))
        resource = next(i for i, s in enumerate(statements) if s.startswith('UPDATE print_files'))
        assert policy < resource
        assert not any(s.startswith('UPDATE audit_chain_state') for s in statements[:resource])
        statements.clear()
        save_destination('new', Destination(name='SOC', kind='syslog', endpoint='syslog://soc.example:6514'), actor(2), database)
        policy = next(i for i, s in enumerate(statements) if s.startswith('UPDATE system_config'))
        chain = next(i for i, s in enumerate(statements) if s.startswith('UPDATE audit_chain_state'))
        assert policy < chain
    finally:
        event.remove(database.get_bind(), 'before_cursor_execute', capture)


def test_controlled_erasure_receipt_rejects_policy_rewrites():
    from core.audit_receipts import redaction
    original = {'before': {'changed_by': 7, 'reason': 'private reason', 'audience_id': 3},
                'after': {'reason': 'another author', 'audience_id': 3}}
    before = dict(actor_user_id=8, actor_username='other', actor_auth_kind='session_jwt',
                  ip_address=None, action='controlled_part.changed', entity_type='file',
                  entity_id=1, details=json.dumps(original))
    erased = {'before': {'changed_by': None, 'reason': '[deleted]', 'audience_id': 3},
              'after': original['after']}
    assert redaction(before, {**before, 'details': json.dumps(erased)})
    erased['before']['audience_id'] = 99
    assert not redaction(before, {**before, 'details': json.dumps(erased)})
