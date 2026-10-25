"""Group A: Bambu printer controls end-to-end to the MQTT wire.

Real FastAPI routers, real auth (JWT role users), real bootstrapped schema,
real Fernet decrypt, real route helper and real legacy BambuPrinter adapter
(the shipped default, ODIN_TELEMETRY_V2=0). Only paho's Client object is
replaced, recording exactly what would be written to the printer's broker.
"""
import pytest
from sqlalchemy import text

from tests.test_contracts.test_cert_A_harness import (  # noqa: F401
    ACCESS, SERIAL, broker_socket, cert_app, cert_client, commands, fernet_env, review_db,
)

REQUEST_TOPIC = f"device/{SERIAL}/request"


def gcode_state(db, printer_id=301):
    db.expire_all()
    return db.execute(text("SELECT gcode_state FROM printers WHERE id=:id"), {"id": printer_id}).scalar_one()


CONTROL_CASES = [
    ("clear-errors", None, {"print": {"sequence_id": "0", "command": "clean_print_error"}}),
    ("pause", None, {"print": {"sequence_id": "0", "command": "pause"}}),
    ("resume", None, {"print": {"sequence_id": "0", "command": "resume"}}),
    ("stop", None, {"print": {"sequence_id": "0", "command": "stop"}}),
    ("skip-objects", {"object_ids": [3, 0]},
     {"print": {"sequence_id": "0", "command": "skip_objects", "obj_list": ["3", "0"]}}),
    ("speed", {"speed": 4}, {"print": {"sequence_id": "0", "command": "print_speed", "param": "4"}}),
    ("fan", {"fan": "part_cooling", "speed": 255},
     {"print": {"sequence_id": "0", "command": "gcode_line", "param": "M106 P1 S255"}}),
    ("fan", {"fan": "auxiliary", "speed": 0},
     {"print": {"sequence_id": "0", "command": "gcode_line", "param": "M106 P2 S0"}}),
    ("fan", {"fan": "chamber", "speed": 128},
     {"print": {"sequence_id": "0", "command": "gcode_line", "param": "M106 P3 S128"}}),
    ("ams/refresh", None,
     {"print": {"sequence_id": "0", "command": "ams_change_filament", "target": 255, "curr_temp": 0, "tar_temp": 0}}),
]


@pytest.mark.parametrize("role", ["operator", "admin"])
@pytest.mark.parametrize("endpoint,body,wire", CONTROL_CASES)
def test_operator_and_admin_controls_reach_bambu_mqtt_wire(cert_app, broker_socket, role, endpoint, body, wire):
    response = cert_app.post(f"/printers/301/{endpoint}", headers=cert_app.roles[role], json=body)
    assert response.status_code == 200, response.text
    assert response.json()["success"] is True
    assert commands() == [(REQUEST_TOPIC, wire)]
    sock = broker_socket.instances[0]
    # Connection contract: printer LAN broker, MQTT over TLS 8883, MQTT v3.1.1,
    # username bblp + access code decrypted from the Fernet-encrypted credential.
    import paho.mqtt.client as paho
    assert sock.endpoint == ("192.0.2.31", 8883, 60)
    assert sock.protocol == paho.MQTTv311
    assert sock.tls and sock.tls_insecure is True
    assert sock.credentials == ("bblp", ACCESS)
    assert sock.subscriptions == [f"device/{SERIAL}/report"]
    assert sock.disconnected and sock.loop_stopped


@pytest.mark.parametrize("endpoint,body,_wire", CONTROL_CASES)
def test_viewer_cannot_command_and_nothing_reaches_wire(cert_app, broker_socket, review_db, endpoint, body, _wire):
    response = cert_app.post(f"/printers/301/{endpoint}", headers=cert_app.roles["viewer"], json=body)
    assert response.status_code == 403
    assert broker_socket.instances == []
    assert gcode_state(review_db) == "RUNNING"


@pytest.mark.parametrize("endpoint,state", [("pause", "PAUSED"), ("resume", "RUNNING"), ("stop", "IDLE")])
def test_pause_resume_stop_persist_state_after_wire_ack(cert_app, broker_socket, review_db, endpoint, state):
    review_db.execute(text("UPDATE printers SET gcode_state='PAUSED' WHERE id=301")) if endpoint == "resume" else None
    review_db.commit()
    assert cert_app.post(f"/printers/301/{endpoint}", headers=cert_app.roles["operator"]).status_code == 200
    assert gcode_state(review_db) == state


