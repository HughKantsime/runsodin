"""A ws token for a deleted account never authorizes a recreated username (real /ws route)."""
import os,subprocess,sys,textwrap
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]

PROBE=textwrap.dedent('''
import os,sys
from datetime import datetime,timedelta,timezone
from pathlib import Path
from sqlalchemy import create_engine,text
from core.schema import bootstrap_database
engine=create_engine(os.environ['DATABASE_URL'])
bootstrap_database(engine,Path('.'))
from core import auth
with engine.begin() as c:
    c.execute(text("INSERT INTO users(id,username,password_hash,role,is_active,created_at) VALUES(40,'reused','x','viewer',1,:t)"),{'t':(datetime.now(timezone.utc)-timedelta(minutes=10)).strftime('%Y-%m-%d %H:%M:%S')})
def ws_token(uid):
    return auth.create_access_token({'sub':'reused','ws':True,'user_id':uid,'capability_snapshot_id':'probe'},timedelta(minutes=5))
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from core.app import create_app
client=TestClient(create_app())
def opens(token):
    try:
        with client.websocket_connect('/api/v1/ws?token='+token) as ws:
            ws.send_text('ping');return ws.receive_text()=='pong'
    except WebSocketDisconnect:
        return False
old=ws_token(40)
assert opens(old),'baseline ws token must connect'
with engine.begin() as c:
    c.execute(text('DELETE FROM users WHERE id=40'))
    c.execute(text("INSERT INTO users(id,username,password_hash,role,is_active,created_at) VALUES(41,'reused','x','admin',1,:t)"),{'t':datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')})
assert not opens(old),'old ws token bound to recreated account'
print('stale-iat-refused')
forged=auth.create_access_token({'sub':'reused','ws':True,'user_id':40,'capability_snapshot_id':'probe'},timedelta(minutes=5))
assert not opens(forged),'ws token for another account id accepted'
print('user-id-mismatch-refused')
assert opens(ws_token(41))
with engine.begin() as c:
    c.execute(text("INSERT INTO token_blacklist(jti,expires_at) VALUES('revoked-ws',:e)"),{'e':datetime.now(timezone.utc)+timedelta(hours=1)})
import jwt
revoked=jwt.encode({'sub':'reused','ws':True,'user_id':41,'capability_snapshot_id':'probe','jti':'revoked-ws','iat':datetime.now(timezone.utc),'exp':datetime.now(timezone.utc)+timedelta(minutes=5)},auth.SECRET_KEY,algorithm=auth.ALGORITHM)
assert not opens(revoked)
print('ws-binding-pass')
''')


def test_ws_token_binding(tmp_path):
    env={**os.environ,'DATABASE_URL':f'sqlite:///{tmp_path}/ws.db','JWT_SECRET_KEY':'ws-binding-test-key-only','PYTHONPATH':str(ROOT/'backend')}
    env.pop('API_KEY',None)
    result=subprocess.run([sys.executable,'-c',PROBE],cwd=ROOT/'backend',env=env,capture_output=True,text=True,timeout=180)
    assert result.returncode==0,result.stdout[-3000:]+result.stderr[-3000:]
    assert 'ws-binding-pass' in result.stdout
