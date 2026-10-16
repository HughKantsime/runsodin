"""Group A: credentials at rest, HMS catalog, retention, camera URL, tags/timelapse via real routers."""
import pytest
from sqlalchemy import text

from tests.test_contracts.test_cert_A_harness import (  # noqa: F401
    ACCESS, SERIAL, broker_socket, cert_app, cert_client, fernet_env, load_frames, replay, review_db, wire_monitor,
)


def test_bambu_serial_and_access_code_are_fernet_encrypted_at_rest(cert_app, review_db):
    from cryptography.fernet import Fernet
    import os
    body = {"name": "cert-p1s-07", "model": "P1S", "slot_count": 4, "api_type": "bambu",
            "api_host": "192.0.2.77", "api_key": f"{SERIAL}|{ACCESS}"}
    review_db.execute(text("UPDATE printers SET slot_count=1,tags='[]',timelapse_enabled=0,shared=0 WHERE id IN (9,10)"))
    review_db.commit()
    response = cert_app.post("/printers", headers=cert_app.roles["operator"], json=body)
    assert response.status_code == 201, response.text
    data = response.json()
    assert data["has_api_key"] is True and "api_key" not in data
    assert SERIAL not in response.text and ACCESS not in response.text
    review_db.expire_all()
    stored = review_db.execute(text("SELECT api_key FROM printers WHERE name='cert-p1s-07'")).scalar_one()
    assert SERIAL not in stored and ACCESS not in stored
    assert Fernet(os.environ["ENCRYPTION_KEY"].encode()).decrypt(stored.encode()).decode() == f"{SERIAL}|{ACCESS}"
    listing = cert_app.get("/printers", headers=cert_app.roles["viewer"])
    assert ACCESS not in listing.text and SERIAL not in listing.text


def test_bambu_monitor_loader_decrypts_credentials_for_mqtt(cert_app, review_db, monkeypatch):
    from modules.printers.monitors import mqtt_monitor
    from tests.test_contracts.test_cert_A_harness import bind_engine
    bind_engine(monkeypatch, review_db.get_bind())
    daemon = mqtt_monitor.MQTTMonitorDaemon()
    loaded = {p["id"]: p for p in daemon.load_printers()}
    assert loaded[301]["serial"] == SERIAL and loaded[301]["access_code"] == ACCESS


def test_hms_catalog_size_and_real_codes_translate(cert_app):
    from modules.printers.hms_codes import KNOWN_CODES, PRINT_ERROR_CODES, get_code_count
    from modules.notifications.error_handling import parse_hms_errors
    # Exact catalog sizes behind the "500+ HMS codes" claim.
    assert (len(KNOWN_CODES), len(PRINT_ERROR_CODES), get_code_count()) == (331, 266, 597)
    hms = [h for _, p in load_frames("bambu-h2d-failure") for h in (p.get("print", {}).get("hms") or [])][:5]
    parsed = parse_hms_errors(hms)
    assert len(parsed) == 5 and all(e["message"] and "Unknown" not in e["message"] for e in parsed)
    assert sum(e["code"] in KNOWN_CODES for e in parsed) == 2  # others use the structural decoder


def test_hms_lookup_api_returns_translation(review_db, fernet_env):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy.orm import Session
    from core.db import get_db
    from modules.system import routes_config
    from tests.test_contracts.test_cert_A_harness import bearer
    app = FastAPI()
    app.include_router(routes_config.router)

    def database():
        with Session(review_db.get_bind()) as db:
            yield db
    app.dependency_overrides[get_db] = database
    client = TestClient(app)
    body = client.get("/hms-codes/05010400_00030004", headers=bearer("student", "viewer")).json()
    assert body["message"].startswith("AMS1: Filament load/unload failure on slot 4")
    assert body["total_codes"] == 597


