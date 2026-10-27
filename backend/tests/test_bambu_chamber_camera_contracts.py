"""Camera resolution, API visibility and relay admission with synthetic credentials."""
from datetime import datetime
import json
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from sqlalchemy import text

from modules.printers import bambu_chamber_camera as chamber
from modules.printers import route_utils
from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401
from tests.test_contracts.test_cert_A_harness import cert_client, fernet_env  # noqa: F401


@pytest.fixture(autouse=True)
def test_credentials(monkeypatch):
    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    from modules.printers import camera_relay
    # Do not read any installed relay secret or boot files.
    monkeypatch.setattr(camera_relay, "relay_secret", lambda: "synthetic-relay-key" * 3)


def printer(**changes):
    from core.crypto import encrypt
    data = dict(id=7, name="Synthetic", model="P1S", api_type="bambu", api_host="192.0.2.7",
                api_key=encrypt("SYNTHETIC|test-code"), camera_url=None, camera_enabled=True,
                is_active=True, created_at=datetime.now(), updated_at=datetime.now())
    data.update(changes)
    return SimpleNamespace(**data)


@pytest.mark.parametrize("model", ["P1P", "P1S", "A1", "A1 Mini", "A1 mini", "A1 MINI", "N1", "C12", "Bambu Lab A1"])
def test_normalized_models_have_auto_camera(model):
    from modules.vision.camera_source import camera_source
    from modules.printers.schemas import PrinterResponse
    p = printer(model=model)
    assert route_utils.BAMBU_CHAMBER_CAMERA_MODELS == {"P1P", "P1S", "A1", "A1 Mini"}
    assert route_utils.get_camera_url(p) == chamber.SOURCE
    assert route_utils.effective_camera_url(p) == chamber.SOURCE
    assert camera_source(p) == camera_source(vars(p)) == "auto"
    response = PrinterResponse.model_validate(p).model_dump_json()
    assert '"camera_source":"auto"' in response and "test-code" not in response
    assert "api_key" not in json.loads(response)


@pytest.mark.parametrize("changes", [
    {"api_key": None}, {"api_key": "SYNTHETIC|"}, {"api_key": "|test-code"},
    {"api_key": "bad"}, {"api_key": "SYNTHETIC|" + "x" * 33}, {"api_host": None},
    {"model": "P2S"}, {"model": "H2S"}, {"model": "unknown"}, {"api_type": "moonraker"},
])
def test_missing_credentials_and_unlisted_models_are_not_auto(changes):
    from modules.vision.camera_source import camera_source
    p = printer(**changes)
    assert route_utils.get_camera_url(p) is None
    assert camera_source(p) is None


def test_stored_camera_wins_and_rtsps_is_unchanged():
    from core.crypto import encrypt
    from modules.vision.camera_source import camera_source
    p = printer(model="A1", camera_url=encrypt("rtsps://192.0.2.8:7441/unifi-camera"))
    assert route_utils.get_camera_url(p) == "rtsps://192.0.2.8:7441/unifi-camera"
    assert camera_source(p) == "configured" and chamber.chamber_source(p) is None
    assert route_utils.get_camera_url(printer(model="X1C")) == "rtsps://bblp:test-code@192.0.2.7:322/streaming/live/1"


def test_go2rtc_only_receives_relay_urls():
    from modules.printers.camera_relay import relay_url
    config = route_utils.go2rtc_config_for([printer()], "192.0.2.10")
    assert config["streams"] == {"printer_7": f"ffmpeg:{relay_url(7)}#video=h264",
                                  "printer_7_mjpeg": relay_url(7)}
    serialized = json.dumps(config)
    assert "test-code" not in serialized and "192.0.2.7" not in serialized
    assert chamber.SOURCE not in serialized


