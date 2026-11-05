"""Media HTTP gates and real response bytes with owned upstreams/files."""
import json

import httpx
import pytest
from sqlalchemy import event, text
from sqlalchemy.orm import Session

from tests.test_contracts.test_audit_completeness import client, review_db, rows, refuse_audit
from tests.test_contracts.test_camera_scope_behavior import (
    managed_camera_client, camera_client, token_client, prepared_source, encoder, frame_writer, owned_runtime,
)


@pytest.fixture
def camera_http(client, review_db, monkeypatch):
    from modules.printers import camera_routes, routes_camera_snapshot
    client.app.include_router(routes_camera_snapshot.router)
    review_db.execute(text('UPDATE printers SET camera_enabled=1,is_active=1 WHERE id=9'))
    review_db.commit()
    monkeypatch.setattr(camera_routes, 'get_camera_url', lambda p: 'rtsp://synthetic.invalid/camera')
    real = httpx.AsyncClient
    state = {'status': 200}
    def upstream(request):
        if request.url.path == '/api/streams':
            return httpx.Response(200, json={'printer_9': {}})
        if request.url.path == '/api/webrtc':
            return httpx.Response(state['status'], content=b'owned-sdp')
        return httpx.Response(state['status'], content=b'\xff\xd8\xffowned-jpeg')
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: real(transport=httpx.MockTransport(upstream), **kw))
    return client, state


@pytest.mark.parametrize('transport', ['snapshot', 'webrtc'])
def test_camera_bytes_audited_and_fail_closed(camera_http, review_db, transport):
    client, state = camera_http
    call = client.get if transport == 'snapshot' else client.post
    url = f'/cameras/9/{transport}'
    assert call(url).status_code == 200
    assert call(url).status_code == 200
    row = rows(review_db, 'camera.viewed')
    assert len(row) == 1 and json.loads(row[0].details) == {'transport': transport}
    assert tuple(row[0][3:]) == (3, 'admin', 'bearer_jwt', '198.51.100.7')
    review_db.execute(text('DELETE FROM audit_logs')); review_db.commit()
    listener = refuse_audit(review_db, 'camera.viewed')
    try:
        response = call(url)
    finally:
        event.remove(review_db.get_bind(), 'before_cursor_execute', listener)
    assert response.status_code == 503 and b'owned-' not in response.content
    assert rows(review_db, 'camera.viewed') == []


@pytest.mark.parametrize('mode', ['anonymous', 'disabled', 'foreign', 'upstream'])
def test_refused_camera_reads_write_nothing(camera_http, review_db, mode):
    client, state = camera_http
    if mode == 'anonymous': client.headers.pop('Authorization')
    if mode == 'disabled':
        review_db.execute(text('UPDATE printers SET camera_enabled=0 WHERE id=9')); review_db.commit()
    if mode == 'foreign':
        from core.auth import create_access_token
        client.headers['Authorization'] = 'Bearer ' + create_access_token({'sub': 'foreign', 'role': 'admin'})
    if mode == 'upstream': state['status'] = 502
    for method, suffix in ((client.get, 'snapshot'), (client.post, 'webrtc')):
        assert method(f'/cameras/9/{suffix}').status_code >= 400
    assert not rows(review_db, 'camera.viewed')


def test_timelapse_range_head_and_both_downloads(managed_camera_client):
    client, issue, _, tid, _, payload, engine = managed_camera_client
    headers = issue(['read:printers'])
    url = f'/timelapses/{tid}'
    assert client.get(url + '/stream').status_code == 401
    assert client.get(url + '/stream', headers={**headers, 'Range': 'bytes=999999999-'}).status_code == 416
    with Session(engine) as db:
        assert not rows(db, 'timelapse.viewed')
    assert client.head(url + '/stream', headers=headers).status_code == 200
    response = client.get(url + '/stream', headers={**headers, 'Range': 'bytes=0-3'})
    assert response.status_code == 206 and response.content == payload[:4]
    assert client.get(url + '/stream', headers={**headers, 'Range': 'bytes=4-7'}).status_code == 206
    for suffix in ('video', 'download'):
        response = client.get(url + '/' + suffix, headers=headers)
        assert response.status_code == 200 and response.content == payload
        assert response.headers['content-disposition'].startswith('attachment')
    with Session(engine) as db:
        assert len(rows(db, 'timelapse.viewed')) == 1
        downloads = rows(db, 'timelapse.downloaded')
        assert len(downloads) == 2 and all(row.actor_auth_kind == 'user_api_token' for row in downloads)
        listener = refuse_audit(db)
    try:
        assert client.get(url + '/video', headers=headers).status_code == 503
        with engine.begin() as conn:
            conn.execute(text("UPDATE audit_logs SET timestamp='2000-01-01'"))
        assert client.get(url + '/stream', headers=headers).status_code == 503
    finally:
        event.remove(engine, 'before_cursor_execute', listener)
