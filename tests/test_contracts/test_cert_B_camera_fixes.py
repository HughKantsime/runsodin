"""Cert B camera fixes, in process (real routers, bootstrapped SQLite schema).

* /printers reports camera_enabled and the effective camera (the generated RTSPS URL of
  Bambu X1C/X1E/H2D, masked); filament slots report remaining grams (F02-2.1-03, F02-2.6-04, F02-2.5-03)
* the OBS overlay camera is an MJPEG relay authorized by a scoped read:cameras API token
  carried in the copied overlay URL (F02-2.8-02). Live evidence: test_cert_B_live_stack.py.
"""
import secrets

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from tests.test_contracts.cert_B_support import check_wire
from tests.test_contracts.test_archive_reprint_behavior import reprint_client  # noqa: F401
from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401


# --------------------------------------------------------------------------- printers

@pytest.fixture
def printers_client(reprint_client, review_db):
    client, identity, _ = reprint_client
    from core import crypto
    from modules.printers import routes_crud
    review_db.execute(text("INSERT INTO printers(id,name,model,api_type,api_host,api_key,is_active,camera_enabled,org_id,slot_count,shared,timelapse_enabled,tags,created_at,updated_at) "
                           "VALUES(21,'X1C auto','X1C','bambu','192.0.2.21',:key,1,1,1,4,0,0,'[]',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP),"
                           "(22,'P1S auto','P1S','bambu','192.0.2.22',:key,1,1,1,4,0,0,'[]',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP),"
                           "(23,'RTSP off','Voron','moonraker','192.0.2.23',NULL,1,NULL,1,1,0,0,'[]',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP)"),
                      {"key": crypto.encrypt("01P00SYNTH0001|" + secrets.token_hex(4))})
    review_db.execute(text("UPDATE printers SET camera_url='rtsp://192.0.2.23:8554/cam' WHERE id=23"))
    review_db.commit()
    client.app.include_router(routes_crud.router)
    identity.update(role="operator", group_id=1)
    return client


def test_printers_report_camera_enabled_and_effective_camera(printers_client):
    rows = {pid: printers_client.get(f"/printers/{pid}").json() for pid in (21, 22, 23)}
    auto = rows[21]
    assert auto["camera_source"] == "auto" and auto["camera_enabled"] is True
    assert auto["camera_url"] == "rtsps://***@192.0.2.21:322/streaming/live/1"  # access code masked
    assert rows[22]["camera_url"] == "bambu-chamber://auto" and rows[22]["camera_source"] == "auto"
    # NULL camera_enabled streams nowhere (go2rtc selects TRUE), so it reads as disabled.
    assert rows[23]["camera_enabled"] is False and rows[23]["camera_source"] == "configured"


def test_resaving_masked_auto_camera_keeps_it_generated(printers_client, review_db):
    masked = printers_client.get("/printers/21").json()["camera_url"]
    response = printers_client.patch("/printers/21", json={"camera_url": masked, "nickname": "Bay 1"})
    assert response.status_code == 200, response.text
    assert review_db.execute(text("SELECT camera_url FROM printers WHERE id=21")).scalar_one() is None
    # The generated URL is derived from the access code at read time; a masked edit
    # would freeze the code into camera_url, so it is refused (enter a full URL to override).
    for edited in ("rtsps://***@192.0.2.99:322/streaming/live/1", "rtsps://***@192.0.2.21:322/streaming/live/2"):
        response = printers_client.patch("/printers/21", json={"camera_url": edited})
        assert response.status_code == 400, response.text
        assert review_db.execute(text("SELECT camera_url FROM printers WHERE id=21")).scalar_one() is None
    # An explicit full URL still overrides the generated camera.
    response = printers_client.patch("/printers/21", json={"camera_url": "rtsps://bblp:other@192.0.2.21:322/streaming/live/2"})
    assert response.status_code == 200 and response.json()["camera_source"] == "configured"