def test_telemetry_and_hms_retention_is_90_days_on_real_schema(wire_monitor, review_db):
    monitor, sock, clock, events = wire_monitor
    for age in (89, 91):
        review_db.execute(text("INSERT INTO printer_telemetry(printer_id,bed_temp,recorded_at) VALUES(301,:a,datetime('now',:o))"),
                          {"a": age, "o": f"-{age} days"})
        review_db.execute(text("INSERT INTO hms_error_history(printer_id,code,message,severity,source,occurred_at) VALUES(301,:c,'old','warning','bambu_hms',datetime('now',:o))"),
                          {"c": f"OLD{age}", "o": f"-{age} days"})
        review_db.execute(text("INSERT INTO ams_telemetry(printer_id,ams_unit,humidity,temperature,recorded_at) VALUES(301,0,2,:a,datetime('now',:o))"),
                          {"a": age, "o": f"-{age} days"})
    for age in (6, 8):
        review_db.execute(text("INSERT INTO ams_telemetry(printer_id,ams_unit,humidity,temperature,recorded_at) VALUES(301,0,2,:a,datetime('now',:o))"),
                          {"a": age, "o": f"-{age} days"})
    review_db.commit()
    sock.deliver({"print": {"gcode_state": "RUNNING", "bed_temper": 60, "nozzle_temper": 210,
                            "hms": [{"attr": 83952640, "code": 196612}],
                            "ams": {"ams": [{"id": "0", "humidity": "2", "temp": "25"}]}}})
    review_db.expire_all()
    assert [r[0] for r in review_db.execute(text("SELECT bed_temp FROM printer_telemetry WHERE bed_temp IN (89,91)"))] == [89]
    assert [r[0] for r in review_db.execute(text("SELECT code FROM hms_error_history WHERE code LIKE 'OLD%'"))] == ["OLD89"]
    # AMS environment: 8-day-old samples are retained (retention is 90 days, not 7).
    kept = sorted(r[0] for r in review_db.execute(text("SELECT temperature FROM ams_telemetry WHERE temperature IN (6,8,89,91)")))
    assert kept == [6, 8, 89]


def test_ams_environment_api_window_is_capped_at_7_days(cert_app):
    assert cert_app.get("/printers/301/ams/environment?hours=168", headers=cert_app.roles["viewer"]).status_code == 200
    assert cert_app.get("/printers/301/ams/environment?hours=169", headers=cert_app.roles["viewer"]).status_code == 422


def set_camera(cert_app, review_db, monkeypatch):
    import modules.printers.route_utils as route_utils
    configs = []
    monkeypatch.setattr(route_utils, "_go2rtc_restart_with_lock", lambda config, **kw: configs.append(config))
    monkeypatch.setenv("ODIN_HOST_IP", "192.0.2.250")
    review_db.execute(text("UPDATE printers SET model='P1S',machine_type='P1S' WHERE id=301"))
    review_db.commit()
    url = "rtsp://cam:secretpw@192.0.2.88:554/stream1"
    response = cert_app.patch("/printers/301", headers=cert_app.roles["operator"], json={"camera_url": url})
    assert response.status_code == 200, response.text
    return url, response, configs


def test_camera_url_field_for_p1s_is_encrypted_and_streamed(cert_app, review_db, monkeypatch):
    import core.crypto as crypto
    import modules.printers.route_utils as route_utils
    url, response, configs = set_camera(cert_app, review_db, monkeypatch)
    assert "secretpw" not in response.text
    review_db.expire_all()
    stored = review_db.execute(text("SELECT camera_url,camera_enabled FROM printers WHERE id=301")).one()
    assert "secretpw" not in stored[0] and crypto.decrypt(stored[0]) == url and stored[1] in (1, True)
    assert configs[-1]["streams"]["printer_301"] == url
    # P1S without a configured URL gets no auto RTSP stream (only X1/H2D expose RTSP).
    assert route_utils.get_camera_url(type("P", (), {"camera_url": None, "api_type": "bambu", "api_key": crypto.encrypt("a|b"),
                                                     "api_host": "192.0.2.1", "model": "P1S"})()) is None


def test_credentialed_camera_url_is_returned_masked_and_settings_resave(cert_app, review_db, monkeypatch):
    url, response, configs = set_camera(cert_app, review_db, monkeypatch)
    returned = cert_app.get("/printers/301", headers=cert_app.roles["viewer"]).json()["camera_url"]
    assert returned == "rtsp://***@192.0.2.88:554/stream1"


