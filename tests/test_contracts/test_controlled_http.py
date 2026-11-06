"""Real routes: parsing, camera, exports, backup acknowledgement, source changes."""
from tests.test_contracts.test_cert_G_harness import run_probe


def test_review_http_boundaries(tmp_path):
    result = run_probe('controlled-review-http', r'''
install_license('enterprise', features=['controlled_parts'])
sql("INSERT INTO groups(id,name,is_org) VALUES(1,'Restricted',1)")
uid = seed_user('member', 'operator', 1)
super_id = seed_user('outsider', 'admin')
member = login('member'); outsider = login('outsider')
a = outsider.post('/api/controlled-parts/audiences', json={'name':'Restricted audience','user_ids':[uid]})
assert a.status_code == 200, a.text
audience = a.json()['id']
sql("INSERT INTO printers(id,name,is_active,camera_enabled,camera_url,org_id,slot_count,tags,timelapse_enabled,shared) VALUES(1,'Visible printer',1,1,'rtsp://127.0.0.1/cam',1,4,'[]',0,0)")
sql("INSERT INTO models(id,name,org_id) VALUES(1,'secret-model',1)")
sql("INSERT INTO print_files(id,filename,model_id,org_id,stored_path) VALUES(1,'secret-file.3mf',1,1,'/controlled-fixture.3mf')")
sql("INSERT INTO jobs(id,item_name,print_file_id,model_id,charged_to_org_id,status) VALUES(1,'secret-job',1,1,1,'pending')")
body = dict(audience_id=audience, printer_tag='Secure', retention_days=1, reason='secret reason')
assert member.put('/api/controlled-parts/files/1', json=body).status_code == 200
# Oddly-cased and structured JSON is parsed exactly like FastAPI. A nested
# extra field is deliberately absent from JobCreate.model_dump(), so only
# the body-reference guard can catch it before handler validation.
for content_type in ['Application/JSON; charset=utf-8','application/problem+json']:
 r = outsider.post('/api/jobs', content=json.dumps({'item_name':'public', 'nested':{'model_id':1}}), headers={'content-type':content_type})
 assert r.status_code == 403, (content_type, r.status_code, r.text)
assert outsider.get('/api/jobs/1').status_code == 403
assert member.patch('/api/jobs/1', json={'model_id':None}).status_code == 403
# Exports cannot leak names, but audit metadata remains visible.
r = outsider.get('/api/export/jobs'); assert r.status_code == 200, r.text
assert 'secret-job' not in r.text
r = outsider.get('/api/audit-logs'); assert r.status_code == 200, r.text
assert 'controlled_part.changed' in r.text and 'secret reason' not in r.text
sql("UPDATE jobs SET status='printing',printer_id=1 WHERE id=1")
assert outsider.get('/api/printers/1').status_code == 200
assert any(p['id']==1 for p in outsider.get('/api/printers').json())
assert outsider.get('/api/cameras/1/stream').status_code == 403
assert member.post('/api/cameras/1/webrtc', content='sdp').status_code == 403
# A real file download through the backup handler, with only path discovery
# redirected to an owned fixture; bytes and acknowledgement are checked.
from modules.system import routes_backup
backup = WORK/'odin_backup_fixture.db'; backup.write_bytes(b'owned-backup')
routes_backup._safe_backup_path = lambda filename: backup
assert outsider.get('/api/backups/odin_backup_fixture.db').status_code == 403
r = outsider.post('/api/backups/odin_backup_fixture.db/acknowledge', headers={'X-ODIN-Controlled-Ack':'true'})
assert r.status_code == 200 and r.content == b'owned-backup', r.text
assert outsider.get('/api/admin/support-bundle').status_code == 403
r = outsider.post('/api/admin/support-bundle/acknowledge', headers={'X-ODIN-Controlled-Ack':'true'})
assert r.status_code == 200, r.text
import io, zipfile
with zipfile.ZipFile(io.BytesIO(r.content)) as bundle:
 assert all(b'secret-job' not in bundle.read(n) and b'secret reason' not in bundle.read(n) for n in bundle.namelist())
OUT['acknowledgements'] = sql("SELECT count(*) FROM audit_logs WHERE action='controlled_part.privileged_export'")[0][0]
''', tmp_path)
    assert result['acknowledgements'] == 2
    assert result['_net_attempts'] == []
