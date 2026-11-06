"""Real middleware, replay, audit list/CSV and management credential boundaries."""
from tests.test_contracts.test_cert_G_harness import run_probe


def test_response_middleware_replays_and_audit_exports(tmp_path):
    result=run_probe('controlled-round2-http',r'''
install_license('enterprise', features=['controlled_parts','audit_export'])
uid=seed_user('member','operator')
sid=seed_user('super','admin')
sql("INSERT INTO groups(id,name,is_org) VALUES(1,'Tenant',1)")
seed_user('tenant-admin','admin',1)
member=login('member');superadmin=login('super');tenant=login('tenant-admin')
from core.auth import hash_password
from sqlalchemy import text
raw='odin_round2_scope_refusal'
with engine.begin() as c:
 c.execute(text("INSERT INTO api_tokens(user_id,name,token_hash,token_prefix,scopes) VALUES(:u,'narrow',:h,:p,:s)"),
           {'u':sid,'h':hash_password(raw),'p':raw[:10],'s':'["read:analytics"]'})
from fastapi.testclient import TestClient
scoped=TestClient(app,raise_server_exceptions=False)
for method,path in [('GET','/api/auth/me/theme'),('POST','/api/auth/logout')]:
 refusal=scoped.request(method,path,headers={'X-API-Key':raw})
 assert refusal.status_code in (401,403),(path,refusal.status_code,refusal.text)
sql("INSERT INTO controlled_audiences(id,name) VALUES(1,'Secure')")
sql(f"INSERT INTO controlled_audience_members(audience_id,user_id) VALUES(1,{uid})")
sql("INSERT INTO jobs(id,item_name,notes,status) VALUES(1,'same-name','private-note','pending'),(2,'same-name','private-note','pending')")
body=dict(audience_id=1,printer_tag='Secure',retention_days=7,reason='private reason')
assert member.put('/api/controlled-parts/jobs/1',json=body).status_code==200
assert tenant.post('/api/controlled-parts/audiences',json={'name':'no','user_ids':[]}).status_code==403
assert member.get('/api/controlled-parts/audiences/1').status_code==403
assert superadmin.get('/api/controlled-parts/audiences/1').status_code==200
# The real response middleware receives known resource IDs, without a route-local redactor.
from fastapi import Depends
from core.dependencies import get_current_user
@app.post('/api/jobs/round2-projection')
def projection(actor=Depends(get_current_user)):
 return {'jobs':[{'id':1,'item_name':'same-name','notes':'private-note'}, {'id':2,'item_name':'same-name','notes':'private-note'}]}
@app.get('/api/round2-empty')
def empty(actor=Depends(get_current_user)):
 from starlette.responses import Response
 return Response(b'',media_type='application/json')
headers={'Idempotency-Key':'controlled-replay-regression'}
r=member.post('/api/jobs/round2-projection',json={},headers=headers)
assert r.status_code==200 and r.json()['jobs'][0]['item_name']=='same-name',r.text
sql(f'DELETE FROM controlled_audience_members WHERE user_id={uid}')
r=member.post('/api/jobs/round2-projection',json={},headers=headers)
assert r.status_code==200,r.text
assert r.json()['jobs'][0]['item_name']=='[controlled]',r.text
assert r.json()['jobs'][1]['item_name']=='same-name',r.text
assert member.get('/api/round2-empty').status_code==200
# Details remain visible to members only. Search must not become a name oracle.
for path in ['/api/audit-logs','/api/export/audit-logs']:
 r=superadmin.get(path)
 assert r.status_code==200 and 'private reason' not in r.text and '[controlled]' in r.text,(path,r.text)
 r=superadmin.get(path,params={'q':'private reason'})
 assert r.status_code==200 and 'controlled_part.changed' not in r.text,(path,r.text)
assert superadmin.get('/api/admin/support-bundle?acknowledge_controlled=true').status_code==403
assert superadmin.post('/api/admin/support-bundle/acknowledge').status_code==403
assert superadmin.post('/api/admin/support-bundle/acknowledge',headers={'X-ODIN-Controlled-Ack':'true','Origin':'https://attacker.invalid'}).status_code==403
install_license('pro')
for path in ['/api/controlled-parts/audiences','/api/controlled-parts/audiences/1','/api/controlled-parts/retention/preview']:
 assert superadmin.get(path).status_code==403,path
assert superadmin.delete('/api/controlled-parts/audiences/1').status_code==403
assert superadmin.post('/api/controlled-parts/retention/acknowledge',json={'jobs':[1]},headers={'X-ODIN-Controlled-Ack':'true'}).status_code==403
OUT['checked']=True
''',tmp_path)
    assert result['checked'] and result['_net_attempts']==[]
