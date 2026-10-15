"""Group A: Bambu telemetry certified at the MQTT wire with captured frames.

Captured LAN MQTT report frames (tests/fixtures/telemetry, real A1 / H2D /
X1C printers) are delivered through a paho-shaped socket into the shipped
default pipeline: BambuPrinter._on_message -> _parse_status ->
PrinterMonitor._on_status -> real bootstrapped schema. Only the broker
socket and the go2rtc process are substituted.
"""
import json

import pytest
from sqlalchemy import text

from tests.test_contracts.test_cert_A_harness import (  # noqa: F401
    ACCESS, SERIAL, broker_socket, fernet_env, load_frames, replay, review_db, wire_monitor,
)


def merged(frames):
    state = {}
    for _, payload in frames:
        state.update(payload.get("print", {}))
    return state


def printer_row(db, columns):
    db.expire_all()
    return dict(db.execute(text(f"SELECT {columns} FROM printers WHERE id=301")).mappings().one())


def frames_until(name, predicate):
    frames = load_frames(name)
    for index, (_, payload) in enumerate(frames):
        if predicate(payload.get("print", {})):
            return frames[: index + 1]
    raise AssertionError("fixture never reached predicate")


def test_a1_capture_persists_live_temperatures_fan_and_heartbeat(wire_monitor, review_db):
    monitor, sock, clock, events = wire_monitor
    frames = load_frames("bambu-a1-happy-path")[:400]
    replay(sock, clock, frames)
    # The heartbeat persists every 10s; flush the latest merged state.
    clock[0] += 11
    sock.deliver({"print": {"command": "push_status", "msg": 1, "sequence_id": "cert"}})
    expected = merged(frames)
    row = printer_row(review_db, "bed_temp,bed_target_temp,nozzle_temp,nozzle_target_temp,fan_speed,last_seen,gcode_state")
    assert row["bed_temp"] == pytest.approx(float(expected["bed_temper"]))
    assert row["nozzle_temp"] == pytest.approx(float(expected["nozzle_temper"]))
    assert row["bed_target_temp"] == pytest.approx(float(expected["bed_target_temper"]))
    assert row["nozzle_target_temp"] == pytest.approx(float(expected["nozzle_target_temper"]))
    assert row["fan_speed"] == pytest.approx(float(expected["cooling_fan_speed"]))
    assert row["gcode_state"] == expected["gcode_state"]
    assert row["last_seen"] is not None
    pushed = [payload for kind, payload in events if kind == "printer_telemetry"]
    assert pushed and pushed[-1]["nozzle_temp"] == expected["nozzle_temper"]
    assert pushed[-1]["bed_temp"] == expected["bed_temper"]


