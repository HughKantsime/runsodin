"""Live enterprise compose run: go2rtc as its own service, cameras relayed end to end.

Brings up docker-compose.enterprise.yml (postgres, api, monitors, vision, reports, go2rtc)
from the image under test plus a camera simulator sidecar (the cert-B RTSP H.264 and
MJPEG simulators). The run uses the real compose file with only these changes: the ODIN
services use CERT_ENT_IMAGE instead of building, the API is published on
127.0.0.1:CERT_ENT_HTTP_PORT, the postgres init script and secrets come from the work
directory, and the camsim service is added. go2rtc's service definition is unchanged.

Proves: the API's config reaches go2rtc through the shared volume and is loaded; go2rtc's
API needs ODIN's per-boot credentials on the network and RTSP is loopback-only inside
go2rtc; no go2rtc API/RTSP port is published; an RTSP camera plays over WebRTC in Chrome
and both RTSP and MJPEG cameras stream through the overlay MJPEG route; vision captures
frames from the go2rtc service; an API restart rotates the credentials and reloads go2rtc
in place, and streams keep working.

Env: CERT_ENT_IMAGE (required), CERT_ENT_WORK (under $HOME, shared with the Docker VM),
CERT_ENT_HTTP_PORT (default 18882), CERT_ENT_ICE_HOST (default 127.0.0.1; host 8555 must
be free).
"""
import json
import os
import secrets
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SIM = ROOT / "tests" / "fixtures" / "certification" / "cert-B" / "sim"
IMAGE = os.environ.get("CERT_ENT_IMAGE", "")
HTTP_PORT = int(os.environ.get("CERT_ENT_HTTP_PORT", "18882"))
ICE_HOST = os.environ.get("CERT_ENT_ICE_HOST", "127.0.0.1")
PROJECT = f"certent{os.getpid()}"
BASE = f"http://127.0.0.1:{HTTP_PORT}"
ODIN_SERVICES = ("api", "monitors", "vision", "reports")


def _docker(*args, check=True, timeout=600, input=None):
    result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout, input=input)
    if check and result.returncode != 0:
        raise AssertionError(f"docker {' '.join(args[:4])} failed: {result.stderr[-3000:]}")
    return result


def _port_free(port):
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


pytestmark = pytest.mark.skipif(
    not IMAGE or shutil.which("docker") is None or _docker("image", "inspect", IMAGE, check=False).returncode != 0,
    reason="CERT_ENT_IMAGE (a built ODIN image) and docker are required",
)


