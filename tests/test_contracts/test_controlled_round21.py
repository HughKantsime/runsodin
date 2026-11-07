"""Atomic receiver rotation and GDPR ownership of standing SIEM trust."""
import pytest
from tests.test_contracts.test_cert_G_harness import run_probe

SETUP = r'''
install_license('enterprise')
sid=seed_user('super','admin'); uid=seed_user('member','operator')
c=login('super'); member=login('member')
headers={'X-ODIN-Controlled-Ack':'true'}
sql("INSERT INTO controlled_audiences(id,name) VALUES(1,'Restricted')")
sql('INSERT INTO controlled_audience_members(audience_id,user_id) VALUES(1,:uid)',uid=uid)
sql("INSERT INTO print_files(id,filename) VALUES(1,'private.gcode')")
mark=dict(audience_id=1,printer_tag='Secure',retention_days=7,reason='restriction')
assert member.put('/api/controlled-parts/files/1',json=mark).status_code==200
dest=dict(name='SOC',kind='webhook',endpoint='https://soc.example/events',secret='fixture-only-signing-secret-00000000',enabled=False)
r=c.put('/api/settings/siem/new',json=dest); assert r.status_code==200,r.text
did=r.json()['id']; path='/api/settings/siem/'+did
'''


def test_refused_enable_preserves_license_deadline(tmp_path):
    result = run_probe('controlled-r21-refusal', SETUP + r'''
sql('UPDATE audit_chain_state SET siem_until=123 WHERE id=1')
before=sql('SELECT * FROM audit_siem_destinations')
r=c.put(path,json={**dest,'enabled':True})
assert r.status_code==403,r.text
assert sql('SELECT siem_until FROM audit_chain_state')==[[123.0]], 'refusal committed staged SIEM deadline'
assert sql('SELECT * FROM audit_siem_destinations')==before
assert not sql("SELECT value FROM system_config WHERE key=:k",k='controlled:siem_ack:'+did)
assert sql("SELECT 1 FROM audit_logs WHERE action='controlled_part.refused'")
OUT['ok']=True
''', tmp_path)
    assert result['ok'] and result['_net_attempts'] == []


def test_standing_ack_gdpr_export_and_erasure(tmp_path):
    result = run_probe('controlled-r21-privacy', SETUP + r'''
other=seed_user('other','admin'); other_client=login('other')
assert c.post(path+'/acknowledge',headers=headers).status_code==200
saved=json.loads(sql('SELECT value FROM system_config WHERE key=:k',k='controlled:siem_ack:'+did)[0][0])
r=other_client.get(f'/api/users/{sid}/export'); assert r.status_code==200,r.text
assert r.json().get('controlled_siem_acknowledgements')==[dict(destination_id=did,user_id=sid,acknowledged_at=saved['acknowledged_at'])], 'standing acknowledgement omitted from export'
assert other_client.get(f'/api/users/{other}/export').json()['controlled_siem_acknowledgements']==[]
r=other_client.delete(f'/api/users/{sid}/erase'); assert r.status_code==200,r.text
scrubbed=json.loads(sql('SELECT value FROM system_config WHERE key=:k',k='controlled:siem_ack:'+did)[0][0])
assert scrubbed=={**saved,'user_id':None}, 'erasure must preserve trust and remove its author'
listed=other_client.get('/api/settings/siem').json()[0]
assert listed['controlled_acknowledgement']==dict(user_id=None,acknowledged_at=saved['acknowledged_at'])
assert other_client.get(f'/api/users/{sid}/export').json()['controlled_siem_acknowledgements']==[]
assert other_client.put(path,json={**dest,'secret':None,'enabled':True}).status_code==200
from core.audit_integrity import verify
from core.db import engine
with engine.connect() as connection:
 verdict=verify(connection); assert verdict['ok'],verdict
OUT['ok']=True
''', tmp_path)
    assert result['ok'] and result['_net_attempts'] == []