def test_query_string_camera_credentials_are_encrypted_masked_and_carried(printers_client, review_db):
    from core import crypto
    url = "rtsp://192.0.2.23:554/stream?user=admin&password=Hunter2x&channel=1"
    response = printers_client.patch("/printers/23", json={"camera_url": url})
    assert response.status_code == 200, response.text
    stored = review_db.execute(text("SELECT camera_url FROM printers WHERE id=23")).scalar_one()
    assert crypto.is_encrypted(stored) and crypto.decrypt(stored) == url  # never plaintext at rest
    masked = printers_client.get("/printers/23").json()["camera_url"]
    assert "Hunter2x" not in masked and "admin" not in masked and masked == "rtsp://192.0.2.23:554/stream?user=***&password=***&channel=1"
    # Resaving the masked URL changes nothing; a path/param change keeps the credentials.
    assert printers_client.patch("/printers/23", json={"camera_url": masked}).status_code == 200
    assert crypto.decrypt(review_db.execute(text("SELECT camera_url FROM printers WHERE id=23")).scalar_one()) == url
    edited = "rtsp://192.0.2.23/stream?user=***&password=***&channel=2"  # default port 554 is the same endpoint
    assert printers_client.patch("/printers/23", json={"camera_url": edited}).status_code == 200
    assert crypto.decrypt(review_db.execute(text("SELECT camera_url FROM printers WHERE id=23")).scalar_one()) == \
        "rtsp://192.0.2.23/stream?user=admin&password=Hunter2x&channel=2"
    # Another host with masked values is refused.
    assert printers_client.patch("/printers/23", json={"camera_url": "rtsp://192.0.2.99/stream?user=***&password=***"}).status_code == 400


@pytest.mark.parametrize("a,b,same", [("rtsp://h/x", "rtsp://h:554/y", True), ("rtsps://h/x", "rtsps://h:322/y", True),
                                      ("rtsps://h:322/x", "rtsps://h:8322/x", False), ("rtsp://h/x", "rtsps://h/x", False)])
def test_default_ports_are_equivalent_endpoints(a, b, same):
    from modules.printers.route_utils import _url_endpoint
    assert (_url_endpoint(a) == _url_endpoint(b)) is same


def test_filament_slot_reports_remaining_grams(printers_client, review_db):
    review_db.execute(text("INSERT INTO filament_library(id,brand,name,material) VALUES(501,'Synthetic','Black','PLA')"))
    review_db.execute(text("INSERT INTO spools(id,filament_id,remaining_weight_g,initial_weight_g) VALUES(601,501,640,1000)"))
    review_db.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type,color,assigned_spool_id) VALUES(21,1,'PLA','Black',601)"))
    review_db.commit()
    slot = next(s for s in printers_client.get("/printers/21").json()["filament_slots"] if s["slot_number"] == 1)
    assert slot["remaining_weight"] == 640 and slot["remaining"] == 64.0


# --------------------------------------------------------------------------- overlay camera

MJPEG = b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: 4\r\n\r\n\xff\xd8\xff\xd9\r\n"


@pytest.fixture
def overlay_client(reprint_client, review_db, monkeypatch):
    client, identity, _ = reprint_client
    from core.auth import hash_password
    from modules.printers import camera_routes, routes_crud
    review_db.execute(text("UPDATE printers SET camera_url='rtsp://192.0.2.9:8554/cam',camera_enabled=1 WHERE id=9"))
    review_db.execute(text("UPDATE printers SET camera_url='rtsp://192.0.2.11:8554/cam',camera_enabled=1 WHERE id=11"))
    tokens = {}
    for user_id, scopes, name in ((1, '["read:cameras"]', "camera"), (1, '["read:jobs"]', "jobs"), (5, '["read:cameras"]', "foreign"),
                                 (1, '["read:printers"]', "printers"), (1, '["read"]', "read"), (1, '["admin"]', "admin"),
                                 (1, '["read:cameras","read"]', "legacy_combo")):
        raw = "odin_" + secrets.token_urlsafe(32)
        review_db.execute(text("INSERT INTO api_tokens(user_id,name,token_hash,token_prefix,scopes) VALUES(:u,:n,:h,:p,:s)"),
                          {"u": user_id, "n": name, "h": hash_password(raw), "p": raw[:10], "s": scopes})
        tokens[name] = raw
    review_db.commit()
    upstream = []

    def go2rtc(request):
        upstream.append(str(request.url))
        if request.url.path == "/api/streams":
            return httpx.Response(200, json={"printer_9": {}, "printer_9_mjpeg": {}, "printer_11": {}, "printer_11_mjpeg": {}})
        assert request.url.path == "/api/stream.mjpeg"
        return httpx.Response(200, headers={"content-type": "multipart/x-mixed-replace; boundary=frame"}, stream=httpx.ByteStream(MJPEG))
    real_client = httpx.AsyncClient
    monkeypatch.setattr(camera_routes.httpx, "AsyncClient", lambda *a, **k: real_client(*a, transport=httpx.MockTransport(go2rtc), **k))
    monkeypatch.setattr(camera_routes, "sync_go2rtc_config", lambda db: None)
    app = FastAPI()
    app.include_router(camera_routes.router)
    app.include_router(routes_crud.router)
    app.dependency_overrides.update(client.app.dependency_overrides)
    return TestClient(app, raise_server_exceptions=False), tokens, upstream