class Stack:
    def __init__(self, work: Path):
        self.work = work
        self.file = work / "docker-compose.enterprise.cert.yml"
        self.password = "Ent1" + secrets.token_urlsafe(18)

    def compose(self, *args, check=True, timeout=600):
        return _docker("compose", "-p", PROJECT, "-f", str(self.file), *args, check=check, timeout=timeout)

    def exec(self, service, cmd, check=True, timeout=120, user=None, input=None):
        extra = ["-u", user] if user else []
        r = self.compose("exec", "-T", *extra, service, "sh", "-c", cmd, check=False, timeout=timeout) if input is None else \
            _docker("compose", "-p", PROJECT, "-f", str(self.file), "exec", "-T", *extra, service, "sh", "-c", cmd,
                    check=False, timeout=timeout, input=input)
        if check and r.returncode != 0:
            raise AssertionError(f"{service}: {cmd[:80]} -> {r.returncode}: {r.stderr[-2000:]} {r.stdout[-1000:]}")
        return r.stdout

    def py(self, service, code):
        """Python in an ODIN service with the app environment; last 'ENT=' line is JSON."""
        out = self.exec(service, "cd /app/backend && exec python3 -", input=code, user="odin")
        lines = [l for l in out.splitlines() if l.startswith("ENT=")]
        return json.loads(lines[-1][4:]) if lines else None

    def container(self, service):
        return self.compose("ps", "-q", service).stdout.strip()

    def ip(self, service):
        cid = self.container(service)
        return _docker("inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}", cid).stdout.strip()

    def request(self, method, path, token=None, body=None, form=None, timeout=60):
        headers, data = {}, None
        if body is not None:
            data, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
        elif form is not None:
            data, headers["Content-Type"] = urllib.parse.urlencode(form).encode(), "application/x-www-form-urlencoded"
        if token:
            headers["Authorization"] = "Bearer " + token
        req = urllib.request.Request(BASE + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, json.loads(r.read() or b"null")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode(errors="replace")

    def go2rtc_credentials(self):
        value = self.py("api", "import json\n"
                               "try:\n    from modules.printers.go2rtc_access import go2rtc_api_credentials as c; v=c()\n"
                               "except ImportError:\n    v=None  # images without per-boot go2rtc API credentials\n"
                               "print('ENT='+json.dumps(v))\n")
        return tuple(value) if value else None

    def go2rtc_get(self, path, auth=None, service="monitors"):
        """HTTP status of go2rtc's API as seen from another container on the network."""
        user = f"-u '{auth[0]}:{auth[1]}' " if auth else ""
        return self.exec(service, f"curl -s -o /dev/null -w '%{{http_code}}' --max-time 10 {user}'http://go2rtc:1984{path}'",
                         check=False).strip()

    def streams(self, auth):
        user = f"-u '{auth[0]}:{auth[1]}' " if auth else ""
        out = self.exec("monitors", f"curl -sf --max-time 10 {user}http://go2rtc:1984/api/streams", check=False)
        try:
            return json.loads(out)
        except ValueError:
            return {}


def _wait(predicate, seconds=180, step=2, what="condition"):
    deadline = time.monotonic() + seconds
    last = None
    while time.monotonic() < deadline:
        try:
            value = predicate()
            last = value
        except Exception as error:
            value, last = None, repr(error)
        if value:
            return value
        time.sleep(step)
    raise AssertionError(f"timed out waiting for {what}; last: {last!r}"[:2000])


def _write_compose(stack: Stack):
    from cryptography.fernet import Fernet
    compose = yaml.safe_load((ROOT / "docker-compose.enterprise.yml").read_text())
    services = compose["services"]
    original_go2rtc = json.dumps(services["go2rtc"], sort_keys=True)
    for name in ODIN_SERVICES:
        services[name].pop("build")
        services[name]["image"] = IMAGE
    services["api"]["ports"] = [f"127.0.0.1:{HTTP_PORT}:8000"]
    init = stack.work / "postgres-init"
    shutil.copytree(ROOT / "docker" / "postgres-init", init)
    services["postgres"]["volumes"] = [v if not v.startswith("./docker/postgres-init/") else
                                       f"{init}/{v.split('/')[-1].split(':')[0]}:/docker-entrypoint-initdb.d/{v.split('/')[-1].split(':')[0]}:ro"
                                       for v in services["postgres"]["volumes"]]
    sim = stack.work / "sim"
    shutil.copytree(SIM, sim)
    services["camsim"] = {
        "image": IMAGE, "entrypoint": ["/bin/sh", "-c"], "user": "odin",
        "command": ["/usr/local/bin/go2rtc -config /sim/camera_sim.yaml > /tmp/camera_sim.log 2>&1 & "
                    "(while true; do ffmpeg -hide_banner -v error -re -f lavfi -i testsrc=size=640x360:rate=15 -c:v libx264 "
                    "-preset ultrafast -tune zerolatency -g 30 -pix_fmt yuv420p -f rtsp -rtsp_transport tcp "
                    "rtsp://127.0.0.1:8556/cam; sleep 1; done) > /tmp/camera_push.log 2>&1 & "
                    "python3 /sim/mjpeg_sim.py 8080 > /tmp/mjpeg_sim.log 2>&1 & "
                    "exec python3 /sim/moonraker_sim.py 7125 \"http://$(hostname -i | cut -d' ' -f1):8080/webcam/?action=stream\""],
        "volumes": [f"{sim}:/sim:ro"],
    }
    assert json.dumps(services["go2rtc"], sort_keys=True) == original_go2rtc
    stack.file.write_text(yaml.safe_dump(compose, sort_keys=False))
    env = dict(os.environ)
    for name in ("postgres_admin_password", "postgres_app_password", "postgres_maintenance_password"):
        path = stack.work / name
        path.write_text(secrets.token_urlsafe(24))
        path.chmod(0o600)
        env[f"{name.upper()}_FILE"] = str(path)
    env.update(ENCRYPTION_KEY=Fernet.generate_key().decode(), JWT_SECRET_KEY=secrets.token_hex(32),
               ODIN_HOST_IP=ICE_HOST, TZ="Etc/UTC", CORS_ORIGINS=BASE)
    return env


@pytest.fixture(scope="module")
def stack():
    default = ROOT / "artifacts" / "application-certification" / "integration" / "enterprise-live"
    work = Path(os.environ.get("CERT_ENT_WORK") or default)
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    deadline = time.monotonic() + 60  # the VM's port forwarder releases ports after a previous stack
    while not (_port_free(HTTP_PORT) and _port_free(8555)) and time.monotonic() < deadline:
        time.sleep(2)
    if not (_port_free(HTTP_PORT) and _port_free(8555)):
        pytest.skip("enterprise live ports busy (CERT_ENT_HTTP_PORT or 8555)")
    S = Stack(work)
    env = _write_compose(S)
    os.environ.update({k: env[k] for k in env if k.endswith("_FILE") or k in ("ENCRYPTION_KEY", "JWT_SECRET_KEY", "ODIN_HOST_IP", "CORS_ORIGINS")})
    try:
        S.compose("up", "-d", timeout=900)
        _wait(lambda: S.request("GET", "/health/ready")[0] == 200, seconds=300, what="api ready")
        _wait(lambda: _docker("inspect", "-f", "{{.State.Running}}", S.container("go2rtc")).stdout.strip() == "true",
              seconds=180, what="go2rtc running (starts after the api is healthy)")
        s, b = S.request("POST", "/api/setup/admin", body={"username": "ent-admin", "email": "ent-admin@example.test",
                                                            "password": S.password})
        assert s == 200, (s, b)
        s, b = S.request("POST", "/api/auth/login", form={"username": "ent-admin", "password": S.password})
        assert s == 200, (s, b)
        S.token = b["access_token"]
        cam_ip = S.ip("camsim")
        S.ids = {}
        # RTSP H.264 camera, configured by editing the printer (API writes the go2rtc config).
        s, b = S.request("POST", "/api/printers", token=S.token, body={"name": "Ent RTSP", "model": "X1C", "slot_count": 1})
        assert s == 201, (s, b)
        S.ids["rtsp"] = b["id"]
        s, b = S.request("PATCH", f"/api/printers/{S.ids['rtsp']}", token=S.token, body={"camera_url": f"rtsp://{cam_ip}:8556/cam"})
        assert s == 200, (s, b)
        # Klipper printer whose MJPEG webcam the monitors container discovers from Moonraker
        # (the monitors write the shared config and reload go2rtc).
        s, b = S.request("POST", "/api/printers", token=S.token, body={"name": "Ent Klipper", "model": "Voron 2.4",
                                                                        "api_type": "moonraker", "api_host": f"{cam_ip}:7125",
                                                                        "slot_count": 1})
        assert s == 201, (s, b)
        S.ids["mjpeg"] = b["id"]
        S.compose("restart", "monitors", timeout=300)  # the split supervisord has no control socket
        S.auth = S.go2rtc_credentials()
        _wait(lambda: {f"printer_{S.ids['rtsp']}", f"printer_{S.ids['mjpeg']}"} <= set(S.streams(S.auth)),
              seconds=120, what="go2rtc loading the API's config")
        yield S
    finally:
        logs = S.compose("logs", "--no-color", "--tail", "400", check=False)
        (work / "compose-logs.txt").write_text(logs.stdout + logs.stderr)
        S.compose("down", "-v", "--remove-orphans", check=False, timeout=300)


def test_the_api_config_is_shared_with_go2rtc_and_loaded(stack):
    api_view = yaml.safe_load(stack.exec("api", "cat /app/go2rtc/go2rtc.yaml", user="odin"))
    go2rtc_view = yaml.safe_load(stack.exec("go2rtc", "cat /config/go2rtc.yaml"))
    assert api_view == go2rtc_view
    assert go2rtc_view["api"] == {"listen": ":1984", "username": stack.auth[0], "password": stack.auth[1]}
    assert go2rtc_view["rtsp"]["listen"] == "127.0.0.1:8554" and go2rtc_view["rtsp"]["password"]
    assert go2rtc_view["webrtc"]["candidates"] == [f"{ICE_HOST}:8555"]  # api and monitors agree
    source = go2rtc_view["streams"][f"printer_{stack.ids['mjpeg']}"]
    assert source.startswith(f"ffmpeg:http://api:8000/internal/camera-source/{stack.ids['mjpeg']}/"), source
    listening = stack.exec("go2rtc", "netstat -tln")
    assert "127.0.0.1:8554" in listening and ":::8554" not in listening and "0.0.0.0:8554" not in listening


def test_go2rtc_api_requires_the_per_boot_credentials_on_the_network(stack):
    for path in ("/api/streams", f"/api/frame.jpeg?src=printer_{stack.ids['rtsp']}",
                 f"/api/stream.mjpeg?src=printer_{stack.ids['rtsp']}_mjpeg", "/api/restart"):
        assert stack.go2rtc_get(path) == "401", path
        assert stack.go2rtc_get(path, auth=("odin", "wrong")) == "401", path
    assert stack.go2rtc_get("/api/streams", auth=stack.auth) == "200"


def test_go2rtc_rtsp_is_not_reachable_from_other_containers(stack):
    code = ("import socket,json\n"
            "s=socket.socket();s.settimeout(5)\n"
            "r=s.connect_ex(('go2rtc',8554));print('ENT='+json.dumps(r))\n")
    assert stack.py("monitors", code) != 0


def test_no_go2rtc_api_or_rtsp_port_is_published(stack):
    published = {}
    for line in stack.compose("ps", "--format", "json").stdout.splitlines():
        row = json.loads(line)
        published[row["Service"]] = sorted({(p["URL"], p["PublishedPort"], p["TargetPort"])
                                            for p in row.get("Publishers") or [] if p.get("PublishedPort")})
    targets = {target for rows in published.values() for _, _, target in rows}
    assert 1984 not in targets and 8554 not in targets, published
    assert {s for s, rows in published.items() if rows} == {"api", "go2rtc"}, published
    assert {t for _, _, t in published["go2rtc"]} == {8555}, published
    assert published["api"] == [("127.0.0.1", HTTP_PORT, 8000)], published
    assert _docker("port", stack.container("go2rtc"), "1984", check=False).stdout.strip() == ""
    assert _docker("port", stack.container("go2rtc"), "8554", check=False).stdout.strip() == ""


def _camera_token(stack):
    s, b = stack.request("POST", "/api/tokens", token=stack.token, body={"name": "Ent overlay", "scopes": ["read:cameras"]})
    assert s == 200, (s, b)
    return b["token"]


def _overlay_frames(printer_id, token, need=2, limit=4 * 1024 * 1024):
    with urllib.request.urlopen(f"{BASE}/api/overlay/{printer_id}/camera?token={token}", timeout=60) as r:
        assert r.status == 200 and r.headers["Content-Type"].startswith("multipart/x-mixed-replace"), r.headers
        data = b""
        while data.count(b"\xff\xd8") < need and len(data) < limit:
            chunk = r.read(65536)
            if not chunk:
                break
            data += chunk
    return data.count(b"\xff\xd8")


@pytest.mark.parametrize("camera", ["rtsp", "mjpeg"])
def test_overlay_mjpeg_streams_each_camera_end_to_end(stack, camera):
    """API -> go2rtc service (credentials) -> RTSP: go2rtc's loopback RTSP + ffmpeg;
    MJPEG: the API's camera relay -> simulator."""
    token = getattr(stack, "camera_token", None) or _camera_token(stack)
    stack.camera_token = token
    assert _wait(lambda: _overlay_frames(stack.ids[camera], token) >= 2, seconds=90, what=f"{camera} overlay frames")


def test_rtsp_camera_plays_over_webrtc_in_chrome(stack):
    from playwright.sync_api import sync_playwright
    script = """async ([pid, token]) => {
      const pc = new RTCPeerConnection();
      pc.addTransceiver('video', {direction: 'recvonly'});
      const video = document.createElement('video'); video.muted = true; video.autoplay = true;
      document.body.appendChild(video);
      pc.ontrack = (e) => { video.srcObject = e.streams[0] || new MediaStream([e.track]); };
      await pc.setLocalDescription(await pc.createOffer());
      await new Promise((ok) => { if (pc.iceGatheringState === 'complete') ok();
        pc.onicegatheringstatechange = () => pc.iceGatheringState === 'complete' && ok(); setTimeout(ok, 3000); });
      const r = await fetch(`/api/cameras/${pid}/webrtc`, {method: 'POST', headers: {'Content-Type': 'application/sdp',
        'Authorization': 'Bearer ' + token}, body: pc.localDescription.sdp});
      if (!r.ok) return {status: r.status, body: await r.text()};
      await pc.setRemoteDescription({type: 'answer', sdp: await r.text()});
      const start = Date.now();
      while (Date.now() - start < 45000 && !(video.videoWidth > 0 && video.currentTime > 0.5)) await new Promise(s => setTimeout(s, 250));
      return {status: r.status, w: video.videoWidth, h: video.videoHeight, t: video.currentTime, ice: pc.iceConnectionState};
    }"""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="chrome")
        try:
            page = browser.new_page()
            page.goto(BASE + "/health/ready")
            result = page.evaluate(script, [stack.ids["rtsp"], stack.token])
        finally:
            browser.close()
    (stack.work / "webrtc-result.json").write_text(json.dumps(result))
    assert result["status"] == 200 and (result["w"], result["h"]) == (640, 360) and result["t"] > 0.5, result