@pytest.mark.parametrize("value", [chamber.SOURCE, "bambu-chamber://other", "http://192.0.2.7/cam"])
def test_internal_source_is_not_a_user_camera_url(value):
    with pytest.raises(HTTPException) as error:
        route_utils._validate_camera_url(value)
    assert error.value.status_code == 400


def test_internal_source_cannot_be_submitted_or_stored(cert_client, review_db):
    headers = cert_client.roles["admin"]
    response = cert_client.patch("/printers/9", headers=headers, json={"camera_url": chamber.SOURCE})
    assert response.status_code == 400
    response = cert_client.post("/printers", headers=headers, json={"name": "Rejected", "camera_url": chamber.SOURCE})
    assert response.status_code == 400
    assert review_db.execute(text("SELECT camera_url FROM printers WHERE id=9")).scalar_one() is None


def test_auto_marker_sent_back_unchanged_stores_nothing(cert_client, camera_db):
    response = cert_client.patch("/printers/9", headers=cert_client.roles["admin"], json={"camera_url": chamber.SOURCE})
    assert response.status_code == 200
    assert camera_db.execute(text("SELECT camera_url FROM printers WHERE id=9")).scalar_one() is None


@pytest.fixture
def camera_db(review_db, monkeypatch):
    from core.crypto import encrypt
    # Use the same DBAPI bridge as the existing camera-relay contracts.
    from tests.test_contracts.test_cert_A_harness import bind_engine
    bind_engine(monkeypatch, review_db.get_bind())
    review_db.execute(text("UPDATE printers SET model='P1S',api_host='192.0.2.7',api_key=:key,"
                           "camera_url=NULL,camera_enabled=1,is_active=1 WHERE id=9"),
                      {"key": encrypt("SYNTHETIC|test-code")})
    review_db.commit()
    return review_db


def test_camera_list_and_relay_recheck_current_configuration(camera_db, monkeypatch):
    from modules.printers import camera_routes, camera_relay
    monkeypatch.setattr(camera_routes, "sync_go2rtc_config", lambda db: None)
    def camera_ids():
        return [p["id"] for p in camera_routes.list_cameras(
            db=camera_db, current_user={"id": 3, "role": "admin", "group_id": None})]
    assert 9 in camera_ids()
    assert camera_relay._stored_chamber_camera(9) == chamber.ChamberSource("192.0.2.7", "test-code")
    for field, value in (("camera_enabled", 0), ("is_active", 0), ("api_key", None),
                         ("model", "P2S"), ("camera_url", "rtsps://192.0.2.8:7441/unifi-camera")):
        previous = camera_db.execute(text(f"SELECT {field} FROM printers WHERE id=9")).scalar_one()
        camera_db.execute(text(f"UPDATE printers SET {field}=:value WHERE id=9"), {"value": value})
        camera_db.commit()
        assert camera_relay._stored_chamber_camera(9) is None
        if field != "camera_url":
            assert 9 not in camera_ids()
        camera_db.execute(text(f"UPDATE printers SET {field}=:value WHERE id=9"), {"value": previous})
        camera_db.commit()
        assert camera_relay._stored_chamber_camera(9) is not None


@pytest.mark.parametrize("peer,headers,token", [
    ("203.0.113.1", [], "test-token"), ("127.0.0.1", [], "wrong"),
    ("127.0.0.1", [(b"x-forwarded-for", b"203.0.113.1")], "test-token"),
])
def test_relay_peer_and_token_guard_still_precede_source_lookup(monkeypatch, peer, headers, token):
    from modules.printers import camera_relay
    from starlette.requests import Request
    monkeypatch.setattr(camera_relay, "relay_token", lambda pid: "test-token")
    monkeypatch.setattr(camera_relay, "_stored_chamber_camera", lambda pid: pytest.fail("unauthorized lookup"))
    request = Request({"type": "http", "headers": headers, "client": (peer, 1234)})
    with pytest.raises(HTTPException) as error:
        camera_relay.camera_source(7, token, request)
    assert error.value.status_code == 404