def test_overlay_points_at_token_guarded_mjpeg_relay(overlay_client):
    client, tokens, upstream = overlay_client
    data = client.get("/overlay/9").json()
    assert data["camera_url"] == "/api/overlay/9/camera"
    check_wire("overlay", data)
    response = client.get("/overlay/9/camera", params={"token": tokens["camera"]})
    assert response.status_code == 200 and response.headers["content-type"].startswith("multipart/x-mixed-replace")
    assert response.headers["cache-control"] == "no-store" and response.content == MJPEG
    assert any(u.endswith("/api/stream.mjpeg?src=printer_9_mjpeg") for u in upstream)


def test_overlay_camera_refuses_missing_wrong_scope_and_foreign_tokens(overlay_client, review_db):
    client, tokens, upstream = overlay_client
    assert client.get("/overlay/9/camera").status_code == 401
    assert client.get("/overlay/9/camera", params={"token": "odin_" + secrets.token_urlsafe(32)}).status_code == 401
    assert client.get("/overlay/9/camera", params={"token": tokens["jobs"]}).status_code == 403
    # Only camera-only tokens open the relay; broader ones (and combinations minted
    # before the camera-only rule) are refused.
    expected = {"printers": 403,      # a valid token, but not camera-only
                "read": 403,          # a valid token, but not camera-only
                "admin": 401,         # admin scope of a non-admin user: refused at resolution
                "legacy_combo": 401}  # combined read:cameras tokens are revoked
    for name, status in expected.items():
        assert client.get("/overlay/9/camera", params={"token": tokens[name]}).status_code == status, name
    assert client.get("/overlay/11/camera", params={"token": tokens["camera"]}).status_code == 404  # org 2 printer, org 1 token
    review_db.execute(text("UPDATE printers SET camera_enabled=0 WHERE id=9")); review_db.commit()
    assert client.get("/overlay/9/camera", params={"token": tokens["camera"]}).status_code == 404
    assert client.get("/overlay/9").json()["camera_url"] is None
    review_db.execute(text("UPDATE printers SET camera_enabled=1 WHERE id=9"))
    review_db.execute(text("DELETE FROM api_tokens WHERE name='camera'")); review_db.commit()  # revoked
    assert client.get("/overlay/9/camera", params={"token": tokens["camera"]}).status_code == 401
    assert not any("stream.mjpeg" in u for u in upstream)


def test_read_cameras_token_grants_no_other_read():
    from core.api_token_policy import token_scope_granted
    assert not token_scope_granted(["read:cameras"], "read:printers")
    assert not token_scope_granted(["read:cameras"], "read:jobs")
    assert token_scope_granted(["read"], "read:cameras")


