"""Connected classroom services -> real FTPS/TLS MQTT -> persisted lifecycle.

Local peers simulate printer firmware. No hardware or deployed API is used.
"""
import json
import secrets
import shutil
import socket
import ssl
import subprocess
import threading
import time

import paho.mqtt.client as mqtt
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import text

from tests.test_contracts.test_bambu_ftps_transfer import ProtectedFTPServer, server_context, resumption_context
from tests.test_contracts.test_education_review_workflow import review_db
from tests.test_contracts.test_education_upload_scheduler_integration import _upload_and_approve, _schedule


SYNTHETIC_ACCESS_CODE = secrets.token_urlsafe(24)


@pytest.fixture
def tls_mqtt(tmp_path):
    binary = shutil.which("mosquitto")
    assert binary, "Connected TLS MQTT checks require the existing mosquitto runtime"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                    "-keyout", str(tmp_path / "key.pem"), "-out", str(tmp_path / "cert.pem"),
                    "-days", "1", "-subj", "/CN=localhost"], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        port = reserved.getsockname()[1]
    password_binary = shutil.which("mosquitto_passwd")
    assert password_binary, "Existing mosquitto authentication utility is required"
    subprocess.run([password_binary, "-b", "-c", str(tmp_path / "passwords"), "bblp", SYNTHETIC_ACCESS_CODE], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    config = tmp_path / "mosquitto.conf"
    config.write_text(f"listener {port} 127.0.0.1\nallow_anonymous false\n"
                      f"password_file {tmp_path / 'passwords'}\ncertfile {tmp_path / 'cert.pem'}\nkeyfile {tmp_path / 'key.pem'}\n")
    ready = threading.Event()
    commands = []
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.username_pw_set("bblp", SYNTHETIC_ACCESS_CODE)
    client.tls_set(cert_reqs=ssl.CERT_NONE)
    client.tls_insecure_set(True)
    client.on_connect = lambda c, *args: c.subscribe("device/synthetic-printer/request")
    client.on_subscribe = lambda *args: ready.set()
    client.on_message = lambda c, u, m: commands.append(json.loads(m.payload))
    with (tmp_path / "broker.log").open("w") as log:
        process = subprocess.Popen([binary, "-c", str(config)], stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 5
            while True:
                assert process.poll() is None, "Local TLS MQTT broker failed to start"
                try:
                    client.connect("127.0.0.1", port, keepalive=5)
                    break
                except OSError:
                    assert time.monotonic() < deadline
                    time.sleep(0.05)
            client.loop_start()
            assert ready.wait(3), "Synthetic printer subscription not ready"
            yield port, commands
        finally:
            client.disconnect()
            client.loop_stop()
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)


def setup_workflow(db, tmp_path, monkeypatch, path, mqtt_port):
    from core import crypto, db as core_db, ws_hub
    from modules.printers.adapters.bambu import BambuPrinter
    from modules.printers.telemetry import feature_flag
    from modules.printers.telemetry.bambu import broker_policy

    monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
    db.execute(text("UPDATE printers SET api_host='127.0.0.1',api_key=:key WHERE id=9"),
               {"key": crypto.encrypt("synthetic-printer|" + SYNTHETIC_ACCESS_CODE)})
    db.commit()
    uploaded = _upload_and_approve(db, tmp_path, "P1S")
    assert _schedule(db, monkeypatch).scheduled_count == 1
    monkeypatch.setattr(core_db, "engine", db.get_bind())
    monkeypatch.setattr(ws_hub, "engine", db.get_bind())
    monkeypatch.setattr(feature_flag, "is_v2_enabled", lambda: path == "v2")
    monkeypatch.setattr(BambuPrinter, "MQTT_PORT", mqtt_port)
    # Replace only endpoint resolution, preserving TLS and the real command client.
    monkeypatch.setattr(broker_policy, "resolve_bambu_broker_config",
                        lambda *args, **kwargs: broker_policy.BrokerEndpoint("127.0.0.1", mqtt_port, True))
    return uploaded


def states(db, uploaded):
    db.expire_all()
    return tuple(db.execute(text("SELECT s.status,j.status FROM education_submissions s "
                                  "JOIN jobs j ON j.id=s.job_id WHERE s.id=:id"),
                            {"id": uploaded["id"]}).one())


@pytest.mark.parametrize("path", ["legacy", "v2"])
@pytest.mark.parametrize("failure", [None, "prot", "data_tls", "control_reset", "final_reply_reset"])
def test_classroom_connected_transport_and_lifecycle(path, failure, review_db, tmp_path,
                                                     monkeypatch, server_context, resumption_context, tls_mqtt):
    from modules.printers import dispatch
    from modules.printers.telemetry.bambu import ftp_upload
    from modules.organizations.education_policy import claim_monitor_observation, terminal_monitor_observation

    port, commands = tls_mqtt
    uploaded = setup_workflow(review_db, tmp_path, monkeypatch, path, port)
    original = dispatch._load_job(uploaded["job_id"])
    with open(original["stored_path"], "rb") as source:
        payload = source.read()
    peer = ProtectedFTPServer(resumption_context if failure is None else server_context,
                              require_session_reuse=failure is None, failure=failure, expected_credentials=("bblp", SYNTHETIC_ACCESS_CODE))
    monkeypatch.setattr(ftp_upload, "FTPS_PORT", peer.port)
    try:
        ok, reason = dispatch.dispatch_job(9, uploaded["job_id"])
    finally:
        peer.finish()
    assert not peer.errors
    if failure:
        assert not ok, reason
        assert states(review_db, uploaded) == ("scheduled", "scheduled")
        assert review_db.execute(text("SELECT count(*) FROM education_monitor_claims WHERE job_id=:id"),
                                 {"id": uploaded["job_id"]}).scalar_one() == 0
        assert not commands
        assert peer.received == (payload if failure == "final_reply_reset" else b"")
        peer = ProtectedFTPServer(resumption_context, require_session_reuse=True, expected_credentials=("bblp", SYNTHETIC_ACCESS_CODE))
        monkeypatch.setattr(ftp_upload, "FTPS_PORT", peer.port)
        try:
            ok, reason = dispatch.dispatch_job(9, uploaded["job_id"])
        finally:
            peer.finish()
    assert ok, reason
    assert peer.received == payload and peer.data_tls and peer.session_reused
    assert states(review_db, uploaded) == ("printing", "printing")
    deadline = time.monotonic() + 3
    def starts():
        return [c["print"] for c in commands if c.get("print", {}).get("command") == "project_file"]
    while not starts() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert len(starts()) == 1
    command = starts()[0]
    assert command["command"] == "project_file"
    assert command["param"] == "Metadata/plate_1.gcode"
    assert command["use_ams"] is True and command["bed_leveling"] is True
    assert command["timelapse"] is False
    assert command["url"] == "ftp:///" + peer.remote_filename
    assert peer.remote_filename.startswith("odin-")
    # A second click must not upload or send a second start command.
    def forbidden(*args, **kwargs):
        pytest.fail("Duplicate dispatch must not reach an adapter")
    monkeypatch.setattr(dispatch, "_dispatch_bambu", forbidden)
    assert not dispatch.dispatch_job(9, uploaded["job_id"])[0]
    time.sleep(0.1)
    assert len(starts()) == 1
    # Unmatched token and wrong-printer reports must not complete this classroom job.
    for observed_printer, filename in [(9, "odin-" + "0" * 32 + ".3mf"), (10, peer.remote_filename)]:
        foreign = review_db.execute(text("INSERT INTO print_jobs (printer_id,filename,job_name,status,started_at) "
                                         "VALUES (:printer,:filename,:filename,'running',CURRENT_TIMESTAMP) RETURNING id"),
                                    {"printer": observed_printer, "filename": filename}).scalar_one()
        denied = claim_monitor_observation(review_db, print_job_id=foreign, printer_id=observed_printer,
                                           observed_filename=filename)
        assert not denied["authorized"]
        review_db.commit()
        assert states(review_db, uploaded) == ("printing", "printing")
    observation = review_db.execute(text("INSERT INTO print_jobs (printer_id,filename,job_name,status,started_at) "
                                         "VALUES (9,:filename,:filename,'running',CURRENT_TIMESTAMP) RETURNING id"),
                                    {"filename": peer.remote_filename}).scalar_one()
    claim = claim_monitor_observation(review_db, print_job_id=observation, printer_id=9,
                                      observed_filename=peer.remote_filename)
    assert claim["authorized"]
    review_db.commit()
    result = terminal_monitor_observation(review_db, print_job_id=observation, printer_id=9,
                                         terminal_status="completed", duration_seconds=120)
    assert result["transitioned"]
    review_db.commit()
    assert states(review_db, uploaded) == ("completed", "completed")
    repeated = terminal_monitor_observation(review_db, print_job_id=observation, printer_id=9,
                                           terminal_status="completed", duration_seconds=120)
    assert not repeated["transitioned"]


@pytest.mark.parametrize("path", ["legacy", "v2"])
@pytest.mark.parametrize("obstacle", ["wrong_printer", "revoked", "material_drift", "tampered"])
def test_connected_authority_and_integrity_denials_before_transport(path, obstacle, review_db, tmp_path, monkeypatch):
    from modules.printers import dispatch
    from modules.printers.telemetry.bambu import ftp_upload

    uploaded = setup_workflow(review_db, tmp_path, monkeypatch, path, 1)
    def forbidden(*args, **kwargs):
        pytest.fail("Denied classroom dispatch must not open a transport")
    monkeypatch.setattr(ftp_upload._ImplicitFTPS, "connect", forbidden)
    printer_id = 9
    if obstacle == "wrong_printer":
        printer_id = 10
    elif obstacle == "revoked":
        review_db.execute(text("UPDATE education_cost_center_printers SET state='revoked' WHERE printer_id=9"))
        review_db.commit()
    elif obstacle == "material_drift":
        review_db.execute(text("UPDATE filament_slots SET filament_type='PETG' WHERE printer_id=9"))
        review_db.commit()
    else:
        job = dispatch._load_job(uploaded["job_id"])
        with open(job["stored_path"], "ab") as target:
            target.write(b"changed-after-approval")
    ok, reason = dispatch.dispatch_job(printer_id, uploaded["job_id"])
    assert not ok
    expected_reason = {"wrong_printer": "assigned", "revoked": "entitlement changed", "material_drift": "material_mismatch", "tampered": "contents changed"}[obstacle]
    assert expected_reason in reason
    assert review_db.execute(text("SELECT count(*) FROM education_monitor_claims WHERE job_id=:id"),
                             {"id": uploaded["job_id"]}).scalar_one() == 0
    expected = "submitted" if obstacle in {"revoked", "material_drift"} else "scheduled"
    assert states(review_db, uploaded) == (expected, expected)


@pytest.mark.parametrize("path", ["legacy", "v2"])
@pytest.mark.parametrize("terminal,expected", [("FINISH", "completed"), ("FAILED", "failed"), ("IDLE", "cancelled")])
def test_real_mqtt_monitor_ingestion_restart_and_terminal(path, terminal, expected, review_db,
                                                        tmp_path, monkeypatch, server_context, resumption_context, tls_mqtt):
    """Real broker reports traverse adapter, monitor and DB lifecycle, including reconnect."""
    from core import db_utils
    from modules.printers import dispatch
    from modules.printers.monitors.mqtt_printer import PrinterMonitor
    from modules.printers.telemetry.bambu import ftp_upload

    port, commands = tls_mqtt
    uploaded = setup_workflow(review_db, tmp_path, monkeypatch, path, port)
    monkeypatch.setattr(db_utils, "engine", review_db.get_bind())
    peer = ProtectedFTPServer(resumption_context, require_session_reuse=True, expected_credentials=("bblp", SYNTHETIC_ACCESS_CODE))
    monkeypatch.setattr(ftp_upload, "FTPS_PORT", peer.port)
    try:
        assert dispatch.dispatch_job(9, uploaded["job_id"])[0]
    finally:
        peer.finish()
    assert not peer.errors
    # Only external notifications and autonomous scheduling are forbidden here.
    # The actual transport parser, monitor callbacks and lifecycle remain intact.
    from queue import SimpleQueue
    forbidden_callbacks = SimpleQueue()
    monkeypatch.setattr(PrinterMonitor, "_dispatch_alert", lambda *a, **k: forbidden_callbacks.put("generic-alert"))
    monkeypatch.setattr(PrinterMonitor, "_trigger_reschedule", lambda *a, **k: forbidden_callbacks.put("generic-scheduling"))
    monkeypatch.setattr(PrinterMonitor, "_try_dispatch", lambda *a, **k: None)
    publisher = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    publisher.username_pw_set("bblp", SYNTHETIC_ACCESS_CODE)
    publisher.tls_set(cert_reqs=ssl.CERT_NONE)
    publisher.tls_insecure_set(True)
    connected = threading.Event()
    publisher.on_connect = lambda *a: connected.set()
    publisher.connect("127.0.0.1", port, keepalive=5)
    publisher.loop_start()
    monitor = PrinterMonitor(9, "Synthetic EDU printer", "127.0.0.1", "synthetic-printer", SYNTHETIC_ACCESS_CODE)
    def report(state, percent):
        packet = {"print": {"gcode_state": state, "subtask_name": peer.remote_filename,
                            "gcode_file": "Metadata/plate_1.gcode", "job_id": "synthetic-job",
                            "mc_percent": percent, "mc_remaining_time": 1,
                            "layer_num": 1, "total_layer_num": 2}}
        info = publisher.publish("device/synthetic-printer/report", json.dumps(packet), qos=1, retain=True)
        info.wait_for_publish(timeout=3)
        assert info.is_published()
    def wait_for(predicate):
        deadline = time.monotonic() + 5
        while not predicate():
            assert time.monotonic() < deadline, "Expected MQTT monitor transition did not arrive"
            time.sleep(0.02)
    try:
        assert connected.wait(3)
        report("RUNNING", 25)
        assert monitor.connect()
        wait_for(lambda: monitor._current_job_id is not None and monitor._last_progress_update > 0)
        observation = monitor._current_job_id
        assert monitor._linked_job_id == uploaded["job_id"]
        review_db.expire_all()
        row = review_db.execute(text("SELECT status,scheduled_job_id,progress_percent FROM print_jobs WHERE id=:id"), {"id": observation}).one()
        assert tuple(row) == ("running", uploaded["job_id"], 25)
        review_db.commit()
        # Restart the actual subscriber with a retained current-state report.
        monitor.disconnect()
        monitor = PrinterMonitor(9, "Synthetic EDU printer", "127.0.0.1", "synthetic-printer", SYNTHETIC_ACCESS_CODE)
        assert monitor.connect()
        wait_for(lambda: monitor._current_job_id == observation)
        assert monitor._linked_job_id == uploaded["job_id"]
        report("RUNNING", 25)
        report(terminal, 100)
        wait_for(lambda: states(review_db, uploaded) == (expected, expected))
        review_db.commit()
        report(terminal, 100)
        time.sleep(0.1)
        review_db.expire_all()
        assert review_db.execute(text("SELECT count(*) FROM print_jobs WHERE printer_id=9")).scalar_one() == 1
        assert review_db.execute(text("SELECT status FROM print_jobs WHERE id=:id"), {"id": observation}).scalar_one() == expected
        assert states(review_db, uploaded) == (expected, expected)
        starts = [c for c in commands if c.get("print", {}).get("command") == "project_file"]
        assert len(starts) == 1
    finally:
        monitor.disconnect()
        publisher.disconnect()
        publisher.loop_stop()
        assert forbidden_callbacks.empty(), "Education emitted forbidden generic callback"