@pytest.mark.parametrize('rotation', ['secret', 'endpoint', 'ca_bundle'])
def test_enabled_rotation_is_atomic_and_acknowledges_new_target(tmp_path, rotation):
    result = run_probe('controlled-r21-rotate', SETUP + r'''
assert c.post(path+'/acknowledge',headers=headers).status_code==200
assert c.put(path,json={**dest,'secret':None,'enabled':True}).status_code==200
sql('DELETE FROM audit_siem_outbox')
import ssl
changes={'secret':'fixture-only-replacement-secret-0000', 'endpoint':'https://new.example/events',
 'ca_bundle':ssl.DER_cert_to_PEM_cert(ssl.create_default_context(cafile=__import__('certifi').where()).get_ca_certs(binary_form=True)[0])}
rotated={**dest,'secret':None,'enabled':True,ROTATION:changes[ROTATION]}
before=sql('SELECT * FROM audit_siem_destinations')
ack_before=sql('SELECT value FROM system_config WHERE key=:k',k='controlled:siem_ack:'+did)
sql('UPDATE audit_chain_state SET siem_until=4102444800 WHERE id=1')
r=c.put(path,json=rotated); assert r.status_code==403,r.text
assert sql('SELECT * FROM audit_siem_destinations')==before
assert sql('SELECT value FROM system_config WHERE key=:k',k='controlled:siem_ack:'+did)==ack_before
assert sql('SELECT siem_until FROM audit_chain_state')==[[4102444800.0]]
sql('DELETE FROM audit_siem_outbox')
# Observe every committed destination update: rotation must never disable it.
from core.db import engine
from sqlalchemy import event
states=[]
def after_commit(connection):
 from sqlalchemy import text
 # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- constant test observation; no external input
 states.append(connection.execute(text('SELECT enabled FROM audit_siem_destinations')).scalar())
event.listen(engine,'commit',after_commit)
r=c.put(path,json={**rotated,'acknowledge_controlled':True},headers=headers)
event.remove(engine,'commit',after_commit)
assert r.status_code==200,r.text
assert states and all(value==1 for value in states),states
from core.db import SessionLocal
from core.controlled_siem import acknowledgement, target
from sqlalchemy import text
with SessionLocal() as db:
 # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- constant fixture read without external input
 row=db.execute(text('SELECT * FROM audit_siem_destinations')).mappings().one()
 assert acknowledgement(db,row)['user_id']==sid
 stored=json.loads(sql('SELECT value FROM system_config WHERE key=:k',k='controlled:siem_ack:'+did)[0][0])
 assert stored['target']==target(row) and stored['target']!=json.loads(ack_before[0][0])['target']
assert len(sql("SELECT 1 FROM audit_logs WHERE action='controlled_part.siem_acknowledged'"))==2
assert not sql("SELECT 1 FROM audit_logs WHERE action='siem.gap'")
assert sql("SELECT 1 FROM audit_siem_outbox o JOIN audit_logs a ON a.id=o.audit_id WHERE a.action='controlled_part.siem_acknowledged'")
OUT['ok']=True
'''.replace('ROTATION', repr(rotation)), tmp_path)
    assert result['ok'] and result['_net_attempts'] == []


def test_save_ack_requires_interactive_superadmin_and_csrf(tmp_path):
    result = run_probe('controlled-r21-auth', SETUP + r'''
from core.auth import hash_password
from fastapi.testclient import TestClient
raw='odin_round21_test_token_only'
sql("INSERT INTO api_tokens(user_id,name,token_hash,token_prefix,scopes) VALUES(:u,'test',:h,:p,:s)",u=sid,h=hash_password(raw),p=raw[:10],s='["admin"]')
token=TestClient(app,raise_server_exceptions=False)
sql("INSERT INTO groups(id,name) VALUES(1,'Org')")
seed_user('orgadmin','admin',1); orgadmin=login('orgadmin')
before=sql('SELECT * FROM audit_siem_destinations')
body={**dest,'enabled':True,'acknowledge_controlled':True}
for client,hs in [(c,{}),(c,{**headers,'Sec-Fetch-Site':'cross-site'}),
 (c,{**headers,'Origin':'https://foreign.example'}),
 (token,{**headers,'X-API-Key':raw}),(member,headers),(orgadmin,headers)]:
 r=client.put(path,json=body,headers=hs); assert r.status_code==403,(r.status_code,r.text)
 assert sql('SELECT * FROM audit_siem_destinations')==before
 assert not sql('SELECT value FROM system_config WHERE key=:k',k='controlled:siem_ack:'+did)
assert not sql("SELECT 1 FROM audit_logs WHERE action='controlled_part.siem_acknowledged'")
# A header alone does not grant trust, and explicit consent can enable on creation.
r=c.put(path,json={**dest,'enabled':True},headers=headers); assert r.status_code==403,r.text
r=c.put('/api/settings/siem/new',json=body,headers=headers); assert r.status_code==200,r.text
new_id=r.json()['id']
row=next(row for row in c.get('/api/settings/siem').json() if row['id']==new_id)
assert row['enabled']==1 and row['controlled_acknowledgement']['user_id']==sid
OUT['ok']=True
''', tmp_path)
    assert result['ok'] and result['_net_attempts'] == []