def test_go2rtc_config_has_on_demand_mjpeg_variant_per_camera(printers_client, review_db):
    """go2rtc 1.9 cannot serve H.264 as stream.mjpeg; each camera gets an ffmpeg MJPEG variant."""
    review_db.execute(text("UPDATE printers SET camera_url='http://192.0.2.24:8080/webcam/?action=stream',camera_enabled=1 WHERE id=23"))
    review_db.commit()
    from modules.printers.camera_relay import relay_url
    from modules.printers.route_utils import _build_go2rtc_config
    streams = _build_go2rtc_config(review_db)["streams"]
    from modules.printers.route_utils import go2rtc_rtsp_url
    # RTSPS X1C: transcode on demand from go2rtc's authenticated loopback RTSP server.
    assert streams["printer_21_mjpeg"] == f"ffmpeg:{go2rtc_rtsp_url('printer_21')}#video=mjpeg"
    # HTTP cameras reach go2rtc only through cert-fix-A's pinned loopback relay, never directly.
    assert streams["printer_23"] == f"ffmpeg:{relay_url(23)}#video=h264"
    assert streams["printer_23_mjpeg"] == relay_url(23)  # MJPEG webcam passed through the relay as is
    assert "192.0.2.24" not in str(streams)
    assert streams["printer_22"] == f"ffmpeg:{relay_url(22)}#video=h264"
    assert streams["printer_22_mjpeg"] == relay_url(22)


def test_raw_and_orm_go2rtc_config_builders_are_identical(printers_client, review_db, monkeypatch):
    """Monitors sync go2rtc through the raw-SQL builder; it must emit the same streams
    (including printer_<id>_mjpeg variants) as the ORM builder the API uses."""
    from contextlib import contextmanager
    from core import db_utils
    from modules.printers import route_utils
    review_db.execute(text("UPDATE printers SET camera_url='http://192.0.2.24:8080/webcam/?action=stream',camera_enabled=1 WHERE id=23"))
    review_db.execute(text("UPDATE printers SET camera_url='rtsps://192.0.2.9:7441/abc?enableSrtp',camera_enabled=1 WHERE id=9"))
    review_db.commit()
    monkeypatch.setenv("ODIN_HOST_IP", "192.0.2.200")

    @contextmanager
    def raw_db():
        conn = review_db.get_bind().raw_connection()
        try:
            yield conn
        finally:
            conn.close()
    monkeypatch.setattr(db_utils, "get_db", raw_db)
    written = []
    monkeypatch.setattr(route_utils, "_go2rtc_restart_with_lock", written.append)
    route_utils.sync_go2rtc_config_raw()
    orm = route_utils._build_go2rtc_config(review_db)
    assert written == [orm]
    assert {"printer_21_mjpeg", "printer_23_mjpeg", "printer_9_mjpeg"} <= set(orm["streams"])


class _Endless(httpx.AsyncByteStream):
    """An MJPEG camera that never stops."""
    def __init__(self):
        self.sent = 0

    async def __aiter__(self):
        import asyncio
        while True:
            self.sent += 1
            yield MJPEG
            await asyncio.sleep(0.02)


_REAL_ASYNC_CLIENT = httpx.AsyncClient  # before any fixture patches the module attribute


def _go2rtc(monkeypatch, handler):
    from modules.printers import camera_routes
    monkeypatch.setattr(camera_routes.httpx, "AsyncClient",
                        lambda *a, **k: _REAL_ASYNC_CLIENT(*a, **{**k, "transport": httpx.MockTransport(handler)}))


def _endless_go2rtc(monkeypatch):
    camera = _Endless()

    def handler(request):
        if request.url.path == "/api/streams":
            return httpx.Response(200, json={"printer_9_mjpeg": {}})
        return httpx.Response(200, headers={"content-type": "multipart/x-mixed-replace; boundary=frame"}, stream=camera)
    _go2rtc(monkeypatch, handler)
    return camera


def test_unreachable_go2rtc_is_reported_never_synced_or_restarted(overlay_client, monkeypatch):
    from modules.printers import camera_routes
    client, tokens, _ = overlay_client
    synced = []
    monkeypatch.setattr(camera_routes, "sync_go2rtc_config", lambda db: synced.append(1))

    def down(request):
        raise httpx.ConnectError("connection refused", request=request)
    _go2rtc(monkeypatch, down)
    assert client.get("/overlay/9/camera", params={"token": tokens["camera"]}).status_code == 502
    assert synced == []


def _open_stream(review_db, token):
    """Call the real endpoint in an event loop (TestClient buffers whole responses,
    so an endless MJPEG stream must be consumed directly)."""
    from sqlalchemy.orm import Session
    from modules.printers import camera_routes
    return camera_routes.overlay_camera_stream(printer_id=9, token=token, db=Session(review_db.get_bind()))