def test_masked_camera_url_resave_keeps_credentials_and_saves_other_settings(cert_app, review_db, monkeypatch):
    import core.crypto as crypto
    url, response, configs = set_camera(cert_app, review_db, monkeypatch)
    assert response.json()["camera_url"] == "rtsp://***@192.0.2.88:554/stream1"
    returned = cert_app.get("/printers/301", headers=cert_app.roles["viewer"]).json()["camera_url"]
    restarts = len(configs)
    resave = cert_app.patch("/printers/301", headers=cert_app.roles["operator"],
                            json={"camera_url": returned, "tags": ["farm-a"], "timelapse_enabled": True})
    assert resave.status_code == 200, resave.text
    assert resave.json()["tags"] == ["farm-a"] and resave.json()["camera_url"] == returned
    review_db.expire_all()
    assert crypto.decrypt(review_db.execute(text("SELECT camera_url FROM printers WHERE id=301")).scalar_one()) == url
    assert len(configs) == restarts  # unchanged camera does not restart go2rtc
    # Editing the path while keeping the masked credentials keeps the stored credentials.
    moved = cert_app.patch("/printers/301", headers=cert_app.roles["operator"],
                           json={"camera_url": "rtsp://***@192.0.2.88:554/stream2"})
    assert moved.status_code == 200, moved.text
    review_db.expire_all()
    assert crypto.decrypt(review_db.execute(text("SELECT camera_url FROM printers WHERE id=301")).scalar_one()) == \
        "rtsp://cam:secretpw@192.0.2.88:554/stream2"


def test_masked_camera_url_without_stored_credentials_is_rejected(cert_app):
    response = cert_app.patch("/printers/302", headers=cert_app.roles["operator"],
                              json={"camera_url": "rtsp://***@192.0.2.89:554/stream1"})
    assert response.status_code == 400


@pytest.mark.parametrize("url", ["RTSP://cam:secretpw@192.0.2.88:554/s", "Rtsps://cam:secretpw@192.0.2.88/s"])
def test_uppercase_scheme_camera_url_never_returns_password(cert_app, review_db, url):
    import core.crypto as crypto
    response = cert_app.patch("/printers/301", headers=cert_app.roles["operator"], json={"camera_url": url})
    assert response.status_code == 200, response.text
    assert "secretpw" not in response.text
    listed = cert_app.get("/printers", headers=cert_app.roles["viewer"])
    assert "secretpw" not in listed.text
    review_db.expire_all()
    stored = crypto.decrypt(review_db.execute(text("SELECT camera_url FROM printers WHERE id=301")).scalar_one())
    assert stored.split("://")[0] in ("rtsp", "rtsps") and "secretpw" in stored  # scheme normalized on store


@pytest.mark.parametrize("legacy", ["http://admin:secretpw@192.0.2.9/snap", "HTTPS://admin:secretpw@192.0.2.9/x",
                                    "rtsp://secretpw@192.0.2.9/x"])
def test_legacy_plaintext_credentialed_row_is_masked_for_any_scheme(cert_app, review_db, legacy):
    review_db.execute(text("UPDATE printers SET camera_url=:u WHERE id=301"), {"u": legacy})
    review_db.commit()
    body = cert_app.get("/printers/301", headers=cert_app.roles["viewer"])
    assert "secretpw" not in body.text and "***@192.0.2.9" in body.json()["camera_url"]


@pytest.mark.parametrize("target", ["rtsp://***@203.0.113.5:554/stream1", "rtsp://***@192.0.2.88:8554/stream1",
                                    "rtsps://***@192.0.2.88:554/stream1"])
def test_masked_camera_url_does_not_carry_credentials_to_another_endpoint(cert_app, review_db, monkeypatch, target):
    import core.crypto as crypto
    url, response, configs = set_camera(cert_app, review_db, monkeypatch)
    moved = cert_app.patch("/printers/301", headers=cert_app.roles["operator"], json={"camera_url": target})
    assert moved.status_code == 400
    review_db.expire_all()
    assert crypto.decrypt(review_db.execute(text("SELECT camera_url FROM printers WHERE id=301")).scalar_one()) == url