def test_vision_captures_frames_from_the_go2rtc_service(stack):
    code = ("import json\n"
            "from modules.vision.detection_thread import PrinterVisionThread, GO2RTC_BASE\n"
            f"w=PrinterVisionThread({stack.ids['rtsp']},'Ent RTSP',None,{{}},2,1,'RUNNING')\n"
            "f=None\n"
            "import time\n"
            "for _ in range(20):\n"
            "    f=w._capture_frame()\n"
            "    if f is not None: break\n"
            "    time.sleep(1)\n"
            "print('ENT='+json.dumps({'base':GO2RTC_BASE,'shape':None if f is None else list(f.shape)}))\n")
    result = stack.py("vision", code)
    assert result == {"base": "http://go2rtc:1984", "shape": [360, 640, 3]}, result


def test_timelapse_is_captured_in_the_vision_container(stack):
    """Review gap O12: enterprise compose ran no timelapse_capture program. It runs in the
    vision container (go2rtc address and shared secret), so a printing timelapse-enabled
    printer gets a recording with frames from the go2rtc service."""
    procs = stack.exec("vision", "for p in /proc/[0-9]*; do tr '\\0' ' ' < $p/cmdline; echo; done", check=False)
    assert "modules.archives.timelapse_capture" in procs, procs[-2000:]
    pid = stack.ids["rtsp"]
    seeded = stack.py("api", (
        "import json\n"
        "from sqlalchemy import text\n"
        "from core.db import engine\n"
        "with engine.begin() as c:\n"
        f"    c.execute(text(\"UPDATE printers SET timelapse_enabled=TRUE, gcode_state='RUNNING' WHERE id={pid}\"))\n"
        "    f=c.execute(text(\"INSERT INTO print_files(filename,original_filename,stored_path) VALUES('ent.3mf','ent.3mf','/data/uploads/ent.3mf') RETURNING id\")).scalar_one()\n"
        f"    j=c.execute(text(\"INSERT INTO jobs(item_name,printer_id,print_file_id,status,actual_start,quantity,priority,hold) VALUES('Ent timelapse',{pid},:f,'printing',CURRENT_TIMESTAMP,1,3,FALSE) RETURNING id\"),{{'f':f}}).scalar_one()\n"
        "print('ENT='+json.dumps(j))\n"))
    query = ("import json\n"
             "from sqlalchemy import text\n"
             "from core.db import engine\n"
             "with engine.connect() as c:\n"
             f"    r=c.execute(text('SELECT id,frame_count FROM timelapses WHERE printer_id={pid} AND print_job_id={seeded}')).fetchall()\n"
             "print('ENT='+json.dumps([list(x) for x in r]))\n")
    rows = _wait(lambda: [r for r in stack.py("api", query) if r[1] and r[1] > 0], seconds=180, step=5,
                 what="a timelapse recording with frames")
    assert rows and rows[0][1] > 0, rows