def test_save_ack_rolls_back_on_audit_failure_and_preserves_backlog_guard(tmp_path):
    result = run_probe('controlled-r21-atomic', SETUP + r'''
assert c.post(path+'/acknowledge',headers=headers).status_code==200
assert c.put(path,json={**dest,'secret':None,'enabled':True}).status_code==200
before=sql('SELECT * FROM audit_siem_destinations')
ack_before=sql('SELECT value FROM system_config WHERE key=:k',k='controlled:siem_ack:'+did)
audits_before=sql("SELECT id FROM audit_logs WHERE action IN ('controlled_part.siem_acknowledged','siem.configured')")
rotated={**dest,'secret':None,'endpoint':'https://new.example/events','enabled':True,'acknowledge_controlled':True}
r=c.put(path,json=rotated,headers=headers); assert r.status_code==409,r.text
assert sql('SELECT * FROM audit_siem_destinations')==before
assert sql('SELECT value FROM system_config WHERE key=:k',k='controlled:siem_ack:'+did)==ack_before
sql('DELETE FROM audit_siem_outbox')
sql('UPDATE audit_chain_state SET siem_until=123 WHERE id=1')
from modules.system import routes_audit_integrity as routes
original=routes.log_audit
def fail_after_staging(*args,**kwargs):
 original(*args,**kwargs)
 if args[1]=='controlled_part.siem_acknowledged': raise RuntimeError('Owned rotation audit failure')
routes.log_audit=fail_after_staging
r=c.put(path,json=rotated,headers=headers); assert r.status_code==500,r.text
routes.log_audit=original
assert sql('SELECT * FROM audit_siem_destinations')==before
assert sql('SELECT value FROM system_config WHERE key=:k',k='controlled:siem_ack:'+did)==ack_before
assert sql('SELECT siem_until FROM audit_chain_state')==[[123.0]]
assert sql("SELECT id FROM audit_logs WHERE action IN ('controlled_part.siem_acknowledged','siem.configured')")==audits_before
assert not sql('SELECT 1 FROM audit_siem_outbox')
OUT['ok']=True
''', tmp_path)
    assert result['ok'] and result['_net_attempts'] == []


from tests.test_contracts.test_controlled_round8 import clock_db, native_pg


def test_standing_ack_privacy_preserves_other_authors_and_rolls_back(clock_db):
    from core.controlled_siem import store_acknowledgement, acknowledgement
    from core.controlled_siem_privacy import export_acknowledgements, erase_acknowledgements
    destination = dict(id='a' * 32, endpoint='syslog://soc.example:6514', kind='syslog',
                       tls=1, secret=None, ca_bundle=None)
    other = {**destination, 'id': 'b' * 32}
    saved = store_acknowledgement(clock_db, destination, {'id': 7})
    other_saved = store_acknowledgement(clock_db, other, {'id': 8})
    clock_db.commit()
    assert export_acknowledgements(clock_db, 7) == [{'destination_id': destination['id'], **saved}]
    erase_acknowledgements(clock_db, 7)
    assert acknowledgement(clock_db, destination) == {**saved, 'user_id': None}
    assert acknowledgement(clock_db, other) == other_saved
    clock_db.rollback()
    assert acknowledgement(clock_db, destination) == saved
    erase_acknowledgements(clock_db, 7)
    clock_db.commit()
    assert export_acknowledgements(clock_db, 7) == []
    assert acknowledgement(clock_db, destination) == {**saved, 'user_id': None}
    assert acknowledgement(clock_db, other) == other_saved