@pytest.mark.parametrize("body", [{"camera_url": "http://192.0.2.88/snap"}, {"camera_url": "rtsp://127.0.0.1/x"}])
def test_camera_url_rejects_non_rtsp_or_loopback(cert_app, body):
    assert cert_app.patch("/printers/301", headers=cert_app.roles["operator"], json=body).status_code == 400


def test_tags_and_timelapse_toggle_persist_per_printer(cert_app, review_db):
    response = cert_app.patch("/printers/301", headers=cert_app.roles["operator"],
                              json={"tags": ["farm-a", "petg"], "timelapse_enabled": True})
    assert response.status_code == 200 and response.json()["tags"] == ["farm-a", "petg"]
    assert response.json()["timelapse_enabled"] is True
    assert cert_app.get("/printers/tags", headers=cert_app.roles["viewer"]).json() == ["farm-a", "petg"]
    off = cert_app.patch("/printers/301", headers=cert_app.roles["operator"], json={"timelapse_enabled": False})
    assert off.json()["timelapse_enabled"] is False
    assert cert_app.patch("/printers/301", headers=cert_app.roles["viewer"], json={"tags": []}).status_code == 403


def test_nozzle_status_endpoint_reports_dual_nozzles_for_h2d(cert_app, review_db):
    review_db.execute(text("UPDATE printers SET machine_type='H2D',nozzle_temp=220,nozzle_target_temp=220,"
                           "nozzle_1_temp=180,nozzle_1_target_temp=0 WHERE id=301"))
    review_db.commit()
    body = cert_app.get("/printers/301/nozzle-status", headers=cert_app.roles["viewer"]).json()
    assert body["nozzle_count"] == 2 and body["nozzle_0"]["temp"] == 220 and body["nozzle_1"]["temp"] == 180
    single = cert_app.get("/printers/302/nozzle-status", headers=cert_app.roles["viewer"]).json()
    assert single["nozzle_count"] == 1 and single["nozzle_1"] is None


def test_plate_cleared_releases_next_waiting_job(cert_app, review_db, monkeypatch):
    from core.registry import registry
    from modules.organizations.services import EducationPolicyService
    monkeypatch.setitem(registry._providers, "EducationPolicyProvider", EducationPolicyService())
    review_db.execute(text("INSERT INTO jobs(id,item_name,status,printer_id,queue_position,charged_to_org_id,quantity,priority) VALUES "
                           "(9301,'second','waiting_plate_clear',301,2,1,1,3),(9302,'first','waiting_plate_clear',301,1,1,1,3)"))
    review_db.commit()
    assert cert_app.post("/printers/301/plate-cleared", headers=cert_app.roles["viewer"]).status_code == 403
    response = cert_app.post("/printers/301/plate-cleared", headers=cert_app.roles["operator"])
    assert response.status_code == 200 and response.json()["message"] == "Job 9302 released for dispatch"
    review_db.expire_all()
    assert dict(review_db.execute(text("SELECT id,status FROM jobs WHERE id IN (9301,9302)")).all()) == {
        9301: "waiting_plate_clear", 9302: "pending"}


def test_slot_count_shrink_keeps_ams_ht_slots(cert_app, review_db):
    review_db.execute(text("DELETE FROM filament_slots WHERE printer_id=301"))
    for number in (1, 2, 3, 4, 17):
        review_db.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type) VALUES(301,:n,'PLA')"), {"n": number})
    review_db.execute(text("UPDATE printers SET slot_count=4 WHERE id=301"))
    review_db.commit()
    response = cert_app.patch("/printers/301", headers=cert_app.roles["operator"], json={"slot_count": 2})
    assert response.status_code == 200, response.text
    review_db.expire_all()
    assert [r[0] for r in review_db.execute(text(
        "SELECT slot_number FROM filament_slots WHERE printer_id=301 ORDER BY slot_number"))] == [1, 2, 17]
    grow = cert_app.patch("/printers/301", headers=cert_app.roles["operator"], json={"slot_count": 4})
    assert grow.status_code == 200
    review_db.expire_all()
    assert [r[0] for r in review_db.execute(text(
        "SELECT slot_number FROM filament_slots WHERE printer_id=301 ORDER BY slot_number"))] == [1, 2, 3, 4, 17]