def _consume(review_db, token, after_frames, mutate, seconds=8):
    """Read the stream; run `mutate` after some frames; True if the server ended it."""
    import asyncio

    async def run():
        response = await _open_stream(review_db, token)
        frames = 0
        loop = asyncio.get_running_loop()
        deadline = loop.time() + seconds
        iterator = response.body_iterator.__aiter__()
        try:
            while loop.time() < deadline:
                try:
                    await asyncio.wait_for(iterator.__anext__(), timeout=max(0.01, deadline - loop.time()))
                except StopAsyncIteration:
                    return True
                except asyncio.TimeoutError:
                    return False
                frames += 1
                if frames == after_frames:
                    await loop.run_in_executor(None, mutate)
            return False
        finally:
            await iterator.aclose()
    return asyncio.run(run())


def _execute(review_db, statement):
    def run():
        review_db.execute(text(statement))
        review_db.commit()
    return run


@pytest.mark.parametrize("statement", ["DELETE FROM api_tokens WHERE name='camera'",
                                       "UPDATE api_tokens SET expires_at='2000-01-01T00:00:00+00:00' WHERE name='camera'",
                                       "UPDATE printers SET camera_enabled=0 WHERE id=9",
                                       "UPDATE users SET is_active=0 WHERE id=1",
                                       "UPDATE api_tokens SET user_id=5 WHERE name='camera'"])  # owner now in another org
def test_revocation_or_lost_access_ends_a_running_stream(overlay_client, review_db, monkeypatch, statement):
    from modules.printers import camera_routes
    _, tokens, _ = overlay_client
    monkeypatch.setattr(camera_routes, "OVERLAY_REVALIDATE_SECONDS", 0.2)
    camera = _endless_go2rtc(monkeypatch)
    assert _consume(review_db, tokens["camera"], 3, _execute(review_db, statement)), f"stream kept running ({camera.sent} frames)"
    assert camera_routes.overlay_stream_count() == 0


def test_unchanged_authorization_keeps_streaming(overlay_client, review_db, monkeypatch):
    from modules.printers import camera_routes
    _, tokens, _ = overlay_client
    monkeypatch.setattr(camera_routes, "OVERLAY_REVALIDATE_SECONDS", 0.2)
    _endless_go2rtc(monkeypatch)
    assert _consume(review_db, tokens["camera"], 3, lambda: None, seconds=1.5) is False  # still live after revalidations


def _mint(review_db, user_id, name):
    from core.auth import hash_password
    raw = "odin_" + secrets.token_urlsafe(32)
    review_db.execute(text("INSERT INTO api_tokens(user_id,name,token_hash,token_prefix,scopes) VALUES(:u,:n,:h,:p,'[\"read:cameras\"]')"),
                      {"u": user_id, "n": name, "h": hash_password(raw), "p": raw[:10]})
    review_db.commit()
    return raw


def _closed(review_db, upstreams):
    return all(u.closed for u in upstreams)


def test_viewer_caps_per_token_and_per_user(overlay_client, review_db, monkeypatch):
    import asyncio
    from fastapi import HTTPException
    from modules.printers import camera_routes
    _, tokens, _ = overlay_client
    _endless_go2rtc(monkeypatch)
    second_token = _mint(review_db, 1, "camera-2")
    monkeypatch.setattr(camera_routes, "OVERLAY_MAX_STREAMS_PER_TOKEN", 1)
    monkeypatch.setattr(camera_routes, "OVERLAY_MAX_STREAMS_PER_USER_PRINTER", 2)

    async def run():
        first = await _open_stream(review_db, tokens["camera"])
        await first.body_iterator.__anext__()
        with pytest.raises(HTTPException) as refused:
            await _open_stream(review_db, tokens["camera"])  # same token
        assert refused.value.status_code == 429
        other = await _open_stream(review_db, second_token)  # same user, another token: 2nd slot
        await other.body_iterator.__anext__()
        third = _mint(review_db, 1, "camera-3")
        with pytest.raises(HTTPException) as refused:
            await _open_stream(review_db, third)  # the user's per-printer share is used up
        assert refused.value.status_code == 429
        await first.body_iterator.aclose()
        await other.body_iterator.aclose()
        assert camera_routes.overlay_stream_count() == 0
    asyncio.run(run())


