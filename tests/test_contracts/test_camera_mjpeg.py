"""Owned MJPEG acceptance; synthetic cameras and DB only, no hardware traffic."""
import asyncio
import gc
import json
import secrets
import threading
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi import HTTPException, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401
from tests.test_contracts.test_cert_G_harness import run_probe
from modules.printers import mjpeg_relay as relay, routes_camera_snapshot as route

FRAME = b'--frame\r\nContent-Type: image/jpeg\r\n\r\n\xff\xd8\xfffixture\r\n'
CONTENT_TYPE = 'multipart/x-mixed-replace; boundary=frame'
REAL_CLIENT = httpx.AsyncClient


class Camera(httpx.AsyncByteStream):
    def __init__(self, stall=False):
        self.stall = stall
        self.closes = 0

    async def __aiter__(self):
        yield FRAME
        if self.stall:
            await asyncio.Event().wait()

    async def aclose(self):
        self.closes += 1


@pytest.fixture
def upstream(monkeypatch, review_db):
    from core import auth
    monkeypatch.setattr(auth, 'SECRET_KEY', 'synthetic-mjpeg-test-signing-key-only')
    review_db.execute(text("INSERT INTO printers (id,name,api_type,camera_url,camera_enabled,is_active,org_id,shared) VALUES (901,'MJPEG fixture','moonraker','rtsp://192.0.2.9/camera',1,1,1,0)"))
    review_db.execute(text("UPDATE users SET role='viewer' WHERE id=1"))
    review_db.commit()
    state = {'known': True, 'status': 200, 'type': CONTENT_TYPE, 'stall': False,
             'calls': [], 'cameras': [], 'clients': [], 'sync_threads': []}

    def transport(request):
        state['calls'].append(request.url.path)
        if state.get('unreachable'):
            raise httpx.ConnectError('fixture offline')
        if request.url.path == '/api/streams':
            return httpx.Response(200, json={'printer_901_mjpeg': {}} if state['known'] else {})
        assert request.url.path == '/api/stream.mjpeg'
        if state.get('open_error'):
            raise httpx.ConnectError('fixture stream offline')
        assert request.url.params['src'] == 'printer_901_mjpeg'
        camera = Camera(state['stall'])
        state['cameras'].append(camera)
        return httpx.Response(state['status'], headers={'content-type': state['type']}, stream=camera)

    def client(**kwargs):
        assert kwargs['trust_env'] is False
        instance = REAL_CLIENT(transport=httpx.MockTransport(transport), **kwargs)
        state['clients'].append(instance)
        return instance

    monkeypatch.setattr(relay.httpx, 'AsyncClient', client)
    from modules.printers import routes
    monkeypatch.setattr(routes, 'sync_go2rtc_config', lambda db: state['sync_threads'].append(threading.get_ident()))
    monkeypatch.setattr(route, 'MJPEG_REVALIDATE_SECONDS', 0.03)
    assert not relay.STREAMS
    yield state
    assert not relay.STREAMS
    assert all(c.is_closed for c in state['clients'])
    assert all(c.closes == 1 for c in state['cameras'])


def jwt_request(db, user=1, cookie=False):
    from core.auth import create_access_token
    row = db.execute(text('SELECT username,role FROM users WHERE id=:id'), {'id': user}).one()
    raw = create_access_token({'sub': row.username, 'role': row.role})
    header = (b'cookie', f'session={raw}'.encode()) if cookie else (b'authorization', f'Bearer {raw}'.encode())
    return Request({'type': 'http', 'method': 'GET', 'path': '/api/cameras/901/mjpeg', 'headers': [header]})


async def open_stream(db, user=1, request=None, request_db=None):
    from core.dependencies import get_current_user, oauth2_scheme
    request = request if request is not None else jwt_request(db, user)
    request_db = request_db if request_db is not None else Session(db.get_bind())
    token = await oauth2_scheme(request)
    # Give admission its own request context: subsequent admin mutations must
    # not inherit the API caller's credential-write guard.
    principal = await asyncio.create_task(get_current_user(request, token=token, db=request_db))
    assert principal is not None
    return await route.camera_mjpeg(901, request=request, db=request_db, token=token, user=principal)


def test_stream_probe_sync_and_close_without_retaining_request_session(upstream, review_db):
    upstream['known'] = False

    class RequestSession(Session):
        def close(self):
            self.closed = True
            super().close()

    async def run():
        db = RequestSession(review_db.get_bind())
        response = await open_stream(review_db, request_db=db)
        assert db.closed and not db.in_transaction()
        assert response.headers['content-type'] == CONTENT_TYPE
        assert response.headers['cache-control'] == 'no-store'
        assert [chunk async for chunk in response.body_iterator] == [FRAME]
    asyncio.run(run())
    assert upstream['sync_threads'] and threading.get_ident() not in upstream['sync_threads']


