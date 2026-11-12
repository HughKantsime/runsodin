"""One still frame for a camera-only token: no stream slot, the stream's authorization."""
import secrets
import httpx
import pytest
from sqlalchemy import text
from tests.test_contracts.test_cert_B_camera_fixes import overlay_client, reprint_client, review_db  # noqa: F401

JPEG = b"\xff\xd8\xff\xe0" + b"frame" * 20 + b"\xff\xd9"


@pytest.fixture
def frames(overlay_client, monkeypatch):
    from modules.printers import camera_routes
    client, tokens, _ = overlay_client
    calls, answer = [], {"status": 200, "body": JPEG}

    def go2rtc(request):
        calls.append(request.url.path + "?" + request.url.query.decode())
        if request.url.path == "/api/frame.jpeg":
            return httpx.Response(answer["status"], content=answer["body"], headers={"content-type": "image/jpeg"})
        return httpx.Response(200, headers={"content-type": "multipart/x-mixed-replace; boundary=frame"}, content=b"")
    from httpx._client import AsyncClient as real   # the fixture above already replaced httpx.AsyncClient
    monkeypatch.setattr(camera_routes.httpx, "AsyncClient",
                        lambda *a, **k: real(*a, **{**k, "transport": httpx.MockTransport(go2rtc)}))
    camera_routes._snapshot_frames.clear(); camera_routes._snapshot_locks.clear()
    synced = []
    monkeypatch.setattr(camera_routes, "sync_go2rtc_config", lambda db: synced.append(1))
    return client, tokens, calls, answer, synced


def test_snapshot_returns_one_jpeg_and_opens_no_stream(frames):
    from modules.printers import camera_routes
    client, tokens, calls, _, synced = frames
    response = client.get("/overlay/9/snapshot", params={"token": tokens["camera"]})
    assert response.status_code == 200 and response.content == JPEG
    assert response.headers["content-type"] == "image/jpeg" and response.headers["cache-control"] == "no-store"
    assert calls == ["/api/frame.jpeg?src=printer_9_mjpeg"]
    assert camera_routes.overlay_stream_count() == 0 and synced == []


def test_many_snapshots_on_one_token_all_succeed_and_share_one_upstream_read(frames, monkeypatch):
    from modules.printers import camera_routes
    client, tokens, calls, _, _ = frames
    monkeypatch.setattr(camera_routes, "OVERLAY_MAX_STREAMS_PER_TOKEN", 1)   # stream caps do not apply
    monkeypatch.setattr(camera_routes, "OVERLAY_SNAPSHOT_SHARE_SECONDS", 60)
    for _ in range(8):
        assert client.get("/overlay/9/snapshot", params={"token": tokens["camera"]}).status_code == 200
    assert len(calls) == 1
    monkeypatch.setattr(camera_routes, "OVERLAY_SNAPSHOT_SHARE_SECONDS", 0)
    assert client.get("/overlay/9/snapshot", params={"token": tokens["camera"]}).status_code == 200
    assert len(calls) == 2


def test_snapshot_refuses_exactly_what_the_stream_refuses(frames, review_db):
    client, tokens, calls, _, _ = frames
    get = lambda printer, **params: client.get(f"/overlay/{printer}/snapshot", params=params).status_code
    assert get(9) == 401
    assert get(9, token="odin_" + secrets.token_urlsafe(32)) == 401
    assert get(9, token=tokens["jobs"]) == 403 and get(9, token=tokens["printers"]) == 403 and get(9, token=tokens["read"]) == 403
    assert get(11, token=tokens["camera"]) == 404          # another organization's printer
    review_db.execute(text("UPDATE printers SET camera_enabled=0 WHERE id=9")); review_db.commit()
    assert get(9, token=tokens["camera"]) == 404
    assert calls == []                                      # nothing refused ever reached the relay


def test_a_cached_frame_is_never_released_to_a_revoked_token(frames, review_db, monkeypatch):
    from modules.printers import camera_routes
    client, tokens, calls, _, _ = frames
    monkeypatch.setattr(camera_routes, "OVERLAY_SNAPSHOT_SHARE_SECONDS", 60)
    assert client.get("/overlay/9/snapshot", params={"token": tokens["camera"]}).status_code == 200
    review_db.execute(text("DELETE FROM api_tokens WHERE name='camera'")); review_db.commit()
    assert client.get("/overlay/9/snapshot", params={"token": tokens["camera"]}).status_code == 401
    assert client.get("/overlay/9/snapshot", params={"token": tokens["foreign"]}).status_code == 404
    assert len(calls) == 1


def test_missing_stream_syncs_the_relay_once_then_reports_unavailable(frames):
    client, tokens, calls, answer, synced = frames
    answer["status"] = 404
    assert client.get("/overlay/9/snapshot", params={"token": tokens["camera"]}).status_code == 502
    assert synced == [1] and len(calls) == 2
    answer.update(status=200, body=b"<html>not a picture</html>")
    assert client.get("/overlay/9/snapshot", params={"token": tokens["camera"]}).status_code == 502


def test_snapshot_view_is_audited(frames, review_db):
    client, tokens, _, _, _ = frames
    assert client.get("/overlay/9/snapshot", params={"token": tokens["camera"]}).status_code == 200
    review_db.commit()
    rows = review_db.execute(text("SELECT COUNT(*) FROM audit_logs WHERE action='camera.viewed' AND entity_id=9")).scalar()
    assert rows >= 1
