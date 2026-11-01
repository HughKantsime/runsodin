"""Cert B live-stack harness (no tests here; imported by test_cert_B_live_stack.py).

Runs the real ODIN single-container image (supervisord: backend, go2rtc, monitors,
timelapse daemon) with the CURRENT working-tree frontend bundle mounted in, and
verifies before use that every backend .py file in the image is byte-identical to
the working tree (source fence). Only physical boundaries are simulated, inside the
container:

* cameras: a second go2rtc 1.9.4 RTSP server fed by a looping ffmpeg lavfi H.264
  test pattern (rtsp://<ip>:8556/cam), and a multipart/x-mixed-replace MJPEG
  webcam (cert-B/sim/mjpeg_sim.py, http://<ip>:8080/webcam/?action=stream, the
  mjpg-streamer/crowsnest format Klipper hosts serve);
* printer: a protocol-accurate Moonraker HTTP simulator (cert-B/sim/moonraker_sim.py);
* Bambu MQTT telemetry: ``core.ws_hub.push_event('printer_telemetry', ...)`` with
  the exact payload keys of modules/printers/monitors/mqtt_printer.py (the
  monitor->backend IPC boundary) plus the printer row fields that monitor writes.

The browser is real Chromium on Linux or Google Chrome on macOS (headless) so H.264
WebRTC decoding, <video>, fullscreen and WebGL are the shipping implementations.
Owned resources: container/volume ``certb-live-<pid>`` (CERT_B_NAME overrides), host
ports CERT_B_HTTP_PORT (default 18871) and the go2rtc WebRTC port. ODIN advertises
<ODIN_HOST_IP>:8555 as its ICE candidate (route_utils.py), so the browser must reach
the container's 8555 at <ice host>:8555. By default the container's 8555 tcp+udp is
published on 127.0.0.1:8555. When that is taken, set CERT_B_ICE_HOST to another host
address (e.g. the LAN IP) and CERT_B_WEBRTC_PORT to a free loopback port: the
container's 8555 is published on 127.0.0.1:CERT_B_WEBRTC_PORT and a harness-owned TCP
relay listens on CERT_B_ICE_HOST:8555 (ICE-TCP; also compatible with macOS Docker
VM port forwarding). Every owned resource is removed in
teardown and again at interpreter exit. Test credentials are generated at runtime and
never printed.
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# Committed inputs (simulators, browser driver, 3MF exports) vs. run outputs
# (screenshots, logs, live-state receipts), which go to the gitignored artifacts tree.
FIXTURES = ROOT / "tests" / "fixtures" / "certification" / "cert-B"
CERT = ROOT / "artifacts" / "application-certification" / "cert-B"
SIM = FIXTURES / "sim"
SCREENS = CERT / "screens"
PLAYWRIGHT = FIXTURES / "live.playwright.mjs"
IMAGE = os.environ.get("CERT_B_IMAGE", "odin-cert-b:local")
HTTP_PORT = int(os.environ.get("CERT_B_HTTP_PORT", "18871"))
ICE_HOST = os.environ.get("CERT_B_ICE_HOST", "127.0.0.1")
WEBRTC_PORT = int(os.environ.get("CERT_B_WEBRTC_PORT", "8555"))
ODIN_WEBRTC_PORT = 8555  # fixed by backend/modules/printers/route_utils.py
NAME = os.environ.get("CERT_B_NAME") or f"certb-live-{os.getpid()}"
BASE = f"http://127.0.0.1:{HTTP_PORT}"


def docker(*args, check=True, timeout=300, input=None):
    r = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout, input=input)
    if check and r.returncode != 0:
        raise AssertionError(f"docker {args[0]} failed: {r.stderr[-2000:]}")
    return r


def port_free(port, proto=socket.SOCK_STREAM, host="127.0.0.1"):
    s = socket.socket(socket.AF_INET, proto)
    try:
        s.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def ports_free():
    """Every host port this harness will bind is free."""
    ok = port_free(HTTP_PORT) and port_free(WEBRTC_PORT)
    if relay_needed():
        ok = ok and port_free(ODIN_WEBRTC_PORT, host=ICE_HOST)
    return ok


def relay_needed():
    return ICE_HOST != "127.0.0.1" or WEBRTC_PORT != ODIN_WEBRTC_PORT


class TcpRelay:
    """ICE_HOST:8555 -> 127.0.0.1:WEBRTC_PORT byte relay (one thread per direction)."""

    def __init__(self, listen_host, listen_port, target_port):
        import threading
        self.target = ("127.0.0.1", target_port)
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind((listen_host, listen_port))
        self.srv.listen(64)
        self.closed = False
        self.conns = []
        self.thread = threading.Thread(target=self._accept, daemon=True)
        self.thread.start()

    def _pipe(self, a, b):
        try:
            while True:
                d = a.recv(65536)
                if not d:
                    break
                b.sendall(d)
        except OSError:
            pass
        finally:
            for x in (a, b):
                try:
                    x.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def _accept(self):
        import threading
        while not self.closed:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            try:
                u = socket.create_connection(self.target, timeout=10)
                u.settimeout(None)
            except OSError:
                c.close()
                continue
            self.conns += [c, u]
            threading.Thread(target=self._pipe, args=(c, u), daemon=True).start()
            threading.Thread(target=self._pipe, args=(u, c), daemon=True).start()

    def close(self):
        self.closed = True
        for s in [self.srv, *self.conns]:
            try:
                s.close()
            except OSError:
                pass


class Live:
    def __init__(self, work: Path):
        self.work = work
        self.ip = None
        self.password = "CertB-" + secrets.token_hex(8) + "!aA1"
        self.tokens = {}
        self.ids = {}
        self.relay = None
        self.created = False

    # ---------------- container lifecycle ----------------
    def source_fence(self):
        files = sorted(p for p in (ROOT / "backend").rglob("*.py")
                       if "__pycache__" not in p.parts and "tests" not in p.relative_to(ROOT / "backend").parts)
        import hashlib
        lines = [f"{hashlib.sha256(p.read_bytes()).hexdigest()}  /app/{p.relative_to(ROOT)}" for p in files]
        sha = self.work / "backend.sha256"
        sha.write_text("\n".join(lines) + "\n")
        r = docker("run", "--rm", "--entrypoint", "sh", "-v", f"{sha}:/tmp/l.sha:ro", IMAGE, "-c",
                   "cd / && sha256sum -c --quiet /tmp/l.sha", check=False)
        return len(files), r.returncode, (r.stdout + r.stderr)[-1500:]

    def build_frontend(self):
        out = self.work / "dist"
        r = subprocess.run(["npx", "vite", "build", "--outDir", str(out), "--emptyOutDir"], cwd=ROOT / "frontend",
                           capture_output=True, text=True, timeout=600)
        (self.work / "vite-build.log").write_text(r.stdout[-20000:] + r.stderr[-20000:])
        assert r.returncode == 0 and (out / "index.html").exists(), r.stderr[-3000:]
        return out

    def start(self, dist: Path):
        import atexit
        atexit.register(self.stop)
        self.created = True
        docker("volume", "create", NAME)
        docker("run", "-d", "--name", NAME, "--label", "com.runsodin.cert-b=live",
               "-p", f"127.0.0.1:{HTTP_PORT}:8000",
               "-p", f"127.0.0.1:{WEBRTC_PORT}:{ODIN_WEBRTC_PORT}/tcp", "-p", f"127.0.0.1:{WEBRTC_PORT}:{ODIN_WEBRTC_PORT}/udp",
               "-e", "TZ=Etc/UTC", "-e", f"ODIN_HOST_IP={ICE_HOST}",
               "-v", f"{NAME}:/data", "-v", f"{dist}:/app/frontend/dist:ro", IMAGE)
        if relay_needed():
            self.relay = TcpRelay(ICE_HOST, ODIN_WEBRTC_PORT, WEBRTC_PORT)
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            try:
                s, b = self.json("GET", "/health/ready")
                if s == 200 and isinstance(b, dict) and b.get("ready") is True:
                    break
            except OSError:
                pass
            time.sleep(1)
        else:
            raise AssertionError("container not ready: " + docker("logs", NAME, check=False).stderr[-3000:])
        self.ip = docker("inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", NAME).stdout.strip()
        assert self.ip and not self.ip.startswith("127."), self.ip

    def stop(self):
        if not self.created:
            return
        self.created = False
        if self.relay:
            self.relay.close()
            self.relay = None
        for name in ("go2rtc.log", "backend.log", "timelapse_capture.log", "moonraker_monitor.log"):
            r = docker("exec", NAME, "sh", "-c", f"tail -c 400000 /data/{name}", check=False)
            (self.work / f"container-{name}").write_text(r.stdout)
        docker("rm", "-f", NAME, check=False)
        docker("volume", "rm", "-f", NAME, check=False)

    # ---------------- simulators ----------------
    def start_simulators(self):
        cam = self.work / "camera_sim.yaml"
        cam.write_text((SIM / "camera_sim.yaml").read_text())
        docker("cp", str(cam), f"{NAME}:/tmp/camera_sim.yaml")
        docker("cp", str(SIM / "moonraker_sim.py"), f"{NAME}:/tmp/moonraker_sim.py")
        docker("cp", str(SIM / "mjpeg_sim.py"), f"{NAME}:/tmp/mjpeg_sim.py")
        docker("exec", "-d", NAME, "sh", "-c", "exec /usr/local/bin/go2rtc -config /tmp/camera_sim.yaml > /tmp/camera_sim.log 2>&1")
        docker("exec", "-d", NAME, "sh", "-c",
               "while true; do ffmpeg -hide_banner -v error -re -f lavfi -i testsrc=size=640x360:rate=15 -c:v libx264 -preset ultrafast "
               "-tune zerolatency -g 30 -pix_fmt yuv420p -f rtsp -rtsp_transport tcp rtsp://127.0.0.1:8556/cam; sleep 1; done > /tmp/camera_push.log 2>&1")
        docker("exec", "-d", NAME, "sh", "-c", "exec python3 /tmp/mjpeg_sim.py 8080 > /tmp/mjpeg_sim.log 2>&1")
        self.mjpeg_url = f"http://{self.ip}:8080/webcam/?action=stream"
        docker("exec", "-d", NAME, "sh", "-c",
               f"exec python3 /tmp/moonraker_sim.py 7125 '{self.mjpeg_url}' > /tmp/moonraker_sim.log 2>&1")
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            r = docker("exec", NAME, "sh", "-c",
                       "timeout 20 ffprobe -v error -rtsp_transport tcp -select_streams v -show_entries stream=codec_name,width,height "
                       "-of json rtsp://127.0.0.1:8556/cam && timeout 20 ffprobe -v error -show_entries stream=codec_name -of csv "
                       f"'{self.mjpeg_url}' && curl -sf http://127.0.0.1:7125/server/info", check=False, timeout=90)
            if r.returncode == 0 and "h264" in r.stdout and "mjpeg" in r.stdout:
                return
            time.sleep(1)
        raise AssertionError("simulators did not start")

    def sim(self, **state):
        r = self.sh(f"curl -sf -X POST -H 'Content-Type: application/json' -d '{json.dumps(state)}' http://127.0.0.1:7125/_sim/state")
        return json.loads(r)

    def sim_status(self):
        return json.loads(self.sh("curl -sf http://127.0.0.1:7125/_sim/state"))

    # ---------------- in-container execution ----------------
    def sh(self, cmd, check=True, timeout=300):
        r = docker("exec", NAME, "sh", "-c", cmd, check=False, timeout=timeout)
        if check and r.returncode != 0:
            raise AssertionError(f"exec failed ({r.returncode}): {r.stderr[-2000:]}")
        return r.stdout

    def py(self, code, timeout=120):
        """Run python inside the backend environment (same env vars the entrypoint exports)."""
        wrapper = ("export JWT_SECRET_KEY=$(cat /data/.jwt_secret) ENCRYPTION_KEY=$(cat /data/.encryption_key); "
                   "cd /app/backend && exec python3 -")
        r = docker("exec", "-i", NAME, "sh", "-c", wrapper, check=False, timeout=timeout, input=code)
        assert r.returncode == 0, r.stderr[-3000:]
        lines = [l for l in r.stdout.splitlines() if l.startswith("CERTB=")]
        return json.loads(lines[-1][6:]) if lines else None

    def sql(self, stmt, params=None):
        code = ("import json,sqlite3\n"
                "c=sqlite3.connect('/data/odin.db',timeout=30)\n"
                f"cur=c.execute({stmt!r},json.loads({json.dumps(params or {})!r}))\n"
                "rows=cur.fetchall() if cur.description else []\n"
                "c.commit()\n"
                "print('CERTB='+json.dumps({'rows':rows,'lastrowid':cur.lastrowid},default=str))\n")
        out = self.py(code)
        return out

    def rows(self, stmt, params=None):
        return self.sql(stmt, params)["rows"]

    def supervisorctl(self, *args):
        return self.sh("supervisorctl -c /etc/supervisor/conf.d/odin.conf " + " ".join(args), check=False)

    def go2rtc_config(self):
        import yaml
        return yaml.safe_load(self.sh("cat /app/go2rtc/go2rtc.yaml"))

    def push_bambu_telemetry(self, printer_id, **values):
        payload = {"printer_id": printer_id, **values}
        self.py("from core.ws_hub import push_event\n"
                f"push_event('printer_telemetry', {json.dumps(payload)})\n")

    # ---------------- HTTP ----------------
    def request(self, method, path, token=None, json_body=None, form=None, raw=None, content_type=None,
                headers=None, timeout=60):
        h = dict(headers or {})
        data = None
        if json_body is not None:
            data = json.dumps(json_body).encode(); h["Content-Type"] = "application/json"
        elif form is not None:
            data = urllib.parse.urlencode(form).encode(); h["Content-Type"] = "application/x-www-form-urlencoded"
        elif raw is not None:
            data = raw; h["Content-Type"] = content_type or "application/octet-stream"
        if token:
            h["Authorization"] = "Bearer " + token
        req = urllib.request.Request(BASE + path, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read(), dict(r.headers)
        except urllib.error.HTTPError as e:
            return e.code, e.read(), dict(e.headers)

    def json(self, method, path, **kw):
        s, b, _ = self.request(method, path, **kw)
        try:
            return s, json.loads(b or b"null")
        except json.JSONDecodeError:
            return s, b.decode(errors="replace")

    def upload(self, path, filename, data, token):
        boundary = "certb" + secrets.token_hex(8)
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
                f"Content-Type: application/octet-stream\r\n\r\n").encode() + data + f"\r\n--{boundary}--\r\n".encode()
        return self.json("POST", path, token=token, raw=body, content_type=f"multipart/form-data; boundary={boundary}")

    def login(self, username):
        s, b = self.json("POST", "/api/auth/login", form={"username": username, "password": self.password})
        assert s == 200, (s, b)
        self.tokens[username] = b["access_token"]
        return b["access_token"]

    def seed_user(self, username, role):
        self.py("from core.auth import hash_password\nimport sqlite3,json\n"
                "c=sqlite3.connect('/data/odin.db',timeout=30)\n"
                "c.execute(\"INSERT INTO users(username,email,password_hash,role,is_active) VALUES(?,?,?,?,1)\","
                f"({username!r},{username + '@example.test'!r},hash_password({self.password!r}),{role!r}))\n"
                "c.commit()\nprint('CERTB='+json.dumps(True))\n")

    # ---------------- browser ----------------
    def browser(self, scenario, timeout=420, **args):
        SCREENS.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ, CERTB_BASE=BASE, CERTB_PASSWORD=self.password, CERTB_SCREENS=str(SCREENS), CERTB_BROWSER_CHANNEL=browser_channel())
        r = subprocess.run(["node", str(PLAYWRIGHT), scenario, json.dumps(args)], cwd=ROOT / "frontend",
                           capture_output=True, text=True, timeout=timeout, env=env)
        log = self.work / f"browser-{scenario}.log"
        log.write_text(r.stdout[-200000:] + "\n--- stderr ---\n" + r.stderr[-50000:])
        lines = [l for l in r.stdout.splitlines() if l.startswith("CERTB_JSON=")]
        assert r.returncode == 0 and lines, f"browser scenario {scenario} failed: {r.stderr[-3000:]} {r.stdout[-3000:]}"
        result = json.loads(lines[-1][len("CERTB_JSON="):])
        (self.work / f"browser-{scenario}.json").write_text(json.dumps(result, indent=1))
        return result


def browser_channel():
    return "chrome" if sys.platform == "darwin" else "chromium"


def chrome_available():
    if browser_channel() == "chrome":
        return Path("/Applications/Google Chrome.app").exists()
    if shutil.which("node") is None:
        return False
    result = subprocess.run(
        ["node", "-e", "const {chromium}=require('@playwright/test'); process.exit(require('fs').existsSync(chromium.executablePath()) ? 0 : 1)"],
        cwd=ROOT / "frontend", capture_output=True, text=True, timeout=30,
    )
    return result.returncode == 0