@pytest.mark.parametrize("endpoint,body", [(c[0], c[1]) for c in CONTROL_CASES])
def test_broker_refusal_returns_503_without_state_change(cert_app, broker_socket, review_db, endpoint, body):
    broker_socket.refuse = True
    response = cert_app.post(f"/printers/301/{endpoint}", headers=cert_app.roles["operator"], json=body)
    assert response.status_code == 503
    assert commands() == []
    assert gcode_state(review_db) == "RUNNING"


@pytest.mark.parametrize("endpoint,body", [
    ("clear-errors", None), ("skip-objects", {"object_ids": [0]}), ("speed", {"speed": 2}),
    ("fan", {"fan": "chamber", "speed": 10}), ("ams/refresh", None), ("lights", None),
])
def test_bambu_only_controls_reject_non_bambu_printers(cert_app, broker_socket, endpoint, body):
    response = cert_app.post(f"/printers/302/{endpoint}", headers=cert_app.roles["operator"], json=body)
    assert response.status_code == 400
    assert broker_socket.instances == []


@pytest.mark.parametrize("body", [{"speed": 25}, {"speed": 100}, {"speed": 200}, {"speed": 0}, {"speed": 5}])
def test_speed_accepts_only_four_bambu_modes_not_percentages(cert_app, broker_socket, body):
    """Claim says 25%-200%; the API accepts only mode levels 1-4."""
    response = cert_app.post("/printers/301/speed", headers=cert_app.roles["operator"], json=body)
    assert response.status_code == 422
    assert broker_socket.instances == []


@pytest.mark.parametrize("level,name", [(1, "Silent"), (2, "Standard"), (3, "Sport"), (4, "Ludicrous")])
def test_speed_modes_map_to_bambu_print_speed_param(cert_app, broker_socket, level, name):
    response = cert_app.post("/printers/301/speed", headers=cert_app.roles["operator"], json={"speed": level})
    assert response.status_code == 200 and response.json()["message"] == f"Speed set to {name}"
    assert commands() == [(REQUEST_TOPIC, {"print": {"sequence_id": "0", "command": "print_speed", "param": str(level)}})]


@pytest.mark.parametrize("body", [{"fan": "chamber", "speed": 256}, {"fan": "chamber", "speed": -1},
                                  {"fan": "nozzle", "speed": 10}])
def test_fan_bounds_0_to_255_and_three_named_fans(cert_app, broker_socket, body):
    assert cert_app.post("/printers/301/fan", headers=cert_app.roles["operator"], json=body).status_code == 422
    assert broker_socket.instances == []


def test_ams_slot_configuration_sends_ams_filament_setting(cert_app, broker_socket):
    response = cert_app.put("/printers/301/ams/1/slots/2", headers=cert_app.roles["operator"],
                            json={"material": "PETG", "color": "#ff5500", "k_factor": 0.028})
    assert response.status_code == 200, response.text
    assert commands() == [(REQUEST_TOPIC, {"print": {
        "sequence_id": "0", "command": "ams_filament_setting", "ams_id": 1, "tray_id": 6,
        "tray_info_idx": "PETG", "tray_color": "FF5500FF", "nozzle_temp_min": 190, "nozzle_temp_max": 240,
        "tray_type": "PETG", "setting_id": "", "k": 0.028}})]


def test_ams_slot_configuration_with_spool_updates_inventory_slot(cert_app, broker_socket, review_db):
    review_db.execute(text("INSERT INTO spools(id,filament_id,org_id,status) SELECT 41,id,1,'active' FROM filament_library LIMIT 1"))
    if review_db.execute(text("SELECT COUNT(*) FROM spools WHERE id=41")).scalar_one() == 0:
        review_db.execute(text("INSERT INTO filament_library(id,brand,name,material) VALUES(41,'Cert','Cert PETG','PETG')"))
        review_db.execute(text("INSERT INTO spools(id,filament_id,org_id,status) VALUES(41,41,1,'active')"))
    review_db.execute(text("INSERT INTO filament_slots(printer_id,slot_number,filament_type) VALUES(301,5,'PLA')"))
    review_db.commit()
    response = cert_app.put("/printers/301/ams/1/slots/1", headers=cert_app.roles["admin"],
                            json={"material": "PETG", "color": "#00FF00", "k_factor": 0.02, "spool_id": 41})
    assert response.status_code == 200, response.text
    review_db.expire_all()
    row = review_db.execute(text("SELECT filament_type,color,assigned_spool_id FROM filament_slots WHERE printer_id=301 AND slot_number=5")).one()
    assert (str(row[0]).split(".")[-1].upper(), row[1], row[2]) == ("PETG", "#00FF00", 41)
    assert commands()[0][1]["print"]["tray_id"] == 5