@pytest.mark.parametrize('field,value', [('status', 503), ('type', 'text/html'), ('type', 'image/jpeg'), ('unreachable', True), ('open_error', True)])
def test_upstream_refusal_releases_slot_and_closes(upstream, review_db, field, value):
    upstream[field] = value
    with pytest.raises(HTTPException) as caught:
        asyncio.run(open_stream(review_db))
    assert caught.value.status_code == 502
    assert not upstream['sync_threads']


@pytest.mark.parametrize('statement', [
    "UPDATE users SET is_active=0 WHERE id=1",
    "UPDATE users SET role='invalid' WHERE id=1",
    "UPDATE users SET group_id=2 WHERE id=1",
    "UPDATE printers SET camera_enabled=0 WHERE id=901",
    "UPDATE printers SET is_active=0 WHERE id=901",
    "UPDATE printers SET org_id=2,shared=0 WHERE id=901",
    "UPDATE printers SET camera_url=NULL,api_host=NULL WHERE id=901",
    "DELETE FROM printers WHERE id=901",
])
def test_authority_loss_stops_even_a_stalled_camera(upstream, review_db, statement):
    upstream['stall'] = True
    async def run():
        response = await open_stream(review_db)
        assert await anext(response.body_iterator) == FRAME
        review_db.execute(text(statement)); review_db.commit()
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(anext(response.body_iterator), 1)
    asyncio.run(run())