def test_one_user_cannot_lock_out_the_printer_owner(overlay_client, review_db, monkeypatch):
    import asyncio
    from fastapi import HTTPException
    from modules.printers import camera_routes
    _, tokens, _ = overlay_client
    _endless_go2rtc(monkeypatch)
    monkeypatch.setattr(camera_routes, "OVERLAY_MAX_STREAMS_PER_PRINTER", 2)
    monkeypatch.setattr(camera_routes, "OVERLAY_MAX_STREAMS_PER_USER_PRINTER", 2)
    viewer_two = _mint(review_db, 1, "camera-2")
    owner = _mint(review_db, 3, "owner-camera")  # user 3: admin of org 1 (mutate rights on printer 9)

    async def run():
        oldest = await _open_stream(review_db, tokens["camera"])
        await oldest.body_iterator.__anext__()
        newer = await _open_stream(review_db, viewer_two)
        await newer.body_iterator.__anext__()
        stranger = _mint(review_db, 4, "outsider")  # another viewer of org 1: ceiling reached
        with pytest.raises(HTTPException) as refused:
            await _open_stream(review_db, stranger)
        assert refused.value.status_code == 429
        admitted = await _open_stream(review_db, owner)  # the owner always gets a slot...
        await admitted.body_iterator.__anext__()
        # ...by evicting the oldest viewer, whose stream ends.
        ended = False
        try:
            for _ in range(200):
                await asyncio.wait_for(oldest.body_iterator.__anext__(), 1)
        except StopAsyncIteration:
            ended = True
        assert ended
        await asyncio.wait_for(newer.body_iterator.__anext__(), 1)  # the newer viewer keeps watching
        for response in (newer, admitted):
            await response.body_iterator.aclose()
        assert camera_routes.overlay_stream_count() == 0
    asyncio.run(run())


def test_never_iterated_body_closes_upstream_and_frees_its_slot(overlay_client, review_db, monkeypatch):
    import asyncio, gc
    from modules.printers import camera_routes
    _, tokens, _ = overlay_client
    opened = []
    real = _REAL_ASYNC_CLIENT

    class Tracking(real):
        def __init__(self, *a, **k):
            super().__init__(*a, **{**k, "transport": httpx.MockTransport(lambda r: httpx.Response(200, json={"printer_9_mjpeg": {}})
                if r.url.path == "/api/streams" else httpx.Response(200, headers={"content-type": "multipart/x-mixed-replace; boundary=frame"}, stream=_Endless()))})
            opened.append(self)
    monkeypatch.setattr(camera_routes.httpx, "AsyncClient", Tracking)

    async def run():
        response = await _open_stream(review_db, tokens["camera"])
        assert camera_routes.overlay_stream_count() == 1
        del response
        gc.collect()
        for _ in range(20):
            await asyncio.sleep(0.05)
            if all(c.is_closed for c in opened):
                break
    asyncio.run(run())
    assert opened and all(c.is_closed for c in opened)
    assert camera_routes.overlay_stream_count() == 0


def test_token_check_runs_off_the_event_loop(overlay_client, monkeypatch):
    import asyncio
    from core import dependencies
    client, tokens, _ = overlay_client
    real = dependencies.resolve_user_api_token
    calls = []

    def checked(raw, db):
        try:
            asyncio.get_running_loop()
            calls.append("on-loop")
        except RuntimeError:
            calls.append("worker")
        return real(raw, db)
    monkeypatch.setattr(dependencies, "resolve_user_api_token", checked)
    assert client.get("/overlay/9/camera", params={"token": tokens["camera"]}).status_code == 200
    assert calls and set(calls) == {"worker"}


@pytest.mark.parametrize("param", ["api_key", "apikey", "usr", "access_code", "sig", "token"])
def test_more_query_credential_parameters_are_protected(param):
    from modules.printers.route_utils import camera_url_has_credentials, sanitize_camera_url
    url = f"rtsp://192.0.2.5/live?{param}=S3cretValue9&res=hd"
    assert camera_url_has_credentials(url)
    assert sanitize_camera_url(url) == f"rtsp://192.0.2.5/live?{param}=***&res=hd"