@pytest.mark.parametrize("body", [{"material": "PLA", "color": "red"}, {"material": "PLA", "k_factor": -1},
                                  {"material": "", "color": "#FFFFFF"}])
def test_ams_slot_configuration_rejects_invalid_values_before_wire(cert_app, broker_socket, body):
    assert cert_app.put("/printers/301/ams/0/slots/0", headers=cert_app.roles["operator"], json=body).status_code == 422
    assert broker_socket.instances == []


@pytest.mark.parametrize("ams_id,slot_id", [(4, 0), (0, 4)])
def test_ams_slot_configuration_rejects_out_of_range_slot(cert_app, broker_socket, ams_id, slot_id):
    assert cert_app.put(f"/printers/301/ams/{ams_id}/slots/{slot_id}", headers=cert_app.roles["operator"],
                        json={"material": "PLA"}).status_code == 422
    assert broker_socket.instances == []


def test_ams_slot_configuration_requires_operator(cert_app, broker_socket):
    assert cert_app.put("/printers/301/ams/0/slots/0", headers=cert_app.roles["viewer"],
                        json={"material": "PLA"}).status_code == 403
    assert broker_socket.instances == []


@pytest.mark.parametrize("role", ["operator", "admin"])
def test_light_toggle_sends_chamber_ledctrl_and_flips_state(cert_app, broker_socket, review_db, role, monkeypatch):
    from threading import Lock
    from types import SimpleNamespace
    import time
    from modules.printers.adapters.bambu import BambuPrinter
    from modules.printers import monitor_commands
    # The monitor owns the one permitted MQTT connection. Any extra session fails.
    transport = BambuPrinter(ip='192.0.2.31', serial=SERIAL, access_code=ACCESS)
    assert transport.connect()
    monitor = SimpleNamespace(_lock=Lock(), _bambu=transport, serial=SERIAL)
    import paho.mqtt.client as paho
    def no_extra_connection(*args, **kwargs):
        pytest.fail('Light command opened a second MQTT connection')
    monkeypatch.setattr(paho, 'Client', no_extra_connection)
    monkeypatch.setattr(monitor_commands, 'request_command', lambda pid, action, **kw:
        monitor_commands.execute({301: monitor}, dict(printer_id=pid, action=action,
            deadline=time.monotonic()+3, **kw)))
    review_db.execute(text("UPDATE printers SET lights_on=0 WHERE id=301"))
    review_db.commit()
    first = cert_app.post("/printers/301/lights", headers=cert_app.roles[role])
    assert first.status_code == 200 and first.json()["lights_on"] is True
    second = cert_app.post("/printers/301/lights", headers=cert_app.roles[role])
    assert second.status_code == 200 and second.json()["lights_on"] is False
    assert [body for _, body in commands()] == [
        {"system": {"sequence_id": "0", "command": "ledctrl", "led_node": "chamber_light", "led_mode": "on"}},
        {"system": {"sequence_id": "0", "command": "ledctrl", "led_node": "chamber_light", "led_mode": "off"}},
    ]
    assert len(broker_socket.instances) == 1
    transport.disconnect()
    review_db.expire_all()
    row = review_db.execute(text("SELECT lights_on,lights_toggled_at FROM printers WHERE id=301")).one()
    assert row[0] in (0, False) and row[1] is not None


def test_light_toggle_viewer_forbidden(cert_app, broker_socket):
    assert cert_app.post("/printers/301/lights", headers=cert_app.roles["viewer"]).status_code == 403
    assert broker_socket.instances == []


def test_foreign_org_printer_is_invisible_to_controls(cert_app, broker_socket, review_db):
    review_db.execute(text("UPDATE printers SET org_id=2 WHERE id=301"))
    review_db.commit()
    for endpoint, body, _ in CONTROL_CASES:
        assert cert_app.post(f"/printers/301/{endpoint}", headers=cert_app.roles["operator"], json=body).status_code == 404
    assert broker_socket.instances == []
