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
                           "(22,'P1S no camera','P1S','bambu','192.0.2.22',:key,1,1,1,4,0,0,'[]',CURRENT_TIMESTAMP,CURRENT_TIMESTAMP),"
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
    assert rows[22]["camera_url"] is None and rows[22]["camera_source"] is None  # P1S: no RTSP camera
    # NULL camera_enabled streams nowhere (go2rtc selects TRUE), so it reads as disabled.
    assert rows[23]["camera_enabled"] is False and rows[23]["camera_source"] == "configured"


def test_resaving_masked_auto_camera_keeps_it_generated(printers_client, review_db):
    masked = printers_client.get("/printers/21").json()["camera_url"]
    response = printers_client.patch("/printers/21", json={"camera_url": masked, "nickname": "Bay 1"})
    assert response.status_code == 200, response.text
    assert review_db.execute(text("SELECT camera_url FROM printers WHERE id=21")).scalar_one() is None
    # Another host with the masked access code is refused (credentials only follow the same endpoint)...
    response = printers_client.patch("/printers/21", json={"camera_url": "rtsps://***@192.0.2.99:322/streaming/live/1"})
    assert response.status_code == 400, response.text
    # ...while a path change on the generated endpoint keeps the access code.
    response = printers_client.patch("/printers/21", json={"camera_url": "rtsps://***@192.0.2.21:322/streaming/live/2"})
    assert response.status_code == 200, response.text
    from core import crypto
    stored = crypto.decrypt(review_db.execute(text("SELECT camera_url FROM printers WHERE id=21")).scalar_one())
    assert stored.startswith("rtsps://bblp:") and stored.endswith("@192.0.2.21:322/streaming/live/2")


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
    for user_id, scopes, name in ((1, '["read:cameras"]', "camera"), (1, '["read:jobs"]', "jobs"), (5, '["read:cameras"]', "foreign")):
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
    from modules.printers.route_utils import _build_go2rtc_config
    streams = _build_go2rtc_config(review_db)["streams"]
    assert streams["printer_21_mjpeg"] == "ffmpeg:printer_21#video=mjpeg"  # RTSPS X1C: transcode on demand
    assert streams["printer_23"] == "ffmpeg:http://192.0.2.24:8080/webcam/?action=stream#video=h264"
    assert streams["printer_23_mjpeg"] == "http://192.0.2.24:8080/webcam/?action=stream"  # MJPEG webcam as is
    assert "printer_22" not in streams and "printer_22_mjpeg" not in streams