def test_api_restart_rotates_credentials_and_reloads_go2rtc_in_place(stack):
    go2rtc = stack.container("go2rtc")
    started = _docker("inspect", "-f", "{{.State.StartedAt}} {{.RestartCount}}", go2rtc).stdout.strip()
    old = stack.auth
    stack.compose("restart", "api", timeout=300)
    _wait(lambda: stack.request("GET", "/health/ready")[0] == 200, seconds=300, what="api ready after restart")
    new = stack.go2rtc_credentials()
    assert new != old
    _wait(lambda: stack.go2rtc_get("/api/streams", auth=new) == "200", seconds=120, what="go2rtc reloaded with new credentials")
    assert stack.go2rtc_get("/api/streams", auth=old) == "401"
    assert _docker("inspect", "-f", "{{.State.StartedAt}} {{.RestartCount}}", go2rtc).stdout.strip() == started
    stack.auth = new
    s, b = stack.request("POST", "/api/auth/login", form={"username": "ent-admin", "password": stack.password})
    assert s == 200, (s, b)
    stack.token = b["access_token"]
    # New relay tokens and RTSP credentials are live: both cameras stream again.
    for camera in ("rtsp", "mjpeg"):
        assert _wait(lambda: _overlay_frames(stack.ids[camera], stack.camera_token) >= 2, seconds=90,
                     what=f"{camera} overlay frames after api restart")