@pytest.mark.parametrize('carrier,loss', [
    ('api', 'revoked'), ('api', 'expired'), ('api', 'scope'),
    ('cookie', 'logout'), ('bearer', 'logout'),
    ('cookie', 'expired'), ('bearer', 'expired'),
    ('legacy', 'rotated'),
])
def test_credential_loss_stops_at_next_check(upstream, review_db, monkeypatch, carrier, loss):
    from core.auth import hash_password
    from core import dependencies
    import jwt

    upstream['stall'] = True
    if carrier == 'api':
        raw = 'odin_' + secrets.token_urlsafe(32)
        review_db.execute(text('INSERT INTO api_tokens(user_id,name,token_hash,token_prefix,scopes,expires_at) '
                               'VALUES(1,\'mjpeg-fixture\',:hash,:prefix,:scopes,:expires)'),
                          {'hash': hash_password(raw), 'prefix': raw[:10],
                           'scopes': json.dumps(['read:printers']),
                           'expires': (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()})
        review_db.commit()
        request = Request({'type': 'http', 'method': 'GET', 'path': '/api/cameras/901/mjpeg',
                           'headers': [(b'x-api-key', raw.encode())]})
    elif carrier == 'legacy':
        raw = secrets.token_urlsafe(32)
        monkeypatch.setenv('API_KEY', raw)
        request = Request({'type': 'http', 'method': 'GET', 'path': '/api/cameras/901/mjpeg',
                           'headers': [(b'x-api-key', raw.encode())]})
    else:
        request = jwt_request(review_db, cookie=carrier == 'cookie')
        # A valid secondary credential must not rescue a revoked/expired cookie.
        if carrier == 'cookie':
            request.scope['headers'].extend(jwt_request(review_db, user=3).scope['headers'])

    checks = []
    original = route._mjpeg_check
    def check(*args):
        checks.append(True)
        return original(*args)
    monkeypatch.setattr(route, '_mjpeg_check', check)

    class FutureDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(days=2)

    async def run():
        response = await open_stream(review_db, request=request)
        assert await anext(response.body_iterator) == FRAME
        assert len(checks) == 1
        if loss == 'revoked':
            review_db.execute(text("DELETE FROM api_tokens WHERE name='mjpeg-fixture'"))
        elif loss == 'scope':
            review_db.execute(text("UPDATE api_tokens SET scopes='[\"read:jobs\"]' WHERE name='mjpeg-fixture'"))
        elif loss == 'logout':
            token = request.cookies['session'] if carrier == 'cookie' else await dependencies.oauth2_scheme(request)
            payload = jwt.decode(token, options={'verify_signature': False})
            review_db.execute(text('INSERT INTO token_blacklist(jti,expires_at) VALUES(:jti,:expires)'),
                              {'jti': payload['jti'], 'expires': '2099-01-01'})
        elif loss == 'expired':
            # Advance only the resolver's clock; exercise real expiry validation.
            monkeypatch.setattr(dependencies if carrier == 'api' else jwt.api_jwt, 'datetime', FutureDatetime)
        else:
            monkeypatch.setenv('API_KEY', secrets.token_urlsafe(32))
        review_db.commit()
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(anext(response.body_iterator), 2)
        assert len(checks) == 2
        assert not relay.STREAMS
    asyncio.run(run())


def test_checks_run_in_threads_and_never_iterated_body_is_closed(upstream, review_db, monkeypatch):
    original = route._mjpeg_check
    threads = []
    def check(*args):
        threads.append(threading.get_ident())
        return original(*args)
    monkeypatch.setattr(route, '_mjpeg_check', check)
    async def run():
        response = await open_stream(review_db)
        assert len(relay.STREAMS) == 1
        del response
        gc.collect()
        for _ in range(20):
            if all(c.is_closed for c in upstream['clients']):
                break
            await asyncio.sleep(0.01)
    asyncio.run(run())
    assert threads and threading.get_ident() not in threads


def test_cancelled_stalled_read_closes_everything(upstream, review_db):
    upstream['stall'] = True
    async def run():
        response = await open_stream(review_db)
        await anext(response.body_iterator)
        waiting = asyncio.create_task(anext(response.body_iterator))
        await asyncio.sleep(0)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
    asyncio.run(run())


def test_authenticated_caps_and_operator_eviction(upstream, review_db, monkeypatch):
    upstream['stall'] = True
    async def run():
        streams = [await open_stream(review_db) for _ in range(3)]
        for stream in streams:
            await anext(stream.body_iterator)
        with pytest.raises(HTTPException) as cap:
            await open_stream(review_db)
        assert cap.value.status_code == 429
        monkeypatch.setattr(route, 'MJPEG_MAX_STREAMS_PER_PRINTER', 3)
        with pytest.raises(HTTPException) as full:
            await open_stream(review_db, 2)
        assert full.value.status_code == 429
        waiting = asyncio.create_task(anext(streams[0].body_iterator))
        owner = await open_stream(review_db, 3)
        await anext(owner.body_iterator)
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(waiting, 1)
        assert len(relay.STREAMS) == 3
        for stream in streams[1:] + [owner]:
            await stream.body_iterator.aclose()
    asyncio.run(run())


PROBE = r'''
from sqlalchemy.orm import Session
from modules.printers.models import Printer
from modules.printers import mjpeg_relay
import httpx
sql("INSERT INTO groups(id,name,is_org) VALUES(1,'own',1),(2,'other',1)")
uid = seed_user('camera-viewer','viewer',1)
seed_user('camera-outsider','viewer',2)
with Session(engine) as db:
    db.add(Printer(id=501,name='Fixture',api_type='moonraker',camera_enabled=True,is_active=True,org_id=1,shared=False,camera_url='rtsp://192.0.2.20/camera'))
    db.commit()
calls = []
def transport(req):
    calls.append(req.url.path)
    if req.url.path == '/api/streams':
        return httpx.Response(200,json={'printer_501_mjpeg':{}})
    assert req.url.params['src'] == 'printer_501_mjpeg'
    return httpx.Response(200,headers={'content-type':'multipart/x-mixed-replace; boundary=frame'},stream=httpx.ByteStream(b'--frame\r\nfixture'))
original = httpx.AsyncClient
mjpeg_relay.httpx.AsyncClient = lambda **kw: original(transport=httpx.MockTransport(transport), **kw)
viewer = login('camera-viewer')
bearer = viewer.headers.pop('Authorization')
outsider = login('camera-outsider')
for prefix in ['/api','/api/v1']:
    path = prefix+'/cameras/501/mjpeg'
    assert client().get(path).status_code == 401
    assert outsider.get(path).status_code == 404
    assert not calls
    # Session cookie alone, then Bearer alone, through real auth and routing.
    for c in [viewer, client()]:
        if c is not viewer:
            c.headers['Authorization'] = bearer
        response = c.get(path)
        assert response.status_code == 200, response.text
        assert response.headers['cache-control'] == 'no-store'
        assert response.content == b'--frame\r\nfixture'
    calls.clear()
# API scope fence uses real minted test tokens, never operator credentials.
for scope, expected in [('read:printers',200),('read:jobs',403),('read:cameras',401)]:
    raw = 'odin_' + secrets.token_urlsafe(32)
    sql('INSERT INTO api_tokens(user_id,name,token_hash,token_prefix,scopes) VALUES(:u,:n,:h,:p,:s)',
        u=uid,n=scope,h=hash_password(raw),p=raw[:10],s=json.dumps([scope]))
    r = client().get('/api/cameras/501/mjpeg',headers={'X-API-Key':raw})
    assert r.status_code == expected, (scope,r.status_code)
sql('UPDATE printers SET camera_enabled=0 WHERE id=501')
assert viewer.get('/api/cameras/501/mjpeg').status_code == 404
assert not mjpeg_relay.STREAMS
OUT['passed'] = True
'''


def test_cookie_bearer_scope_and_org_fences_through_real_app(tmp_path):
    result = run_probe('camera-mjpeg', PROBE, tmp_path)
    assert result['passed'] and result['_net_attempts'] == []


def test_overlay_and_authenticated_routes_share_user_budget(upstream, review_db):
    from modules.printers import camera_routes
    async def run():
        streams = [await open_stream(review_db) for _ in range(3)]
        for stream in streams:
            await anext(stream.body_iterator)
        try:
            with pytest.raises(HTTPException) as caught:
                camera_routes._overlay_claim({'user_id': 1, 'token_id': 999, 'privileged': False}, 901)
            assert caught.value.status_code == 429
        finally:
            for stream in streams:
                await stream.body_iterator.aclose()
    asyncio.run(run())
