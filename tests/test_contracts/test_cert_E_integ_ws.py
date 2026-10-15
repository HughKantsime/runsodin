"""Cert-E integ: F14 14.3 `/ws` real-time push on the real create_app().

Runs in an isolated subprocess (the shared privacy fixture refuses an already-imported core).
Path exercised end to end, exactly as the shipped frontend hook does it:
  POST /api/auth/ws-token (Bearer session)  ->  WebSocket /ws?token=...
  captured Bambu LAN report frames -> shipped PrinterMonitor (printer socket simulated)
  -> core.ws_hub.push_event IPC table -> lifespan _ws_broadcaster -> socket frame.
The received frames plus the real GET /api/v1/printers list are written as the producer
wire fixture consumed by frontend/src/hooks/CertEIntegWebSocket.cert.test.tsx.
"""
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "frontend" / "src" / "test-fixtures" / "cert-E-integ-ws-telemetry.json"

PROBE = textwrap.dedent('''
import json, os, sys, threading
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0, os.environ["CERTE_ROOT"])
from cryptography.fernet import Fernet
os.environ["ENCRYPTION_KEY"] = Fernet.generate_key().decode()
os.environ["ODIN_TELEMETRY_V2"] = "0"
STAGE = ["start"]
def watchdog():
    sys.stderr.write("CERTE-WS-TIMEOUT at " + json.dumps(STAGE[-2:]) + "\\n"); sys.stderr.flush()
    os._exit(3)
timer = threading.Timer(90, watchdog); timer.daemon = True; timer.start()

from ops.edu_readiness.api_load import _prepare_database
app, tokens = _prepare_database(Path(os.environ["CERTE_DB"]))
from starlette.testclient import TestClient
import paho.mqtt.client as paho
from tests.test_contracts.test_cert_A_harness import FakeBrokerSocket, load_frames, SERIAL, ACCESS
paho.Client = FakeBrokerSocket
import modules.printers.route_utils as route_utils
route_utils._go2rtc_restart_with_lock = lambda config, **kw: None
from modules.printers.monitors import mqtt_printer
mqtt_printer.Thread = lambda *a, **kw: SimpleNamespace(start=lambda: None)
mqtt_printer.mqtt_republish = None
clock = [1_000_000.0]  # report timestamps drive the monitor clock, as in test_cert_A_harness
mqtt_printer.time = SimpleNamespace(time=lambda: clock[0], sleep=lambda s: None)

out = {"frames": []}
with TestClient(app) as client:
    bearer = {"Authorization": "Bearer " + tokens["viewer"][0]}
    printers = client.get("/api/v1/printers", headers=bearer)
    assert printers.status_code == 200, printers.text
    out["printers_list"] = printers.json()
    detail = client.get("/api/v1/printers/9001", headers=bearer)
    assert detail.status_code == 200, detail.text
    out["printer_detail"] = detail.json()
    issued = client.post("/api/auth/ws-token", headers=bearer)
    assert issued.status_code == 200, issued.text
    with client.websocket_connect("/ws?token=" + issued.json()["token"]) as ws:
        monitor = mqtt_printer.PrinterMonitor(9001, "Synthetic-A", "192.0.2.31", SERIAL, ACCESS)
        STAGE.append("monitor"); assert monitor.connect()
        sock = FakeBrokerSocket.instances[-1]
        state = {}
        for ts, payload in load_frames("bambu-a1-happy-path"):
            clock[0] = ts
            sock.deliver(payload)
            state.update(payload.get("print", {}))
            if state.get("gcode_state") == "RUNNING" and (state.get("mc_percent") or 0) >= 10:
                break
        out["report_state"] = {k: state.get(k) for k in ("gcode_state", "mc_percent", "nozzle_temper", "bed_temper")}
        from core.db import engine
        from sqlalchemy import text as _t
        with engine.connect() as c:
            from core.db import engine
        from sqlalchemy import text as _t
        with engine.connect() as c:
            expected = c.execute(_t("SELECT COUNT(*) FROM ws_events")).scalar_one()
        STAGE.append("replayed, expecting %d frames" % expected)
        while len(out["frames"]) < expected:
            msg = ws.receive_json()
            if msg.get("type") == "ping":
                continue
            out["frames"].append(msg)
            STAGE.append("got %d" % len(out["frames"]))
    after = client.get("/api/v1/printers/9001", headers=bearer)
    out["printer_detail_after"] = after.json()
timer.cancel()
print("CERTE-WS " + json.dumps(out))
''')


@pytest.fixture(scope="module")
def ws_capture(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("certe-ws")
    env = {**os.environ, "CERTE_ROOT": str(ROOT), "CERTE_DB": str(tmp / "odin.db"),
           "PYTHONPATH": str(ROOT / "backend") + os.pathsep + str(ROOT)}
    env.pop("API_KEY", None)
    result = subprocess.run([sys.executable, "-c", PROBE], cwd=ROOT / "backend", env=env,
                            capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-4000:]
    line = [l for l in result.stdout.splitlines() if l.startswith("CERTE-WS ")][-1]
    return json.loads(line[len("CERTE-WS "):])


def test_root_ws_endpoint_pushes_live_monitor_telemetry(ws_capture):
    telemetry = [f for f in ws_capture["frames"] if f["type"] == "printer_telemetry"]
    assert telemetry, [f["type"] for f in ws_capture["frames"]]
    last = telemetry[-1]["data"]
    after = ws_capture["printer_detail_after"]
    assert last["printer_id"] == 9001 and last["state"] == "RUNNING" == after["gcode_state"]
    assert last["nozzle_temp"] == pytest.approx(after["nozzle_temp"])
    assert last["bed_temp"] == pytest.approx(after["bed_temp"])
    assert any((f["data"].get("progress") or 0) > 0 for f in telemetry)
    assert "_audience" not in telemetry[-1]
    assert any(f["type"] == "job_started" and f["data"]["printer_id"] == 9001 for f in ws_capture["frames"])


def _stable(capture):
    keep = ("printer_id", "state", "progress", "nozzle_temp", "bed_temp", "remaining_min",
            "current_layer", "total_layers")
    frames = [{"type": f["type"], "data": {k: f["data"].get(k) for k in keep if k in f["data"]}}
              for f in capture["frames"] if f["type"] == "printer_telemetry"][-1:]
    printers = [{k: p.get(k) for k in ("id", "name", "nozzle_temp", "bed_temp", "gcode_state")}
                for p in capture["printers_list"] if p["id"] == 9001]
    detail = {k: capture["printer_detail"].get(k) for k in ("id", "name", "nozzle_temp", "bed_temp", "gcode_state")}
    return {"printers_list": printers, "printer_detail": detail, "frames": frames}


def test_ws_wire_fixture_for_frontend_consumer(ws_capture):
    fixture = _stable(ws_capture)
    assert fixture["printers_list"] and fixture["frames"]
    if FIXTURE.exists():
        assert json.loads(FIXTURE.read_text()) == fixture, "Producer contract changed; reconcile the consumer explicitly"
    else:
        FIXTURE.write_text(json.dumps(fixture, indent=2) + "\n")