def test_a1_capture_snapshots_every_60_seconds_only_while_active(wire_monitor, review_db):
    monitor, sock, clock, events = wire_monitor
    frames = load_frames("bambu-a1-happy-path")
    start = next(ts for ts, p in frames if p.get("print", {}).get("gcode_state") in ("PREPARE", "RUNNING"))
    end = start + 30 * 60
    window = [(ts, p) for ts, p in frames if ts <= end]
    replay(sock, clock, window)
    review_db.expire_all()
    rows = review_db.execute(text("SELECT bed_temp,nozzle_temp,fan_speed FROM printer_telemetry WHERE printer_id=301")).all()
    active_seconds = end - start
    # One sample per >=60s window: never more than one per minute, and the 10s
    # heartbeat granularity means at least one per 70s of active printing.
    assert int(active_seconds // 70) <= len(rows) <= int(active_seconds // 60) + 1
    assert all(r[1] is not None and r[0] is not None for r in rows)
    assert any(r[2] is not None for r in rows)
    # Nothing was captured during the idle frames that preceded the print.
    idle = [(ts, p) for ts, p in frames if ts < start]
    assert idle and (start - idle[0][0]) > 0


def test_idle_capture_records_no_snapshots(wire_monitor, review_db):
    monitor, sock, clock, events = wire_monitor
    frames = [(ts, p) for ts, p in load_frames("bambu-a1-happy-path")[:200]
              if p.get("print", {}).get("gcode_state", "IDLE") in ("IDLE", "FINISH")]
    replay(sock, clock, frames)
    assert review_db.execute(text("SELECT COUNT(*) FROM printer_telemetry")).scalar_one() == 0


def test_a1_full_print_lifecycle_tracks_progress_and_completion(wire_monitor, review_db):
    monitor, sock, clock, events = wire_monitor
    frames = load_frames("bambu-a1-happy-path")
    replay(sock, clock, frames)
    review_db.expire_all()
    jobs = review_db.execute(text("SELECT status,progress_percent,started_at,ended_at FROM print_jobs WHERE printer_id=301")).all()
    assert [j[0] for j in jobs] == ["completed"]
    assert jobs[0][2] is not None and jobs[0][3] is not None
    progress = [p["progress"] for kind, p in events if kind == "printer_telemetry" and p.get("progress") is not None]
    assert progress and max(progress) == 100
    remaining = [p["remaining_min"] for kind, p in events if kind == "printer_telemetry" and p.get("remaining_min")]
    assert remaining and remaining[0] > remaining[-1]


@pytest.mark.parametrize("fixture,target", [
    ("bambu-a1-happy-path", "RUNNING"), ("bambu-h2d-failure-arc", "PAUSE"),
    ("bambu-h2d-failure-arc", "FAILED"), ("bambu-x1c-ams-swap", "RUNNING"),
])
def test_captured_state_frames_persist_gcode_state(wire_monitor, review_db, fixture, target):
    monitor, sock, clock, events = wire_monitor
    frames = frames_until(fixture, lambda p: p.get("gcode_state") == target)
    replay(sock, clock, frames)
    clock[0] += 11
    sock.deliver({"print": {"command": "push_status", "sequence_id": "cert"}})
    assert printer_row(review_db, "gcode_state")["gcode_state"] == target


def test_h2d_hms_codes_logged_once_per_change_with_translations_and_alerts(wire_monitor, review_db):
    monitor, sock, clock, events = wire_monitor
    frames = load_frames("bambu-h2d-failure-arc")
    replay(sock, clock, frames)
    review_db.expire_all()
    history = review_db.execute(text("SELECT code,message,severity,source FROM hms_error_history WHERE printer_id=301 ORDER BY id")).all()
    changes = []
    for _, payload in frames:
        hms = payload.get("print", {}).get("hms")
        if hms and (not changes or changes[-1] != hms):
            changes.append(hms)
    # Logged on each change of the reported HMS set (A, B, A here), never per frame.
    assert len(changes) == 3 and len(frames) > 100
    assert len(history) == sum(len(v) for v in changes)
    assert {r[3] for r in history} == {"bambu_hms"}
    assert all(r[1] and not r[1].startswith("Unknown") for r in history[:3])
    alerts = review_db.execute(text("SELECT alert_type,severity,title FROM alerts WHERE printer_id=301")).all()
    assert alerts, "HMS errors must dispatch alerts"


def test_x1c_ipcam_report_registers_rtsps_camera_stream(wire_monitor, review_db, monkeypatch):
    import core.crypto as crypto
    monkeypatch.setenv("ODIN_HOST_IP", "192.0.2.250")
    review_db.execute(text("UPDATE printers SET model='X1C',camera_enabled=1,api_key=:k WHERE id=301"),
                      {"k": crypto.encrypt(f"{SERIAL}|{ACCESS}")})
    review_db.commit()
    monitor, sock, clock, events = wire_monitor
    replay(sock, clock, load_frames("bambu-x1c-ams-swap"))
    assert monitor._camera_discovered is True
    assert len(monitor.go2rtc_configs) == 1
    assert monitor.go2rtc_configs[0]["streams"] == {
        "printer_301": f"rtsps://bblp:{ACCESS}@192.0.2.31:322/streaming/live/1"}


def x1c_reported_trays(frames):
    ams = merged(frames)["ams"]["ams"]
    return ams, [(int(u["id"]) * 4 + int(t["id"]) + 1, t.get("tray_type"), (t.get("tray_color") or "")[:6], t.get("tag_uid"))
                 for u in ams for t in u.get("tray", []) if t.get("tray_type")]


def test_x1c_ams_frames_capture_environment(wire_monitor, review_db):
    monitor, sock, clock, events = wire_monitor
    frames = load_frames("bambu-x1c-ams-swap")
    replay(sock, clock, frames)
    ams, _ = x1c_reported_trays(frames)
    review_db.expire_all()
    env = review_db.execute(text("SELECT ams_unit,humidity,temperature FROM ams_telemetry WHERE printer_id=301 ORDER BY ams_unit")).all()
    assert [(e[0], e[1], e[2]) for e in env] == [
        (i, int(u["humidity"]), pytest.approx(float(u["temp"]))) for i, u in enumerate(ams)]


def test_real_x1c_report_mixes_rfid_and_non_rfid_spools():
    _, trays = x1c_reported_trays(load_frames("bambu-x1c-ams-swap"))
    tags = [t[3] for t in trays]
    assert tags.count("0000000000000000") == 3 and len(set(tags)) == 6


def assert_slots_match(review_db, trays):
    review_db.expire_all()
    slots = {s[0]: s for s in review_db.execute(text(
        "SELECT slot_number,filament_type,color_hex FROM filament_slots WHERE printer_id=301")).all()}
    for slot_number, material, color, _ in trays:
        assert slot_number in slots
        # Bambu tray codes map onto ODIN filament types (e.g. PLA-S -> PLA_SUPPORT).
        from modules.printers.bambu_integration import map_bambu_filament_type
        expected = map_bambu_filament_type(material)
        assert str(slots[slot_number][1]).split(".")[-1].upper() == str(getattr(expected, "name", expected)).split(".")[-1].upper()
        assert (slots[slot_number][2] or "").upper().lstrip("#")[:6] == color.upper()


def test_x1c_ams_frames_auto_sync_all_reported_slots(wire_monitor, review_db):
    monitor, sock, clock, events = wire_monitor
    frames = load_frames("bambu-x1c-ams-swap")
    replay(sock, clock, frames)
    assert_slots_match(review_db, x1c_reported_trays(frames)[1])


def a1_trays(frames):
    ams = [p["print"]["ams"]["ams"] for _, p in frames
           if isinstance(p.get("print", {}).get("ams"), dict) and isinstance(p["print"]["ams"].get("ams"), list)][0]
    return [(int(u["id"]) * 4 + int(t["id"]) + 1, t.get("tray_type"), (t.get("tray_color") or "")[:6], t.get("tag_uid"))
            for u in ams for t in u.get("tray", []) if t.get("tray_type")]


def test_a1_rfid_ams_report_on_heartbeat_auto_syncs_slots(wire_monitor, review_db):
    """A1 + AMS Lite RFID report (one non-RFID spool) syncs material/colour when it lands on a heartbeat."""
    monitor, sock, clock, events = wire_monitor
    frames = frames_until("bambu-a1-happy-path", lambda p: isinstance(p.get("ams"), dict) and p["ams"].get("ams"))
    replay(sock, clock, frames[:-1])
    clock[0] = frames[-1][0] + 11  # first AMS list arrives after the 10s heartbeat throttle
    sock.deliver(frames[-1][1])
    trays = a1_trays(frames)
    assert len(trays) == 3 and sum(1 for t in trays if t[3] != "0000000000000000") == 2
    assert_slots_match(review_db, trays)


def test_a1_capture_auto_syncs_slots_in_real_timing(wire_monitor, review_db):
    monitor, sock, clock, events = wire_monitor
    frames = load_frames("bambu-a1-happy-path")[:200]
    replay(sock, clock, frames)
    assert_slots_match(review_db, a1_trays(frames))


def test_light_report_frames_reconcile_lights_state(wire_monitor, review_db):
    monitor, sock, clock, events = wire_monitor
    replay(sock, clock, load_frames("bambu-x1c-ams-swap")[:5])
    assert printer_row(review_db, "lights_on")["lights_on"] in (1, True)


# --------------------------------------------------------------------------
# H2D: real captured H2D frames vs. the advertised H2D features
# --------------------------------------------------------------------------

def h2d_frames():
    return load_frames("bambu-h2d-failure")


def test_h2d_capture_auto_detects_machine_type(wire_monitor, review_db):
    monitor, sock, clock, events = wire_monitor
    replay(sock, clock, load_frames("bambu-h2d-failure-arc")[:200])
    assert printer_row(review_db, "machine_type")["machine_type"] == "H2D"


def test_h2d_capture_populates_second_nozzle_temperature(wire_monitor, review_db):
    review_db.execute(text("UPDATE printers SET machine_type='H2D' WHERE id=301"))
    review_db.commit()
    monitor, sock, clock, events = wire_monitor
    replay(sock, clock, h2d_frames())
    row = printer_row(review_db, "nozzle_temp,nozzle_1_temp")
    assert row["nozzle_temp"] is not None
    assert row["nozzle_1_temp"] is not None


def test_h2d_capture_carries_dual_extruder_temperatures_in_device_block():
    """Protocol evidence for the defect above: both extruders are in device.extruder.info."""
    frames = [p["print"] for _, p in h2d_frames() if "device" in p.get("print", {})
              and "extruder" in p["print"]["device"]]
    assert all(len(p["device"]["extruder"]["info"]) == 2 for p in frames)
    decoded = {(e["id"], e["temp"] & 0xFFFF, e["temp"] >> 16) for p in frames for e in p["device"]["extruder"]["info"]}
    assert (1, 140, 140) in decoded and any(i == 0 and cur > 30 for i, cur, _ in decoded)
    assert not any("nozzle_temper_1" in p for p in frames)


def h2d_frame_with_external_spools():
    """Real H2D push_status frame with spools loaded in its two vir_slot entries."""
    frame = json.loads(json.dumps([p for _, p in h2d_frames() if "vir_slot" in p.get("print", {})][-1]))
    left, right = frame["print"]["vir_slot"]
    left.update(tray_type="PLA", tray_color="FF0000FF", remain=80)
    right.update(tray_type="PETG", tray_color="00FF00FF", remain=35)
    return frame


def test_h2d_vir_slot_external_spools_reach_websocket(wire_monitor, review_db):
    review_db.execute(text("UPDATE printers SET machine_type='H2D' WHERE id=301"))
    review_db.commit()
    monitor, sock, clock, events = wire_monitor
    clock[0] = 2_000_000
    sock.deliver(h2d_frame_with_external_spools())
    pushed = [p for kind, p in events if kind == "printer_telemetry"]
    assert pushed[-1]["external_spools"] == {
        "left": {"material": "PLA", "color": "#FF0000FF", "remain_percent": 80},
        "right": {"material": "PETG", "color": "#00FF00FF", "remain_percent": 35},
    }


def test_real_h2d_frames_use_vir_slot_not_vt_tray():
    prints = [p["print"] for _, p in h2d_frames() if "print" in p]
    assert any("vir_slot" in p for p in prints)
    assert not any("vt_tray" in p or "vt_tray" in (p.get("ams") or {}) for p in prints)
    assert [s["id"] for s in [p for p in prints if "vir_slot" in p][-1]["vir_slot"]] == ["254", "255"]


# --------------------------------------------------------------------------
# Model code tables must agree between connect-time and monitor auto-detect
# --------------------------------------------------------------------------

def test_model_tables_agree():
    from modules.printers.printer_models import normalize_model_name
    from modules.models_library.threemf_parser import _friendly_printer_name
    assert all(normalize_model_name("bambu", c) == _friendly_printer_name(c)
               for c in ["BL-P001", "BL-P002", "BL-P003", "C11", "C12", "N2S", "N1", "BL-H001"])


def test_h2d_with_ams_ht_auto_syncs_ams_trays(wire_monitor, review_db):
    review_db.execute(text("UPDATE printers SET machine_type='H2D' WHERE id=301"))
    review_db.commit()
    monitor, sock, clock, events = wire_monitor
    frames = frames_until("bambu-h2d-failure-arc", lambda p: isinstance(p.get("ams"), dict) and p["ams"].get("ams"))
    replay(sock, clock, frames[:-1])
    clock[0] = frames[-1][0] + 11  # AMS list lands on a heartbeat: only the unit id matters here
    sock.deliver(frames[-1][1])
    review_db.expire_all()
    synced = review_db.execute(text(
        "SELECT COUNT(*) FROM filament_slots WHERE printer_id=301 AND filament_type IS NOT NULL AND loaded_at IS NOT NULL")).scalar_one()
    assert synced >= 8


def test_real_h2d_report_includes_ams_ht_unit_128():
    units = [p["print"]["ams"]["ams"] for _, p in load_frames("bambu-h2d-failure-arc")
             if isinstance(p.get("print", {}).get("ams"), dict) and p["print"]["ams"].get("ams")][0]
    assert [u["id"] for u in units] == ["0", "1", "128"]