def test_unifi_protect_path_token_is_encrypted_masked_and_restored(printers_client, review_db):
    from core import crypto
    from modules.printers.route_utils import camera_url_has_credentials, sanitize_camera_url
    token = "aB3dE5fG7hJ9kL1m"
    for url in (f"rtsp://192.0.2.23:7447/{token}", f"rtsps://192.0.2.23:7441/{token}?enableSrtp"):
        assert camera_url_has_credentials(url) and token not in sanitize_camera_url(url)
    assert not camera_url_has_credentials("rtsp://192.0.2.23:554/stream1")
    url = f"rtsp://192.0.2.23:7447/{token}"
    assert printers_client.patch("/printers/23", json={"camera_url": url}).status_code == 200
    stored = review_db.execute(text("SELECT camera_url FROM printers WHERE id=23")).scalar_one()
    assert crypto.is_encrypted(stored) and crypto.decrypt(stored) == url
    masked = printers_client.get("/printers/23").json()["camera_url"]
    assert masked == "rtsp://192.0.2.23:7447/***"
    assert printers_client.patch("/printers/23", json={"camera_url": masked}).status_code == 200  # unchanged resave
    assert crypto.decrypt(review_db.execute(text("SELECT camera_url FROM printers WHERE id=23")).scalar_one()) == url


class _Stalled(httpx.AsyncByteStream):
    """A camera that sends one frame and then stalls (no next chunk)."""

    async def __aiter__(self):
        import asyncio
        yield MJPEG
        await asyncio.Event().wait()


def test_an_evicted_stream_ends_at_once_even_while_the_camera_stalls(overlay_client, review_db, monkeypatch):
    import asyncio
    from modules.printers import camera_routes
    _, tokens, _ = overlay_client
    _go2rtc(monkeypatch, lambda r: httpx.Response(200, json={"printer_9_mjpeg": {}}) if r.url.path == "/api/streams"
            else httpx.Response(200, headers={"content-type": "multipart/x-mixed-replace; boundary=frame"}, stream=_Stalled()))
    monkeypatch.setattr(camera_routes, "OVERLAY_MAX_STREAMS_PER_PRINTER", 1)
    owner = _mint(review_db, 3, "owner-camera")  # user 3: admin of org 1

    async def run():
        viewer = await _open_stream(review_db, tokens["camera"])
        await viewer.body_iterator.__anext__()
        waiting = asyncio.ensure_future(viewer.body_iterator.__anext__())  # blocked on the stalled camera
        await asyncio.sleep(0.05)
        admitted = await _open_stream(review_db, owner)
        await admitted.body_iterator.__anext__()
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(waiting, 1)  # ends now, not on the next upstream chunk
        assert camera_routes.overlay_stream_count() == 1
        await admitted.body_iterator.aclose()
        assert camera_routes.overlay_stream_count() == 0
    asyncio.run(run())


def test_privilege_is_re_evaluated_at_every_revalidation(overlay_client, review_db, monkeypatch):
    import asyncio
    from modules.printers import camera_routes
    _, tokens, _ = overlay_client
    _endless_go2rtc(monkeypatch)
    monkeypatch.setattr(camera_routes, "OVERLAY_REVALIDATE_SECONDS", 0.1)
    owner = _mint(review_db, 3, "owner-camera")

    async def run():
        stream = await _open_stream(review_db, owner)
        await stream.body_iterator.__anext__()
        assert camera_routes._overlay_streams[0]["privileged"] is True
        review_db.execute(text("UPDATE users SET role='viewer' WHERE id=3")); review_db.commit()
        for _ in range(100):
            await asyncio.wait_for(stream.body_iterator.__anext__(), 1)
            if camera_routes._overlay_streams[0]["privileged"] is False:
                break
        assert camera_routes._overlay_streams[0]["privileged"] is False  # demoted: now evictable
        await stream.body_iterator.aclose()
        assert camera_routes.overlay_stream_count() == 0
    asyncio.run(run())
