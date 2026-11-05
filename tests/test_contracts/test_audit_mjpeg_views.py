"""Live MJPEG admission, renewal, erasure attribution, and audit failure cleanup."""
import asyncio
import json

import pytest
from fastapi import HTTPException
from sqlalchemy import event, text

from tests.test_contracts.test_camera_mjpeg import upstream, open_stream, review_db, FRAME
from tests.test_contracts.test_cert_B_camera_fixes import overlay_client, reprint_client
from tests.test_contracts.test_audit_completeness import rows, refuse_audit


def test_mjpeg_admission_failure_releases_all_resources(upstream, review_db):
    listener = refuse_audit(review_db)
    try:
        with pytest.raises(HTTPException) as error:
            asyncio.run(open_stream(review_db))
        assert error.value.status_code == 503
    finally:
        event.remove(review_db.get_bind(), 'before_cursor_execute', listener)
    assert not rows(review_db, 'camera.viewed')


@pytest.mark.parametrize('fail_renewal', [False, True])
def test_long_mjpeg_stream_renews_or_stops(upstream, review_db, fail_renewal):
    upstream['stall'] = True
    async def run():
        response = await open_stream(review_db)
        body = response.body_iterator
        assert await anext(body) == FRAME
        initial = rows(review_db, 'camera.viewed')
        assert len(initial) == 1
        assert tuple(initial[0][3:]) == (1, 'student', 'bearer_jwt', '198.51.100.7')
        await asyncio.sleep(0.06)
        assert len(rows(review_db, 'camera.viewed')) == 1
        review_db.execute(text("UPDATE audit_logs SET timestamp='2000-01-01'")); review_db.commit()
        listener = refuse_audit(review_db) if fail_renewal else None
        try:
            pending = asyncio.create_task(anext(body))
            if fail_renewal:
                with pytest.raises(StopAsyncIteration):
                    await asyncio.wait_for(pending, timeout=3)
            else:
                for _ in range(100):
                    await asyncio.sleep(0.02)
                    if len(rows(review_db, 'camera.viewed')) == 2:
                        break
                assert len(rows(review_db, 'camera.viewed')) == 2
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
        finally:
            if listener:
                event.remove(review_db.get_bind(), 'before_cursor_execute', listener)
            await body.aclose()
    asyncio.run(run())


def test_obs_token_view_is_attributed_and_rate_limited(overlay_client, review_db):
    client, tokens, _ = overlay_client
    for _ in range(2):
        assert client.get('/overlay/9/camera', params={'token': tokens['camera']}).status_code == 200
    found = rows(review_db, 'camera.viewed')
    assert len(found) == 1
    assert found[0].actor_user_id == 1 and found[0].actor_username == 'student'
    assert found[0].actor_auth_kind == 'user_api_token'
    assert json.loads(found[0].details) == {'transport': 'mjpeg'}
    assert client.get('/overlay/9/camera', params={'token': tokens['jobs']}).status_code == 403
    assert client.get('/overlay/9/camera').status_code == 401
    assert len(rows(review_db, 'camera.viewed')) == 1
    review_db.execute(text("UPDATE audit_logs SET timestamp='2000-01-01'")); review_db.commit()
    listener = refuse_audit(review_db)
    try:
        response = client.get('/overlay/9/camera', params={'token': tokens['camera']})
    finally:
        event.remove(review_db.get_bind(), 'before_cursor_execute', listener)
    assert response.status_code == 503