@pytest.mark.parametrize("module_name", [
    # core.db_utils is byte-pinned by migration 029 (SOURCE_PINS) and keeps its plain
    # import; the cert-A harness rebinds it (test_bind_engine_rebinds_pinned_db_utils).
    "core.ws_hub", "modules.notifications.alert_dispatch", "modules.notifications.mqtt_republish",
    "modules.notifications.quiet_hours", "modules.printers.smart_plug", "modules.printers.monitors.mqtt_monitor",
    "modules.printers.monitors.moonraker_monitor", "modules.printers.monitors.prusalink_monitor",
    "modules.printers.monitors.elegoo_monitor",
])
def test_long_lived_modules_resolve_the_engine_at_use_time(monkeypatch, module_name):
    """A module must not keep the Engine that core.db.engine named when it was imported."""
    import importlib
    from sqlalchemy import create_engine
    import core.db as core_db
    module = importlib.import_module(module_name)
    replacement = create_engine("sqlite://")
    monkeypatch.setattr(core_db, "engine", replacement)
    with module.engine.connect() as conn:
        assert conn.engine is replacement


def test_bind_engine_rebinds_pinned_db_utils(monkeypatch, tmp_path):
    """The raw get_db() of the byte-pinned core.db_utils follows the harness's owned engine."""
    from sqlalchemy import create_engine, text
    import core.db_utils as db_utils
    from tests.test_contracts.test_cert_A_harness import bind_engine
    owned = create_engine(f"sqlite:///{tmp_path / 'owned.db'}")
    with owned.begin() as conn:
        conn.execute(text("CREATE TABLE marker (v TEXT)"))
        conn.execute(text("INSERT INTO marker VALUES ('owned')"))
    bind_engine(monkeypatch, owned)
    with db_utils.get_db() as conn:
        assert conn.execute("SELECT v FROM marker").fetchone()[0] == "owned"


@pytest.mark.parametrize("module_name", [
    # modules.notifications.job_events is byte-pinned by test_committed_completion_boundary
    # and keeps its plain import; the cert-A harness rebinds it.
    "modules.archives.archive", "modules.notifications.printer_health",
    "modules.notifications.error_handling", "modules.notifications.channels",
    "modules.printers.monitors.mqtt_job_lifecycle", "modules.printers.monitors.mqtt_printer",
])
def test_modules_resolve_get_db_at_call_time(monkeypatch, module_name):
    """A module must not keep the get_db that core.db_utils exposed when it was imported."""
    import importlib
    from contextlib import contextmanager
    import core.db_utils as db_utils
    module = importlib.import_module(module_name)
    marker = object()

    @contextmanager
    def replacement(*args, **kwargs):
        yield marker
    monkeypatch.setattr(db_utils, "get_db", replacement)
    with module.get_db() as conn:
        assert conn is marker


def test_live_engine_stays_out_of_engine_type_checks():
    """core.db.live_engine is an internal proxy: it fails isinstance(Engine)/inspect,
    so modules that type-check their engine must never import or receive it."""
    import re
    from pathlib import Path
    from sqlalchemy.engine import Engine
    import core.db as core_db
    assert not isinstance(core_db.live_engine, Engine)
    assert isinstance(core_db.current_engine(), Engine)
    backend = Path(__file__).resolve().parents[2] / "backend"
    users, checkers = set(), set()
    for path in backend.rglob("*.py"):
        if "preserved" in path.parts:
            continue
        source = path.read_text(errors="ignore")
        name = str(path.relative_to(backend))
        if "live_engine" in source and name != "core/db.py":
            users.add(name)
        if re.search(r"isinstance\(\s*engine\s*,\s*Engine\s*\)|inspect\(\s*engine\s*\)", source):
            checkers.add(name)
    assert users and not users & checkers, users & checkers
    assert not any("timelapse" in name for name in users)
    # Long-lived modules only hand the proxy to helpers that use it through attributes.
    for name in users:
        source = (backend / name).read_text(errors="ignore")
        for call in re.findall(r"(\w+)\(engine\b", source):
            assert call in {"bind_generic_observation", "retire_observed_terminal", "Session"}, (name, call)

