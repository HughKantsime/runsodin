"""Capture the real printer HTTP/WebSocket producer shapes consumed by the group A UI cert tests.

Producers: legacy Bambu monitor fed captured A1/H2D MQTT frames, Moonraker /
PrusaLink / Elegoo monitors fed protocol-accurate transport payloads, then
the real printer routers. Wall-clock timestamps are normalized to a fixed
base so the fixture is reproducible; every other value is as produced.
"""
import json
from pathlib import Path

from sqlalchemy import text

from tests.test_contracts.test_cert_A_harness import (  # noqa: F401
    ACCESS, SERIAL, broker_socket, cert_client, fernet_env, load_frames, replay, review_db, wire_monitor,
)
from tests.test_contracts.test_cert_A_protocols import (  # noqa: F401
    elegoo, moonraker, proto, prusalink, sdcp_frame,
)

FIXTURE = Path(__file__).resolve().parents[2] / "frontend/src/test-fixtures/cert-A-printers-wire.json"
BASE = "2026-10-14 12:00:00"
VOLATILE = {"last_seen", "loaded_at", "recorded_at", "occurred_at", "time", "installed_at", "removed_at", "created_at",
            "updated_at", "lights_toggled_at", "last_error_at", "nozzle_1_temp_observed_at", "nozzle_1_target_observed_at"}


def normalize(value, counter):
    if isinstance(value, dict):
        return {k: ((BASE.replace(" ", "T") if k == "last_seen" else stamp(counter)) if k in VOLATILE and v
                    else normalize(v, counter)) for k, v in value.items()}
    if isinstance(value, list):
        return [normalize(v, counter) for v in value]
    if isinstance(value, str) and value.startswith("gAAAA"):
        return "<fernet-ciphertext as returned by the API>"  # random IV per run
    return value


def differences(a, b, path=""):
    if isinstance(a, dict) and isinstance(b, dict):
        return [d for k in sorted(set(a) | set(b)) for d in differences(a.get(k), b.get(k), f"{path}.{k}")]
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        return [d for i, (x, y) in enumerate(zip(a, b)) for d in differences(x, y, f"{path}[{i}]")]
    return [] if a == b else [(path, a, b)]


def stamp(counter):
    from datetime import datetime, timedelta
    counter[0] += 1
    return (datetime.fromisoformat(BASE) - timedelta(seconds=60 * (100 - counter[0]))).strftime("%Y-%m-%dT%H:%M:%S")


def test_capture_cert_A_printer_wire(cert_client, wire_monitor, proto, moonraker, prusalink, elegoo, review_db):
    client = cert_client
    roles = client.roles
    monitor, sock, clock, events = wire_monitor
    # Bambu A1: captured frames through the active print (progress, speed, AMS, lights).
    frames = load_frames("bambu-a1-happy-path")
    start = next(i for i, (_, p) in enumerate(frames) if p.get("print", {}).get("gcode_state") == "RUNNING")
    replay(sock, clock, frames[: start + 600])
    # H2D HMS codes captured from a real failure, delivered to the same printer.
    hms = [p for _, p in load_frames("bambu-h2d-failure") if p.get("print", {}).get("hms")][0]
    clock[0] += 11
    sock.deliver({"print": {"hms": hms["print"]["hms"], "gcode_state": "RUNNING"}})
    # AMS 1 environment from a captured X1C report (AMS Lite on the A1 has no sensors).
    x1c = load_frames("bambu-x1c-ams-swap")[0][1]
    clock[0] += 301
    sock.deliver({"print": {"ams": x1c["print"]["ams"], "gcode_state": "RUNNING"}})
    import core.crypto as crypto
    review_db.execute(text("UPDATE printers SET tags='[\"farm-a\"]',model='A1',api_key=:k WHERE id=301"),
                      {"k": crypto.encrypt(f"{SERIAL}|{ACCESS}")})
    review_db.execute(text("UPDATE printers SET tags='[\"klipper\"]', nickname='Voron Workshop' WHERE id=401"))
    review_db.commit()
    # Other protocols: one printing observation each.
    moonraker.poll()
    prusalink.run(1)
    elegoo.push(sdcp_frame(print_status=8))
    nozzle = client.post("/printers/301/nozzle", headers=roles["operator"],
                         json={"nozzle_type": "hardened_steel", "nozzle_diameter": 0.4, "notes": ""})
    assert nozzle.status_code == 200, nozzle.text
    review_db.execute(text("UPDATE nozzle_lifecycle SET print_hours_accumulated=12.5,print_count=3 WHERE printer_id=301"))
    review_db.commit()

    printers = client.get("/printers", headers=roles["operator"])
    assert printers.status_code == 200, printers.text
    listed = {p["id"]: p for p in printers.json() if p["id"] in (301, 401, 402, 403)}
    assert set(listed) == {301, 401, 402, 403}
    wire = {
        "scope": "Owned real-router responses after real monitor ingestion of captured Bambu A1/H2D MQTT frames "
                 "and protocol-accurate Moonraker/PrusaLink/SDCP payloads. Timestamps normalized: last_seen = "
                 f"{BASE} UTC (just reported), others whole minutes before it.",
        "printers": [listed[i] for i in (301, 401, 402, 403)],
        "telemetry_301": client.get("/printers/301/telemetry?hours=24", headers=roles["viewer"]).json(),
        "hms_301": client.get("/printers/301/hms-history?days=30", headers=roles["viewer"]).json(),
        "ams_environment_301": client.get("/printers/301/ams/environment?hours=24", headers=roles["viewer"]).json(),
        "nozzle_301": client.get("/printers/301/nozzle", headers=roles["viewer"]).json(),
        "nozzle_history_301": client.get("/printers/301/nozzle/history", headers=roles["viewer"]).json(),
        "ws_301": [p for kind, p in events if kind == "printer_telemetry" and p["printer_id"] == 301][-1],
        "ws_other": {str(pid): [p for kind, p in proto.events if kind == "printer_telemetry" and p["printer_id"] == pid][-1]
                     for pid in (401, 402, 403)},
    }
    assert wire["telemetry_301"] and wire["hms_301"]["total"] == 5 and wire["ams_environment_301"]["units"]
    assert wire["ws_301"]["progress"] is not None and wire["ws_301"]["speed_percent"] is not None
    counter = [0]
    fixture = normalize(json.loads(json.dumps(wire)), counter)
    if FIXTURE.exists():
        changed = differences(json.loads(FIXTURE.read_text()), fixture)
        assert not changed, f"Producer contract changed; reconcile the consumer explicitly: {changed[:10]}"
    else:
        FIXTURE.write_text(json.dumps(fixture, indent=2) + "\n")
