"""Cert-B protocol-accurate Moonraker/Klipper simulator (physical printer boundary).

Serves the subset of the Moonraker HTTP API that ODIN's MoonrakerPrinter adapter
calls (/server/info, /printer/info, /printer/objects/list, /printer/objects/query,
/server/webcams/list, /server/history/list). Response envelopes follow the
Moonraker docs: {"result": {...}}; objects/query returns
{"result": {"eventtime": t, "status": {<object>: {...}}}}.

The test controls the simulated print through POST /_sim/state (JSON merge).
Usage: python3 moonraker_sim.py <port> <webcam_stream_url>
"""
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

PORT = int(sys.argv[1])
WEBCAM = sys.argv[2]
LOCK = threading.Lock()
STATE = {
    "state": "standby",          # print_stats.state: standby|printing|paused|complete|cancelled|error
    "filename": "",
    "progress": 0.0,              # virtual_sdcard.progress 0..1
    "print_duration": 0.0,
    "filament_used": 0.0,
    "current_layer": 0,
    "total_layer": 0,
    "extruder": [24.0, 0.0],
    "bed": [23.0, 0.0],
    "fan": 0.0,
}
REQUESTS = []


def status_objects():
    s = STATE
    return {
        "heater_bed": {"temperature": s["bed"][0], "target": s["bed"][1], "power": 0.0},
        "extruder": {"temperature": s["extruder"][0], "target": s["extruder"][1], "power": 0.0},
        "print_stats": {"filename": s["filename"], "state": s["state"], "print_duration": s["print_duration"],
                         "total_duration": s["print_duration"], "filament_used": s["filament_used"],
                         "message": "", "info": {"current_layer": s["current_layer"], "total_layer": s["total_layer"]}},
        "display_status": {"progress": s["progress"], "message": None},
        "idle_timeout": {"state": "Printing" if s["state"] == "printing" else "Idle"},
        "virtual_sdcard": {"progress": s["progress"], "is_active": s["state"] == "printing", "file_position": 0},
        "gcode_move": {"speed_factor": 1.0, "extrude_factor": 1.0},
        "webhooks": {"state": "ready", "state_message": "Printer is ready"},
        "fan": {"speed": s["fan"], "rpm": None},
        "configfile": {"settings": {"extruder": {"nozzle_diameter": 0.4}}},
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, body, code=200):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        url = urlparse(self.path)
        with LOCK:
            REQUESTS.append(url.path)
            if url.path == "/server/info":
                return self._json({"result": {"klippy_connected": True, "klippy_state": "ready",
                                              "components": ["webcam", "history"], "moonraker_version": "v0.8.0-cert"}})
            if url.path == "/printer/info":
                return self._json({"result": {"state": "ready", "hostname": "certb-klipper",
                                              "software_version": "v0.12.0-cert", "device_type": "Voron 2.4 (sim)"}})
            if url.path == "/printer/objects/list":
                return self._json({"result": {"objects": ["heater_bed", "extruder", "print_stats", "display_status",
                                                          "idle_timeout", "virtual_sdcard", "gcode_move", "webhooks",
                                                          "fan", "configfile"]}})
            if url.path == "/printer/objects/query":
                wanted = [k for k in parse_qs(url.query, keep_blank_values=True)] or list(status_objects())
                objs = status_objects()
                return self._json({"result": {"eventtime": time.monotonic(),
                                              "status": {k: objs[k] for k in wanted if k in objs}}})
            if url.path == "/server/webcams/list":
                return self._json({"result": {"webcams": [{"name": "cam", "enabled": True, "service": "mjpegstreamer",
                                                           "stream_url": WEBCAM, "snapshot_url": WEBCAM}]}})
            if url.path == "/server/history/list":
                return self._json({"result": {"count": 0, "jobs": []}})
            if url.path == "/_sim/state":
                return self._json({"state": STATE, "requests": len(REQUESTS), "paths": sorted(set(REQUESTS))})
        return self._json({"error": {"code": 404, "message": "Not Found"}}, 404)

    def do_POST(self):
        url = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        if url.path == "/_sim/state":
            with LOCK:
                STATE.update(json.loads(body or b"{}"))
                return self._json({"state": STATE})
        with LOCK:
            REQUESTS.append("POST " + url.path)
        return self._json({"result": "ok"})


ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
