"""Cert B live stack: camera, PiP, detail, TV, timelapse, overlay and archive UI claims.

Real ODIN image (source-fenced to the working tree) + current frontend bundle +
real Google Chrome. See test_cert_B_live_harness.py for what is simulated (only
the physical camera, the physical Klipper printer and Bambu MQTT telemetry IPC).
Tests run in file order against one module-scoped stack.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import shutil
import threading
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tests.test_contracts.test_cert_B_live_harness import CERT, FIXTURES, IMAGE, NAME, Live, chrome_available, docker, port_free, ports_free, HTTP_PORT

pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None or docker("image", "inspect", IMAGE, check=False).returncode != 0
    or not chrome_available() or shutil.which("node") is None,
    reason="docker, the ODIN image (CERT_B_IMAGE), node and Google Chrome are required",
)

RTSP = "CertB RTSP X1C"
KLIPPER = "CertB Klipper"
FLEET = [f"CertB Fleet {i:02d}" for i in range(1, 12)]


def wait_until(fn, timeout=60, interval=1.0, message="condition"):
    end = time.monotonic() + timeout
    last = None
    while time.monotonic() < end:
        last = fn()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f"timed out waiting for {message}: {last!r}")


class BambuPrinting:
    """Emulates a Bambu monitor for one printer: the printer-row fields mqtt_printer
    writes (gcode_state/temps/fan/last_seen) and its 'printer_telemetry' WS payload."""

    def __init__(self, live, pid, **telemetry):
        self.live, self.pid, self.t = live, pid, telemetry
        self.stop_event = threading.Event()
        self.state = "RUNNING"
        self.thread = threading.Thread(target=self.run, daemon=True)

    def tick(self):
        self.live.sql("UPDATE printers SET last_seen=datetime('now'),gcode_state=:s,print_stage=:stage,nozzle_temp=219.6,"
                      "nozzle_target_temp=220,bed_temp=59.8,bed_target_temp=60,fan_speed=80 WHERE id=:id",
                      {"s": self.state, "stage": "Printing" if self.state == "RUNNING" else "Idle", "id": self.pid})
        if self.state == "RUNNING":
            self.live.push_bambu_telemetry(self.pid, bed_temp=59.8, bed_target=60, nozzle_temp=219.6, nozzle_target=220,
                                           state="RUNNING", **self.t)

    def run(self):
        while not self.stop_event.is_set():
            try:
                self.tick()
            except Exception:
                pass
            self.stop_event.wait(2.0)

    def start(self):
        self.tick(); self.thread.start(); return self

    def finish(self):
        self.state = "FINISH"; self.tick()

    def stop(self):
        self.stop_event.set(); self.thread.join(timeout=10)


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    # Bind-mounted into the container, so it must be on a path the Docker VM shares
    # (colima/Docker Desktop share $HOME by default); CERT_B_WORK overrides.
    work = Path(os.environ["CERT_B_WORK"]) if os.environ.get("CERT_B_WORK") else CERT / "work"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)  # creates the gitignored output tree on a clean checkout
    if not ports_free():
        pytest.skip("cert-B ports busy")
    L = Live(work)
    checked, code, detail = L.source_fence()
    assert code == 0 and checked > 500, f"image backend differs from working tree: {detail}"
    L.fence = {"backend_py_files": checked, "image": IMAGE}
    dist = L.build_frontend()
    L.start(dist)
    try:
        L.start_simulators()
        s, b = L.json("POST", "/api/setup/admin", json_body={"username": "certb-admin", "email": "certb-admin@example.test", "password": L.password})
        assert s == 200, b
        admin = L.login("certb-admin")
        assert L.json("POST", "/api/setup/complete", token=admin)[0] == 200
        for user, role in (("certb-operator", "operator"), ("certb-viewer", "viewer")):
            L.seed_user(user, role)
            L.login(user)
        # Printer with an RTSP H.264 camera (camera configured by editing the printer, as the UI does).
        s, b = L.json("POST", "/api/printers", token=admin, json_body={"name": RTSP, "model": "X1C", "slot_count": 4})
        assert s == 201, b
        L.ids["rtsp"] = b["id"]
        s, b = L.json("PATCH", f"/api/printers/{L.ids['rtsp']}", token=admin, json_body={"camera_url": f"rtsp://{L.ip}:8556/cam"})
        assert s == 200, b
        # Klipper printer whose webcam is discovered from the (simulated) Moonraker config.
        s, b = L.json("POST", "/api/printers", token=admin, json_body={"name": KLIPPER, "model": "Voron 2.4", "api_type": "moonraker",
                                                                         "api_host": f"{L.ip}:7125", "slot_count": 1})
        assert s == 201, b
        L.ids["klipper"] = b["id"]
        L.supervisorctl("restart", "moonraker_monitor")
        wait_until(lambda: (L.go2rtc_config().get("streams") or {}).get(f"printer_{L.ids['klipper']}"), 90, 2, "klipper camera discovery")
        # Farm of 11 more printers (13 total) for TV pagination; rows copied from an API-created printer.
        cols = [c[1] for c in L.rows("PRAGMA table_info(printers)") if c[1] not in ("id", "name", "camera_url", "api_type", "api_host", "api_key")]
        for name in FLEET:
            L.sql(f"INSERT INTO printers(name,{','.join(cols)}) SELECT :n,{','.join(cols)} FROM printers WHERE id=:src", {"n": name, "src": L.ids["rtsp"]})
        L.sql("UPDATE printers SET last_seen=datetime('now'),gcode_state='IDLE',camera_enabled=0,model='P1S' WHERE name LIKE 'CertB Fleet%'")
        # Filament slot 1 of the RTSP printer is loaded with a tracked spool that has 640 g remaining.
        fid = L.sql("INSERT INTO filament_library(brand,name,material,color_hex) VALUES('CertB','Galaxy Black','PLA','111111')")["lastrowid"]
        spool = L.sql("INSERT INTO spools(filament_id,remaining_weight_g,initial_weight_g,vendor) VALUES(:f,640,1000,'CertB')", {"f": fid})["lastrowid"]
        L.sql("UPDATE filament_slots SET assigned_spool_id=:s,color='Black',color_hex='111111',filament_type='PLA' WHERE printer_id=:p AND slot_number=1",
              {"s": spool, "p": L.ids["rtsp"]})
        # A job is printing on the RTSP printer with timelapse enabled (frames accumulate during the UI tests).
        assert L.json("PATCH", f"/api/printers/{L.ids['rtsp']}", token=admin, json_body={"timelapse_enabled": True})[0] == 200
        L.ids["tl_file"] = L.sql("INSERT INTO print_files(filename,original_filename,stored_path) VALUES('certb_benchy.3mf','certb_benchy.3mf','/data/uploads/certb_benchy.3mf')")["lastrowid"]
        L.ids["tl_job"] = L.sql("INSERT INTO jobs(item_name,printer_id,print_file_id,status,actual_start,quantity,priority,hold) VALUES('CertB Benchy',:p,:f,'printing',datetime('now'),1,3,0)",
                                {"p": L.ids["rtsp"], "f": L.ids["tl_file"]})["lastrowid"]
        L.ids["tl_print_job"] = L.sql("INSERT INTO print_jobs(printer_id,job_id,filename,job_name,status,started_at,progress_percent,current_layer,total_layers,remaining_minutes,scheduled_job_id) "
                                      "VALUES(:p,'certb-1','certb_benchy.3mf','CertB Benchy','running',datetime('now'),42,84,200,37,:j)",
                                      {"p": L.ids["rtsp"], "j": L.ids["tl_job"]})["lastrowid"]
        L.bambu = BambuPrinting(L, L.ids["rtsp"], progress=42, remaining_min=37, current_layer=84, total_layers=200,
                                gcode_file="CertB Benchy", fan_speed=80).start()
        L.capture_started = time.time()
        yield L
    finally:
        if getattr(L, "bambu", None):
            L.bambu.stop()
        (CERT / "live-state.json").write_text(json.dumps({"ids": L.ids, "fence": getattr(L, "fence", None), "ip": L.ip}, indent=1))
        if not os.environ.get("CERT_B_KEEP"):
            L.stop()


def admin(L):
    return L.tokens["certb-admin"]


from contextlib import contextmanager


@contextmanager
def spare_printer_slot(L):
    """The Community license allows 5 printers and the farm holds 13 (SQL-seeded for TV
    pagination); park the last fleet row while a test creates a printer through the API."""
    cols = [c[1] for c in L.rows("PRAGMA table_info(printers)")]
    row = L.rows(f"SELECT {','.join(cols)} FROM printers WHERE name=:n", {"n": FLEET[-1]})[0]
    keep = L.rows("SELECT COUNT(*) FROM printers")[0][0] - 4  # leave room for exactly one more
    parked = L.rows(f"SELECT {','.join(cols)} FROM printers WHERE name LIKE 'CertB Fleet%' ORDER BY id DESC LIMIT :k", {"k": keep})
    L.sql("DELETE FROM printers WHERE id IN (SELECT id FROM printers WHERE name LIKE 'CertB Fleet%' ORDER BY id DESC LIMIT :k)", {"k": keep})
    try:
        yield
    finally:
        for values in parked:
            L.sql(f"INSERT INTO printers({','.join(cols)}) VALUES({','.join(':' + c for c in cols)})", dict(zip(cols, values)))
        assert row in L.rows(f"SELECT {','.join(cols)} FROM printers WHERE name LIKE 'CertB Fleet%'")


def camera_token(L, user="certb-operator"):
    """A per-user read:cameras API token, as the copy-overlay-URL button mints."""
    s, b = L.json("POST", "/api/tokens", token=L.tokens[user], json_body={"name": "CertB OBS overlay", "scopes": ["read:cameras"]})
    assert s == 200, (s, b)
    return b["token"]


def stream_head(path):
    """Status and content type of an endless stream, without reading its body."""
    import urllib.error
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{HTTP_PORT}{path}", timeout=60) as r:
            first = r.read(64)
            return r.status, r.headers.get("content-type", ""), first
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("content-type", ""), b""


# --------------------------------------------------------------------------- F02-2.1 / 2.2 cameras

def test_source_fence_image_matches_working_tree(live):
    assert live.fence["backend_py_files"] > 500


def test_F02_2_1_01_rtsp_camera_is_relayed_by_go2rtc_to_webrtc_in_chrome(live):
    streams = live.go2rtc_config()["streams"]
    # RTSP sources are handed to go2rtc unchanged (relay/remux; no ffmpeg transcode stage).
    assert streams[f"printer_{live.ids['rtsp']}"] == f"rtsp://{live.ip}:8556/cam"
    r = live.browser("camera_detail", user="certb-viewer", id=live.ids["rtsp"], name=RTSP, tag="webrtc")
    assert r["live"]["live"] and (r["live"]["w"], r["live"]["h"]) == (640, 360), r["live"]
    signaling = [q for q in r["requests"] if q["method"] == "POST" and q["url"] == f"/api/cameras/{live.ids['rtsp']}/webrtc"]
    assert signaling, r["requests"]
    log = live.sh("cat /data/backend.log")
    assert re.search(rf'"POST /api/cameras/{live.ids["rtsp"]}/webrtc HTTP/1.1" 200', log)


def test_F02_2_1_01_webrtc_signaling_rejects_unauthenticated_and_disabled(live):
    s, _, _ = live.request("POST", f"/api/cameras/{live.ids['rtsp']}/webrtc", raw=b"v=0", content_type="application/sdp")
    assert s == 401


def test_F02_2_2_02_moonraker_webcam_discovered_from_moonraker_config(live):
    kid = live.ids["klipper"]
    stored = live.rows("SELECT camera_url FROM printers WHERE id=:id", {"id": kid})[0][0]
    assert stored and "8080" not in stored  # persisted encrypted, not plaintext
    assert "/server/webcams/list" in live.sim_status()["paths"]
    assert live.go2rtc_config()["streams"][f"printer_{kid}"] == f"ffmpeg:{live.mjpeg_url}#video=h264"


def test_F02_2_2_05_mjpeg_camera_is_transcoded_to_h264_and_plays_over_webrtc(live):
    r = live.browser("camera_detail", user="certb-viewer", id=live.ids["klipper"], name=KLIPPER, tag="mjpeg")
    assert r["live"]["live"] and (r["live"]["w"], r["live"]["h"]) == (640, 360), r["live"]
    probe = live.sh("curl -s http://127.0.0.1:1984/api/streams")
    stream = json.loads(probe)[f"printer_{live.ids['klipper']}"]
    codecs = [rcv.get("codec", {}).get("codec_name") for p in stream.get("producers", []) for rcv in p.get("receivers", []) or []]
    assert "h264" in codecs, stream


def test_F02_2_1_02_camera_grid_shows_every_active_feed_live(live):
    r = live.browser("camera_grid", user="certb-viewer", names=[RTSP, KLIPPER])
    assert r["videoCount"] == 2, r
    for name in (RTSP, KLIPPER):
        card = r["cards"][name]
        assert card["live"] and card["w"] == 640 and card["liveLabel"] and card["statusText"] == "live", (name, card)
    assert r["subtitle"].strip() == "2 cameras available"
    api = live.json("GET", "/api/cameras", token=admin(live))[1]
    assert sorted(c["name"] for c in api) == sorted([RTSP, KLIPPER])  # fleet printers have no camera -> not listed


def test_F02_2_1_03_per_camera_toggle_disables_and_reenables_feed(live):
    kid = live.ids["klipper"]
    off = live.browser("camera_toggle", user="certb-operator", name=KLIPPER)
    assert (off["before"], off["after"]) == ("Enabled", "Disabled"), off
    assert off["gridHasAfterToggle"] == 0 and off["gridHasAfterReload"] == 0 and RTSP in off["gridNames"], off
    assert live.rows("SELECT camera_enabled FROM printers WHERE id=:id", {"id": kid})[0][0] == 0
    assert all(c["id"] != kid for c in live.json("GET", "/api/cameras", token=admin(live))[1])
    wait_until(lambda: f"printer_{kid}" not in (live.go2rtc_config().get("streams") or {}), 30, 1, "go2rtc stream removal")
    assert live.request("POST", f"/api/cameras/{kid}/webrtc", token=admin(live), raw=b"v=0", content_type="application/sdp")[0] == 404
    on = live.browser("camera_toggle", user="certb-operator", name=KLIPPER)
    live.toggle_on = on
    assert on["gridHasAfterReload"] == 1, on
    assert live.rows("SELECT camera_enabled FROM printers WHERE id=:id", {"id": kid})[0][0] == 1
    wait_until(lambda: f"printer_{kid}" in (live.go2rtc_config().get("streams") or {}), 30, 1, "go2rtc stream restore")
    viewer = live.request("PATCH", f"/api/cameras/{kid}/toggle", token=live.tokens["certb-viewer"])[0]
    assert viewer == 403


def test_F02_2_1_03_settings_panel_shows_persisted_disabled_state(live):
    assert live.toggle_on["before"] == "Disabled", live.toggle_on


def test_F02_2_1_03_auto_discovered_bambu_camera_has_a_toggle(live):
    with spare_printer_slot(live):
        _auto_bambu_toggle(live)


def _auto_bambu_toggle(live):
    s, b = live.json("POST", "/api/printers", token=admin(live), json_body={"name": "CertB Bambu H2D", "model": "H2D", "api_type": "bambu",
                                                                             "api_host": live.ip, "api_key": "01P00CERTB0001|certb123", "slot_count": 4})
    assert s == 201, b
    bid = b["id"]
    try:
        assert any(c["id"] == bid for c in live.json("GET", "/api/cameras", token=admin(live))[1])
        panel = live.browser("camera_toggle", user="certb-operator")
        assert "CertB Bambu H2D" in panel["settingsNames"], panel
    finally:
        live.json("DELETE", f"/api/printers/{bid}", token=admin(live))


def test_F02_2_1_01_create_printer_with_camera_url(live):
    with spare_printer_slot(live):
        _create_with_camera(live)


def _create_with_camera(live):
    s, b = live.json("POST", "/api/printers", token=admin(live), json_body={"name": "CertB Create With Camera", "model": "X1C", "slot_count": 1,
                                                                             "camera_url": f"rtsp://{live.ip}:8556/cam"})
    try:
        assert s == 201, (s, b)
    finally:
        if s == 201:
            live.json("DELETE", f"/api/printers/{b['id']}", token=admin(live))


# --------------------------------------------------------------------------- F02-2.3 control room

def test_F02_2_3_01_02_control_room_full_screen_grid_and_escape(live):
    r = live.browser("control_room", user="certb-viewer", names=[RTSP, KLIPPER])
    assert r["overlayBox"] == {"x": 0, "y": 0, "w": r["viewport"]["width"], "h": r["viewport"]["height"]}
    assert r["rootHasControlRoomClass"] and r["topElementAtSidebarIsOverlay"]
    assert sorted(r["overlayNames"]) == sorted([RTSP, KLIPPER]) and r["overlayVideoCount"] == 2
    assert all(v["live"] for v in r["overlayVideos"]), r["overlayVideos"]
    assert r["overlayAfterEscape"] == 0 and r["rootClassAfterEscape"] is False and r["gridVideosAfterEscape"] == 2


# --------------------------------------------------------------------------- F02-2.4 PiP

def test_F02_2_4_01_02_pip_floats_drags_resizes_and_survives_navigation(live):
    r = live.browser("pip", user="certb-viewer", name=KLIPPER)
    assert r["title"] == KLIPPER and r["live1"]["live"] and r["status1"].startswith("LIVE")
    assert (r["box1"]["w"], r["box1"]["h"]) == (320, 220)
    assert r["box2"]["x"] < r["box1"]["x"] - 150 and r["box2"]["y"] < r["box1"]["y"] - 150  # dragged
    assert (r["box3"]["w"], r["box3"]["h"]) == (240, 180) and (r["box4"]["w"], r["box4"]["h"]) == (320, 220)
    assert r["urlAfterNav"] == "/printers" and r["visibleAfterNav"] and r["boxAfterNav"]["x"] == r["box4"]["x"]
    assert r["liveAfterNav"]["live"] and r["timeAdvancedAcrossNav"]
    assert r["visibleAfterSecondNav"] and r["liveAfterSecondNav"]["live"] and r["countAfterClose"] == 0


# --------------------------------------------------------------------------- F02-2.5 camera detail

def test_F02_2_5_01_02_dedicated_route_large_stream_with_panels_alongside(live):
    r = live.browser("camera_detail", user="certb-viewer", id=live.ids["rtsp"], name=RTSP, tag="layout", fullscreen=True, breadcrumb=True)
    assert r["live"]["live"]
    assert r["flexDirection"] == "row" and abs(r["stream"]["y"] - r["panels"]["y"]) <= 1 and r["panels"]["x"] > r["stream"]["x"] + r["stream"]["w"] - 1
    share = r["stream"]["w"] / (r["stream"]["w"] + r["panels"]["w"])
    assert 0.68 <= share <= 0.72, share
    assert r["fullscreenElementIsStream"] is True and r["fullscreenAfterExit"] is False
    assert r["breadcrumbUrl"] == "/cameras" and r["breadcrumbLandsOnGrid"]
    live.detail_desktop = r


def test_F02_2_5_03_panels_status_temps_fan_job_progress_eta(live):
    r = getattr(live, "detail_desktop", None) or live.browser("camera_detail", user="certb-viewer", id=live.ids["rtsp"], name=RTSP)
    text = r["panelText"]
    assert "Printer Status" in r["panelTitles"] and "Online" in text and "Printing" in text
    assert "220°/220°C" in text and "60°/60°C" in text and "80%" in text  # nozzle, bed, fan
    assert "Current Job" in " ".join(r["panelTitles"]) and "CertB Benchy" in text
    assert r["progressBar"] == 1 and "42%" in text and "37m left" in text and "Layer 84/200" in text


def test_F02_2_5_03_filament_slot_shows_remaining_weight(live):
    r = getattr(live, "detail_desktop", None) or live.browser("camera_detail", user="certb-viewer", id=live.ids["rtsp"], name=RTSP)
    assert "Filament" in r["panelTitles"] and "640g" in r["panelText"], r["panelText"]


def test_F02_2_5_04_ai_badge_follows_per_printer_vision_setting(live):
    pid = live.ids["rtsp"]
    s, _ = live.json("PATCH", f"/api/printers/{pid}/vision", token=admin(live), json_body={"enabled": 0})
    assert s == 200
    off = live.browser("camera_detail", user="certb-viewer", id=pid, name=RTSP, tag="ai-off", settleMs=2500)
    s, _ = live.json("PATCH", f"/api/printers/{pid}/vision", token=admin(live), json_body={"enabled": 1})
    assert s == 200
    on = live.browser("camera_detail", user="certb-viewer", id=pid, name=RTSP, tag="ai-on", settleMs=2500)
    assert off["aiBadge"] is False and on["aiBadge"] is True


def test_F02_2_5_07_mobile_stacks_vertically(live):
    r = live.browser("camera_detail", user="certb-viewer", id=live.ids["rtsp"], name=RTSP, mobile=True)
    assert r["flexDirection"] == "column"
    assert r["panels"]["y"] >= r["stream"]["y"] + r["stream"]["h"] - 1 and abs(r["panels"]["x"] - r["stream"]["x"]) <= 1
    assert r["docScrollWidth"] <= 375 and r["live"]["live"]


# --------------------------------------------------------------------------- F02-2.6 TV dashboard

def test_F02_2_6_tv_dashboard_live(live):
    # Two unread print-failure alerts and one unread cancellation alert for the viewing user.
    uid = live.rows("SELECT id FROM users WHERE username='certb-viewer'")[0][0]
    for kind, sev, title in (("print_failed", "error", "CertB failure A"), ("print_failed", "error", "CertB failure B"), ("print_cancelled", "warning", "CertB cancel")):
        live.sql("INSERT INTO alerts(user_id,alert_type,severity,title,is_read,is_dismissed,created_at) VALUES(:u,:k,:s,:t,0,0,datetime('now'))",
                 {"u": uid, "k": kind, "s": sev, "t": title})
    r = live.browser("tv", user="certb-viewer", inspect=[RTSP, KLIPPER])
    live.tv = r
    assert r["tvLink"] == {"href": "/tv", "target": "_blank", "rel": "noopener noreferrer"}
    assert r["newTabUrl"] == "/tv" and r["originalTabUrl"] == "/cameras"
    assert r["rootBox"] == {"x": 0, "y": 0, "w": 1920, "h": 1080} and r["sidebarLinks"] == 0
    assert r["brand"] == "O.D.I.N."
    now = datetime.fromisoformat(r["nowUtc"].replace("Z", "+00:00"))
    assert any(re.fullmatch(r"\d{2}:\d{2}( [AP]M)?", t) for t in r["headerText"]), r["headerText"]
    assert now.strftime("%a, %b ") + str(now.day) in r["headerText"], r["headerText"]
    assert len(r["cardsPage1"]) == 12 and len(r["cardsPage2"]) == 1 and set(r["cardsPage1"]) | set(r["cardsPage2"]) == set([RTSP, KLIPPER] + FLEET)
    rtsp = r["cards"][RTSP]
    assert rtsp["status"] == "Printing" and rtsp["video"]["live"] and "CertB Benchy" in rtsp["text"] and "42%" in rtsp["text"] and "ETA 37m" in rtsp["text"]
    assert r["dots"] == 2 and r["activeDot"] == 0 and r["activeDotPage2"] == 1
    assert 28000 <= r["pageTurnAfterMs"] <= 34000, r["pageTurnAfterMs"]
    assert r["afterEscape"] == "/" and r["afterX"] == "/"


def test_F02_2_6_03_header_printing_count(live):
    r = live.tv
    assert "1/13 Printing" in r["headerText"], r["headerText"]


def test_F02_2_6_06_stats_bar_alert_count(live):
    s, summary = live.json("GET", "/api/alerts/summary", token=live.tokens["certb-viewer"])
    assert s == 200 and summary["total"] == 2
    assert "2 Active Alerts" in live.tv["statsText"], live.tv["statsText"]
    s, unread = live.json("GET", "/api/alerts/unread-count", token=live.tokens["certb-viewer"])
    live.tv_unread = unread  # 3 unread (cancellation not counted by the TV summary) -> claim wording recorded in verdicts


def test_F02_2_6_04_bambu_auto_camera_gets_tv_thumbnail(live):
    with spare_printer_slot(live):
        _bambu_tv(live)


def _bambu_tv(live):
    s, b = live.json("POST", "/api/printers", token=admin(live), json_body={"name": "CertB Bambu TV", "model": "X1C", "api_type": "bambu",
                                                                             "api_host": live.ip, "api_key": "01P00CERTB0002|certb456", "slot_count": 4})
    assert s == 201, b
    try:
        assert any(c["id"] == b["id"] for c in live.json("GET", "/api/cameras", token=admin(live))[1])
        printers = live.json("GET", "/api/printers", token=admin(live))[1]
        row = next(p for p in printers if p["id"] == b["id"])
        assert row["camera_url"], "TV hasCamera input is empty"
        # TVDashboard.tsx hasCamera = camera_url && camera_enabled; the access code stays masked.
        assert row["camera_enabled"] is True and row["camera_source"] == "auto" and "certb456" not in json.dumps(row)
        assert row["camera_url"] == f"rtsps://***@{live.ip}:322/streaming/live/1"
    finally:
        live.json("DELETE", f"/api/printers/{b['id']}", token=admin(live))


# --------------------------------------------------------------------------- F02-2.8-01..06 OBS overlay

def test_F02_2_8_01_03_04_overlay_is_public_shows_job_and_polls_every_5s(live):
    pid = live.ids["rtsp"]
    s, data, _ = live.request("GET", f"/api/overlay/{pid}")
    assert s == 200
    data = json.loads(data)
    assert data["printer_name"] == RTSP and data["job_name"] == "CertB Benchy" and data["print_progress"] == 42
    # The overlay page itself needs no login; its camera needs the copied URL's camera token.
    assert data["camera_url"] == f"/api/overlay/{pid}/camera"
    tok = camera_token(live)
    live.overlay_token = tok
    r = live.browser("overlay", id=pid, pollSeconds=16, query=f"?token={tok}", screenshotFrames=True)
    live.overlay = r
    assert r["url"] == f"/overlay/{pid}?token={tok}" and r["cookies"] == []
    assert not any("token=" in q["url"] for q in r["requests"] if q["url"].startswith(f"/api/overlay/{pid}") and not q["url"].startswith(f"/api/overlay/{pid}/camera"))
    texts = r["texts"]
    assert RTSP in texts and "CertB Benchy" in texts and "Printing" in texts and "42%" in texts and "ETA 37m" in texts and "Layer 84 / 200" in texts
    assert "220°C / 220°C" in texts and "60°C / 60°C" in texts and r["progressBarWidth"] == "42%"
    gaps = [b - a for a, b in zip(r["pollTimes"], r["pollTimes"][1:])]
    assert len(gaps) >= 2 and all(4500 <= g <= 5600 for g in gaps), gaps


def test_F02_2_8_02_overlay_full_screen_dark_70_30_layout(live):
    r = live.overlay
    assert r["rootBox"] == {"x": 0, "y": 0, "w": 1920, "h": 1080} and r["rootBg"] == "rgb(0, 0, 0)"
    assert r["cameraPanel"]["x"] == 0 and 0.66 <= r["cameraPanel"]["w"] / 1920 <= 0.72  # flex 7:3
    assert 0.28 <= r["statusPanel"]["w"] / 1920 <= 0.34 and r["statusPanel"]["x"] >= r["cameraPanel"]["w"] - 1


def test_F02_2_8_02_overlay_camera_feed_renders(live):
    r = live.overlay
    assert r["img"] and r["img"]["naturalWidth"] > 0 and r["img"]["display"] != "none", (r["img"], r.get("imgFetch"))
    assert (r["img"]["naturalWidth"], r["img"]["naturalHeight"]) == (640, 360)  # the RTSP test pattern
    assert r["imgFetch"]["status"] == 200 and r["imgFetch"]["type"].startswith("multipart/x-mixed-replace")
    assert _diff(*r["frameShots"]) > 0.05  # the feed is live (test-pattern clock advances), not a still


def test_F02_2_8_02_overlay_camera_requires_a_camera_token(live):
    pid = live.ids["rtsp"]
    assert stream_head(f"/api/overlay/{pid}/camera")[0] == 401
    assert stream_head(f"/api/overlay/{pid}/camera?token={live.tokens['certb-viewer']}")[0] == 401  # session JWTs are not accepted
    s, b = live.json("POST", "/api/tokens", token=live.tokens["certb-operator"], json_body={"name": "CertB jobs", "scopes": ["read:jobs"]})
    assert s == 200 and stream_head(f"/api/overlay/{pid}/camera?token={b['token']}")[0] == 403
    # read:cameras grants camera images only, never printer data.
    assert live.request("GET", "/api/printers", headers={"X-API-Key": live.overlay_token})[0] == 403
    no_token = live.browser("overlay", id=pid)
    assert no_token["img"] is None and no_token["cameraPanel"] is None and RTSP in no_token["texts"]
    # Revoking the token in Settings > API Tokens stops the stream.
    tokens = live.json("GET", "/api/tokens", token=live.tokens["certb-operator"])[1]
    victim = next(t for t in tokens if t["name"] == "CertB jobs")
    assert live.json("DELETE", f"/api/tokens/{victim['id']}", token=live.tokens["certb-operator"])[0] == 200
    assert stream_head(f"/api/overlay/{pid}/camera?token={b['token']}")[0] == 401


def test_F02_2_8_05_overlay_query_params(live):
    pid = live.ids["rtsp"]
    hidden = live.browser("overlay", id=pid, query=f"?camera=false&token={live.overlay_token}")
    assert hidden["img"] is None and hidden["cameraPanel"] is None and RTSP in hidden["texts"]
    light = live.browser("overlay", id=pid, query="?theme=light")
    dark = live.browser("overlay", id=pid, query="?theme=dark")
    assert light["rootBg"] == "rgb(255, 255, 255)" and dark["rootBg"] == "rgb(0, 0, 0)"


def test_F02_2_8_06_copy_overlay_url_from_printer_card(live):
    r = live.browser("copy_overlay", user="certb-operator", name=RTSP)
    base = f"http://127.0.0.1:{HTTP_PORT}/overlay/{live.ids['rtsp']}"
    assert r["buttonCount"] >= 2 and r["clipboard"].startswith(base + "?token=odin_") and r["tokenRequests"] == 1, r
    copied = r["clipboard"][len(base) + len("?token="):]
    status, kind, first = stream_head(f"/api/overlay/{live.ids['rtsp']}/camera?token={copied}")
    assert status == 200 and kind.startswith("multipart/x-mixed-replace") and first, (status, kind)
    tokens = live.json("GET", "/api/tokens", token=live.tokens["certb-operator"])[1]
    assert any(t["name"] == f"OBS overlay: {RTSP} (#{live.ids['rtsp']})" and t["scopes"] == ["read:cameras"] for t in tokens), tokens


# --------------------------------------------------------------------------- Klipper live print lifecycle

def _klipper_print(live, filename, terminal, **extra):
    live.sim(state="printing", filename=filename, progress=0.35, print_duration=600.0, filament_used=1234.0,
             current_layer=40, total_layer=120, extruder=[215.0, 215.0], bed=[60.0, 60.0], fan=0.5, **extra)
    kid = live.ids["klipper"]
    row = wait_until(lambda: live.rows("SELECT id,job_name,status FROM print_jobs WHERE printer_id=:p AND filename=:f", {"p": kid, "f": filename}), 60, 1, "klipper job start")
    state = wait_until(lambda: (lambda v: v if v not in (None, "STANDBY", "IDLE", "COMPLETE", "CANCELLED", "ERROR") else None)(
        live.rows("SELECT gcode_state FROM printers WHERE id=:p", {"p": kid})[0][0]), 40, 1, "monitor printing state")
    live.sim(state=terminal)
    wait_until(lambda: live.rows("SELECT status FROM print_jobs WHERE id=:i AND status!='running'", {"i": row[0][0]}), 60, 1, "klipper job end")
    archive = wait_until(lambda: live.rows("SELECT id,status,print_name,printer_id FROM print_archives WHERE print_job_id=:i", {"i": row[0][0]}), 60, 1, "archive")
    live.sim(state="standby", filename="", progress=0.0, extruder=[24.0, 0.0], bed=[23.0, 0.0], fan=0.0)
    time.sleep(4)
    return row[0][0], state, archive[0]


def test_F02_2_8_07_live_klipper_terminal_outcomes_create_archives(live):
    results = {}
    for filename, terminal, expected in (("certb_ok.gcode", "complete", "completed"), ("certb_cancel.gcode", "cancelled", "cancelled"),
                                         ("certb_error.gcode", "error", "failed")):
        pj, state, archive = _klipper_print(live, filename, terminal)
        results[filename] = (state, archive)
        assert archive[1] == expected and archive[3] == live.ids["klipper"] and archive[2] == filename, archive
    live.klipper_results = results


def test_klipper_printing_state_as_written_by_monitor(live):
    """Evidence for the cross-protocol state defects below: the Moonraker monitor stores PRINTING, not RUNNING."""
    states = {state for state, _ in live.klipper_results.values()}
    assert states == {"PRINTING"}, live.klipper_results


def test_F02_2_7_02_timelapse_captures_for_klipper_printer(live):
    kid = live.ids["klipper"]
    assert live.json("PATCH", f"/api/printers/{kid}", token=admin(live), json_body={"timelapse_enabled": True})[0] == 200
    fid = live.sql("INSERT INTO print_files(filename,original_filename,stored_path) VALUES('certb_klip_tl.gcode','certb_klip_tl.gcode','/data/uploads/certb_klip_tl.gcode')")["lastrowid"]
    jid = live.sql("INSERT INTO jobs(item_name,printer_id,print_file_id,status,actual_start,quantity,priority,hold) VALUES('certb_klip_tl.gcode',:p,:f,'printing',datetime('now'),1,3,0)", {"p": kid, "f": fid})["lastrowid"]
    live.sim(state="printing", filename="certb_klip_tl.gcode", progress=0.2, print_duration=100.0, current_layer=5, total_layer=50, extruder=[215.0, 215.0], bed=[60.0, 60.0])
    try:
        rows = []
        end = time.monotonic() + 75
        while time.monotonic() < end and not rows:
            rows = live.rows("SELECT id FROM timelapses WHERE printer_id=:p", {"p": kid}); time.sleep(3)
        assert rows, "no timelapse capture for a printing Klipper printer with timelapse enabled"
    finally:
        live.sim(state="complete"); time.sleep(5)
        live.sql("UPDATE jobs SET status='completed' WHERE id=:j", {"j": jid})
        live.sim(state="standby", filename="", progress=0.0, extruder=[24.0, 0.0], bed=[23.0, 0.0])
        live.json("PATCH", f"/api/printers/{kid}", token=admin(live), json_body={"timelapse_enabled": False})


def test_F02_2_8_03_overlay_shows_job_for_printing_klipper(live):
    kid = live.ids["klipper"]
    live.sim(state="printing", filename="certb_overlay.gcode", progress=0.5, print_duration=300.0, current_layer=10, total_layer=20, extruder=[215.0, 215.0], bed=[60.0, 60.0])
    try:
        wait_until(lambda: live.rows("SELECT id FROM print_jobs WHERE printer_id=:p AND filename='certb_overlay.gcode' AND status='running'", {"p": kid}), 60, 1, "job")
        wait_until(lambda: live.rows("SELECT gcode_state FROM printers WHERE id=:p", {"p": kid})[0][0] == "PRINTING", 60, 1, "monitor PRINTING state")
        r = live.browser("overlay", id=kid, query="?camera=false")
        state = live.rows("SELECT gcode_state FROM printers WHERE id=:p", {"p": kid})[0][0]
        assert "Printing" in r["texts"] and "certb_overlay.gcode" in r["texts"], (r["texts"], state, live.sim_status().get("state"))
    finally:
        live.sim(state="complete"); time.sleep(6)
        live.sim(state="standby", filename="", progress=0.0, extruder=[24.0, 0.0], bed=[23.0, 0.0])


# --------------------------------------------------------------------------- F02-2.7 timelapse (RTSP printer, real daemon + ffmpeg)

def _frames(live):
    out = live.sh("find /data/timelapses -name '*.jpg' -printf '%T@ %s %p\\n' 2>/dev/null | sort -n", check=False)
    return [(float(l.split()[0]), int(l.split()[1]), l.split()[2]) for l in out.splitlines() if l.strip()]


def test_F02_2_7_02_default_unscoped_printer_captures_frames(live):
    pid = live.ids["rtsp"]
    first = wait_until(lambda: live.rows("SELECT id,created_at FROM timelapses WHERE printer_id=:p AND print_job_id=:j", {"p": pid, "j": live.ids["tl_job"]}), 120, 3, "timelapse row")
    time.sleep(max(0, 70 - (time.time() - live.capture_started)))
    owner = live.rows("SELECT owner_state,ownership_source FROM timelapse_ownership_anchors WHERE timelapse_id=:t", {"t": first[0][0]})
    live.unscoped_timelapse = (first[0][0], owner)
    frames = live.rows("SELECT frame_count FROM timelapses WHERE id=:t", {"t": first[0][0]})[0][0]
    assert frames > 0, (frames, owner)
    # No organizations on this install: the org-less, unshared printer's capture belongs to the install.
    assert owner == [["shared", "first_capture_job_file"]], owner
    assert live.rows("SELECT COUNT(*) FROM groups WHERE is_org IS TRUE")[0][0] == 0


def test_F02_2_7_01_02_daemon_captures_jpeg_frames_every_30s_while_printing(live):
    pid = live.ids["rtsp"]
    # End the unscoped job; continue with the printer shared (an ownership state capture accepts).
    live.sql("UPDATE jobs SET status='completed',actual_end=datetime('now') WHERE id=:j", {"j": live.ids["tl_job"]})
    live.sql("UPDATE print_jobs SET status='completed',ended_at=datetime('now') WHERE id=:j", {"j": live.ids["tl_print_job"]})
    assert live.json("PATCH", f"/api/printers/{pid}", token=admin(live), json_body={"shared": True})[0] == 200
    time.sleep(35)  # daemon observes the ended job before the next one starts
    live.ids["tl_job"] = live.sql("INSERT INTO jobs(item_name,printer_id,print_file_id,status,actual_start,quantity,priority,hold) VALUES('CertB Benchy 2',:p,:f,'printing',datetime('now'),1,3,0)",
                                  {"p": pid, "f": live.ids["tl_file"]})["lastrowid"]
    live.sql("UPDATE print_files SET job_id=NULL WHERE id=:f", {"f": live.ids["tl_file"]})
    live.ids["tl_print_job"] = live.sql("INSERT INTO print_jobs(printer_id,job_id,filename,job_name,status,started_at,progress_percent,current_layer,total_layers,remaining_minutes,scheduled_job_id) "
                                        "VALUES(:p,'certb-2','certb_benchy.3mf','CertB Benchy 2','running',datetime('now'),5,4,200,90,:j)", {"p": pid, "j": live.ids["tl_job"]})["lastrowid"]
    tl = wait_until(lambda: live.rows("SELECT id,status,frame_count FROM timelapses WHERE printer_id=:p AND print_job_id=:j AND frame_count>=4",
                                      {"p": pid, "j": live.ids["tl_job"]}), 300, 5, "4 frames")
    # Frames of this recording only (each recording has its own frame namespace directory;
    # the earlier install-owned recording captured frames too).
    every = _frames(live)
    newest_dir = os.path.dirname(every[-1][2])
    frames = [f for f in every if os.path.dirname(f[2]) == newest_dir]
    assert len(frames) >= 4, every
    gaps = [b[0] - a[0] for a, b in zip(frames, frames[1:])]
    assert all(27 <= g <= 45 for g in gaps), gaps
    head = live.sh(f"head -c 3 {frames[0][2]} | od -An -tx1").split()
    assert head == ["ff", "d8", "ff"]  # JPEG SOI
    dims = live.sh(f"ffprobe -v error -select_streams v -show_entries stream=codec_name,width,height -of csv=p=0 {frames[0][2]}").strip()
    assert dims == "mjpeg,640,360", dims
    assert tl[0][1] == "capturing"
    prog = live.sh("cat /etc/supervisor/conf.d/odin.conf")
    assert "[program:timelapse_capture]" in prog and "python3 -m modules.archives.timelapse_capture" in prog
    # Disabled-timelapse fleet printers never get a recording (the Klipper printer is exercised separately).
    assert live.rows("SELECT COUNT(*) FROM timelapses t JOIN printers p ON p.id=t.printer_id WHERE p.name LIKE 'CertB Fleet%'")[0][0] == 0
    live.timelapse_id = tl[0][0]


def test_F02_2_7_03_print_completion_stitches_mp4_with_ffmpeg(live):
    pid, tid = live.ids["rtsp"], live.timelapse_id
    live.sql("UPDATE jobs SET status='completed',actual_end=datetime('now') WHERE id=:j", {"j": live.ids["tl_job"]})
    live.sql("UPDATE print_jobs SET status='completed',ended_at=datetime('now') WHERE id=:j", {"j": live.ids["tl_print_job"]})
    live.bambu.finish(); live.bambu.stop(); live.bambu.state = "FINISH"; live.bambu.tick()
    row = wait_until(lambda: live.rows("SELECT id,status,filename,frame_count,duration_seconds,file_size_mb FROM timelapses WHERE id=:t AND status='ready'", {"t": tid}), 240, 5, "encode")
    _, status, filename, frames, duration, size = row[0]
    info = json.loads(live.sh(f"ffprobe -v error -count_frames -select_streams v -show_entries stream=codec_name,width,height,nb_read_frames,avg_frame_rate "
                              f"-show_entries format=format_name,duration -of json /data/timelapses/{filename}"))
    stream = info["streams"][0]
    assert stream["codec_name"] == "h264" and (stream["width"], stream["height"]) == (640, 360)
    assert int(stream["nb_read_frames"]) == frames and frames >= 4
    assert "mp4" in info["format"]["format_name"] and stream["avg_frame_rate"] == "30/1"
    assert abs(float(info["format"]["duration"]) - duration) < 0.2 and size > 0
    live.timelapse = {"id": tid, "filename": filename, "frames": frames, "duration": duration}


def _ready_ids(live):
    return [t["id"] for t in live.json("GET", "/api/timelapses?status=ready", token=admin(live))[1]["timelapses"]]


def test_F02_2_7_04_api_list_filters_pagination(live):
    pid, tid = live.ids["rtsp"], live.timelapse["id"]
    tok = live.tokens["certb-viewer"]
    s, all_ = live.json("GET", "/api/timelapses", token=tok)
    assert s == 200 and all_["total"] >= 1 and all_["limit"] == 50 and all_["offset"] == 0
    # The earlier install-owned recording of the same printer was also encoded when its job ended.
    first = live.unscoped_timelapse[0]
    assert live.rows("SELECT status FROM timelapses WHERE id=:t", {"t": first})[0][0] == "ready"
    assert [t["id"] for t in live.json("GET", f"/api/timelapses?printer_id={pid}&status=ready", token=tok)[1]["timelapses"]] == sorted([tid, first], reverse=True)
    # The Klipper recording (PRINTING state, install-owned) is filtered by printer and visible to the viewer.
    klipper = live.rows("SELECT COUNT(*) FROM timelapses WHERE printer_id=:p", {"p": live.ids["klipper"]})[0][0]
    assert klipper >= 1 and live.json("GET", f"/api/timelapses?printer_id={live.ids['klipper']}", token=tok)[1]["total"] == klipper
    assert live.json("GET", "/api/timelapses?status=failed", token=tok)[1]["timelapses"] == []
    page = live.json("GET", "/api/timelapses?limit=1&offset=0", token=tok)[1]
    assert page["limit"] == 1 and len(page["timelapses"]) == 1
    assert live.request("GET", "/api/timelapses?limit=201", token=tok)[0] == 422


def test_F02_2_7_04_api_serves_mp4_with_query_token_for_video_element(live):
    tid = live.timelapse["id"]
    tok = live.tokens["certb-viewer"]
    s, body, headers = live.request("GET", f"/api/timelapses/{tid}/video?token={tok}")
    assert s == 200 and headers["content-type"] == "video/mp4" and body[4:8] == b"ftyp"
    assert live.request("GET", f"/api/timelapses/{tid}/video")[0] == 401
    r = live.browser("token_video", src=f"/api/timelapses/{tid}/video?token={tok}")
    assert r["cookies"] == [] and r["video"]["w"] == 640 and r["video"]["d"] > 0 and not r["video"]["err"], r["video"]


def test_F02_2_7_04_delete_is_admin_only(live):
    tid = live.timelapse["id"]
    s, _, headers = live.request("GET", f"/api/timelapses/{tid}", token=admin(live))
    etag = headers["etag"]
    for user in ("certb-viewer", "certb-operator"):
        assert live.request("DELETE", f"/api/timelapses/{tid}", token=live.tokens[user], headers={"If-Match": etag})[0] == 403
    assert live.request("DELETE", f"/api/timelapses/{tid}", token=admin(live))[0] == 428
    assert tid in _ready_ids(live)


def test_F02_2_7_05_gallery_grid_player_filters_pagination_download(live):
    tid, pid = live.timelapse["id"], live.ids["rtsp"]
    r = live.browser("timelapse_gallery", user="certb-admin", play=tid, download=tid, filterPrinter=pid, filterStatus="ready")
    live.gallery = r
    card = next(a for a in r["articles"] if any(t.startswith(f"Recording {tid}:") for t in a))
    assert card[0] == RTSP and f"Recording {tid}: ready, {live.timelapse['frames']} frames" in card
    assert r["gridColumns"] == 3 and r["pagination"] == "Page 1 of 1"
    assert {f"Play recording {tid}", f"Download recording {tid}", f"Edit recording {tid}", f"Delete recording {tid}"} <= set(r["buttons"])
    assert any(f"printer_id={pid}" in u and "status=ready" in u and "limit=24" in u for u in r["filterRequests"]), r["filterRequests"]
    # The recording is frame_count/30 s long (4 frames = 0.133 s), so "played" means it advanced 0.2 s or reached its end.
    assert r["player"]["w"] == 640 and r["player"]["d"] > 0 and r["player"]["t"] >= min(0.2, r["player"]["d"]) - 0.001, r["player"]
    assert r["download"]["suggested"] == f"odin_timelapse_{tid}.mp4" and r["download"]["bytes"] > 1000
    with open(r["download"]["path"], "rb") as f:
        assert f.read(8)[4:8] == b"ftyp"
    viewer = live.browser("timelapse_delete", user="certb-viewer")
    assert viewer["deleteButtons"] == []


def test_F02_2_7_05_player_is_a_modal(live):
    """Recorded for the claim text: the player renders inline in the page, not as a modal dialog."""
    assert live.gallery["playerIsDialog"] is False


def test_F02_2_7_07_editor_trim_and_speed_with_real_ffmpeg(live):
    tid = live.timelapse["id"]
    duration = live.timelapse["duration"]
    end = 0.1  # slider step is 0.1 s; the recording is a few 30 fps frames long
    r = live.browser("timelapse_editor", user="certb-operator", id=tid, trimStart=0, trimEnd=end, speed=2, timeout=600)
    live.editor = r
    assert r["dialogModal"] == "true" and r["preview"]["w"] == 640 and r["preview"]["d"] > 0
    assert [s["label"] for s in r["sliders"]] == ["Trim start", "Trim end"] and all(float(s["max"]) == pytest.approx(duration) for s in r["sliders"])
    assert r["speedOptions"] == ["0.5x", "1x", "1.5x", "2x", "4x", "8x"]
    assert r["numbersAfterSlider"] == ["0", str(end)]
    body = json.loads(r["editRequests"][0]["body"])
    assert r["editRequests"][0]["url"] == f"/api/timelapses/{tid}/trim" and body["start_seconds"] == 0 and body["end_seconds"] == pytest.approx(end)
    assert r["trimReceipt"].startswith("Edit completed:") and r["trimVideo"]["w"] == 640 and r["trimVideo"]["d"] > 0
    assert r["speedReceipt"].startswith("Edit completed:")
    ids = _ready_ids(live)
    assert len(ids) >= 2, ids  # speed creates another recording
    for href in (r["trimDownloadHref"], r["speedDownloadHref"]):
        s, data, headers = live.request("GET", href.split("?")[0] + ("?" + href.split("?")[1] if "?" in href else ""), token=live.tokens["certb-operator"])
        assert s == 200 and headers["content-type"] == "video/mp4" and data[4:8] == b"ftyp", (href, s)
    # Speed 2x halves the duration of the (trimmed) source it was applied to.
    secs = [float(re.search(r"([0-9.]+) seconds", t).group(1)) for t in (r["trimReceipt"], r["speedReceipt"])]
    assert secs[0] == pytest.approx(0.1, abs=0.034) and secs[1] == pytest.approx(secs[0] / 2, abs=0.034), secs
    assert live.request("POST", f"/api/timelapses/{tid}/speed", token=live.tokens["certb-viewer"], json_body={"multiplier": 2, "operation_id": "a" * 32})[0] == 403


def test_F02_2_7_04_05_admin_deletes_from_gallery(live):
    victim = max(_ready_ids(live))
    r = live.browser("timelapse_delete", user="certb-admin", id=victim)
    assert r["deleted"] and r["deleteRequests"] and r["deleteRequests"][0]["url"] == f"/api/timelapses/{victim}"
    assert victim not in _ready_ids(live)
    assert live.request("GET", f"/api/timelapses/{victim}/video?token={admin(live)}")[0] != 200


# --------------------------------------------------------------------------- F02-2.8-07..39 archives, projects, 3D

CUBE_MODEL = ('<?xml version="1.0" encoding="UTF-8"?>\n<model unit="millimeter" xml:lang="en-US" '
              'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02"><metadata name="Title">CertB Cube</metadata><resources>'
              '<object id="1" type="model"><mesh><vertices>'
              + "".join(f'<vertex x="{x}" y="{y}" z="{z}"/>' for x, y, z in
                        [(0, 0, 0), (20, 0, 0), (20, 20, 0), (0, 20, 0), (0, 0, 20), (20, 0, 20), (20, 20, 20), (0, 20, 20)])
              + '</vertices><triangles>'
              + "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in
                        [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7), (0, 1, 5), (0, 5, 4), (1, 2, 6), (1, 6, 5), (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)])
              + '</triangles></mesh></object></resources><build><item objectid="1"/></build></model>')


def two_plate_sliced_3mf():
    """Real Bambu Studio sliced export (repo fixture) with a cube mesh and a second plate."""
    import hashlib
    source = FIXTURES / "a1-acceptance-coupon.gcode.3mf"
    buf = io.BytesIO()
    with zipfile.ZipFile(source) as src, zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            data = CUBE_MODEL.encode() if item.filename == "3D/3dmodel.model" else src.read(item.filename)
            dst.writestr(item.filename, data)
        gcode = src.read("Metadata/plate_1.gcode")
        dst.writestr("Metadata/plate_2.gcode", gcode)
        dst.writestr("Metadata/plate_2.gcode.md5", hashlib.md5(gcode).hexdigest())
    return buf.getvalue()


def _pixels(path):
    import cv2
    return cv2.imread(path)


def _diff(a, b):
    import numpy as np
    return float(np.abs(_pixels(a).astype(int) - _pixels(b).astype(int)).mean())


def test_archive_seed_upload_and_history(live):
    op = live.tokens["certb-operator"]
    s, up = live.upload("/api/print-files/upload", "certb_cube_two_plate.gcode.3mf", two_plate_sliced_3mf(), op)
    assert s == 200, up
    live.ids["file"], live.ids["model"] = up["id"], up["model_id"]
    pf = live.rows("SELECT stored_path,thumbnail_b64 IS NOT NULL,mesh_data IS NOT NULL,plate_count FROM print_files WHERE id=:i", {"i": up["id"]})[0]
    live.upload_row = pf
    assert pf[2] == 1  # mesh extracted and stored at upload
    rtsp, klip = live.ids["rtsp"], live.ids["klipper"]
    statuses = ["completed", "failed", "cancelled"]
    for i in range(1, 56):
        tags = "alpha, beta" if i % 5 == 0 else ("gamma" if i % 7 == 0 else None)
        live.sql("INSERT INTO print_archives(print_name,printer_id,status,started_at,completed_at,actual_duration_seconds,filament_used_grams,cost_estimate,"
                 "tags,ownership_state,ownership_source,accounting_state,accounting_reason,created_at,user_id) VALUES(:n,:p,:s,datetime('2026-09-01','+'||:i||' hours'),"
                 "datetime('2026-09-01','+'||:i||' hours','+'||:d||' seconds'),:d,:g,:c,:t,'known','certb_seed','unresolved','certb_seed',datetime('2026-09-01','+'||:i||' hours'),"
                 "(SELECT id FROM users WHERE username='certb-operator'))",
                 {"n": f"CertB Archive {i:02d}", "p": rtsp if i % 2 else klip, "s": statuses[i % 3], "i": i, "d": 600 + 60 * i,
                  "g": round(10 + i * 1.5, 1), "c": round(0.5 + i * 0.1, 2), "t": tags})
    # created_at is in the future so the 3D part is the newest row for every role: the list orders by created_at DESC
    # and admins also see the live-print archives (created during this run).
    live.ids["archive_3d"] = live.sql(
        "INSERT INTO print_archives(print_name,printer_id,status,started_at,completed_at,actual_duration_seconds,filament_used_grams,cost_estimate,thumbnail_b64,"
        "file_path,print_file_id,plate_count,ownership_state,ownership_source,accounting_state,accounting_reason,created_at,user_id) "
        "SELECT 'CertB 3D Part',:p,'completed',datetime('2026-10-01 10:00'),datetime('2026-10-01 11:00'),3600,12.5,1.25,thumbnail_b64,stored_path,id,plate_count,"
        "'known','certb_seed','unresolved','certb_seed',datetime('now','+1 day'),(SELECT id FROM users WHERE username='certb-operator') FROM print_files WHERE id=:f",
        {"p": rtsp, "f": up["id"]})["lastrowid"]
    s, page = live.json("GET", "/api/archives", token=admin(live))
    live_prints = live.rows("SELECT COUNT(*) FROM print_archives WHERE ownership_source!='certb_seed'")[0][0]
    live.archive_total = 56 + live_prints
    assert live_prints >= 3 and s == 200 and page["total"] == live.archive_total and page["per_page"] == 50 and len(page["items"]) == 50
    # The live Klipper prints were started at the printer (no ODIN job), so the archive writer records
    # ownership_state='unresolved'. This install has no organizations, so that history belongs to the install
    # and every role sees it (modules/archives/access.py); with organizations it would be admin-only.
    unresolved = live.rows("SELECT COUNT(*) FROM print_archives WHERE ownership_source!='certb_seed' AND ownership_state='unresolved'")[0][0]
    assert unresolved >= 3
    live.archive_total_scoped = live.archive_total
    for user in ("certb-viewer", "certb-operator"):
        s, vpage = live.json("GET", "/api/archives", token=live.tokens[user])
        assert s == 200 and vpage["total"] == live.archive_total, (user, vpage["total"], live.archive_total, unresolved)


def test_F02_2_8_27_uploaded_two_plate_file_records_plate_count(live):
    assert live.upload_row[3] == 2, live.upload_row


def test_F02_2_8_15_archive_api_crud_and_roles(live):
    adm, op, view = admin(live), live.tokens["certb-operator"], live.tokens["certb-viewer"]
    s, page2 = live.json("GET", "/api/archives?page=2&per_page=50", token=view)
    assert s == 200 and page2["page"] == 2 and len(page2["items"]) == live.archive_total_scoped - 50
    s, failed = live.json("GET", f"/api/archives?status=failed&printer_id={live.ids['rtsp']}&per_page=200", token=view)
    assert failed["items"] and all(a["status"] == "failed" and a["printer_id"] == live.ids["rtsp"] for a in failed["items"])
    s, found = live.json("GET", "/api/archives?search=Archive%2007", token=view)
    assert [a["print_name"] for a in found["items"]] == ["CertB Archive 07"]
    aid = found["items"][0]["id"]
    s, one = live.json("GET", f"/api/archives/{aid}", token=view)
    assert s == 200 and one["print_name"] == "CertB Archive 07" and one["printer_display"] == RTSP
    assert live.json("PATCH", f"/api/archives/{aid}", token=view, json_body={"notes": "viewer"})[0] == 403
    assert live.json("PATCH", f"/api/archives/{aid}", token=op, json_body={"notes": "Operator note"})[0] == 200
    assert live.json("GET", f"/api/archives/{aid}", token=view)[1]["notes"] == "Operator note"
    victim = live.json("GET", "/api/archives?search=Archive%2055", token=view)[1]["items"][0]["id"]
    assert live.json("DELETE", f"/api/archives/{victim}", token=op)[0] == 403
    assert live.json("DELETE", f"/api/archives/{victim}", token=adm)[0] == 200
    assert live.json("GET", f"/api/archives/{victim}", token=adm)[0] == 404


def test_F02_2_8_17_18_print_log_api_and_csv(live):
    view = live.tokens["certb-viewer"]
    s, log = live.json("GET", "/api/archives/log?per_page=10&page=1", token=view)
    assert s == 200 and log["per_page"] == 10 and len(log["items"]) == 10 and log["total"] == live.archive_total_scoped - 1
    completed = [i["completed_at"] for i in log["items"]]
    assert completed == sorted(completed, reverse=True)
    s, filt = live.json("GET", f"/api/archives/log?status=cancelled&printer_id={live.ids['klipper']}&per_page=200", token=view)
    assert filt["items"] and all(i["status"] == "cancelled" and i["printer_id"] == live.ids["klipper"] for i in filt["items"])
    s, body, headers = live.request("GET", "/api/archives/log/export?status=cancelled&tag=alpha", token=view)
    assert s == 200 and headers["content-type"].startswith("text/csv") and "print_log.csv" in headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(body.decode())))
    expected = live.json("GET", "/api/archives/log?status=cancelled&tag=alpha&per_page=200", token=view)[1]["items"]
    assert rows and len(rows) == len(expected) and all(r["Status"] == "cancelled" and "alpha" in r["Tags"] for r in rows)
    assert list(rows[0]) == ["Print Name", "Printer", "User", "Status", "Started", "Completed", "Duration (s)", "Filament (g)", "Cost", "Tags"]


def test_F02_2_8_19_20_21_compare_api(live):
    view = live.tokens["certb-viewer"]
    ids = {a["print_name"]: a["id"] for a in live.json("GET", "/api/archives?per_page=200", token=view)[1]["items"]}
    s, cmp_ = live.json("GET", f"/api/archives/compare?a={ids['CertB Archive 01']}&b={ids['CertB Archive 03']}", token=view)
    assert s == 200 and cmp_["a"]["print_name"] == "CertB Archive 01" and cmp_["b"]["print_name"] == "CertB Archive 03"
    assert set(cmp_["diff"]) == {"status", "actual_duration_seconds", "filament_used_grams", "cost_estimate", "started_at", "completed_at"}
    s, cross = live.json("GET", f"/api/archives/compare?a={ids['CertB Archive 01']}&b={ids['CertB Archive 02']}", token=view)
    assert "printer_display" in cross["diff"]
    live.archive_ids = ids


def test_F02_2_8_22_24_25_tags_api(live):
    aid = live.archive_ids["CertB Archive 11"]
    assert live.json("PATCH", f"/api/archives/{aid}/tags", token=live.tokens["certb-viewer"], json_body={"tags": ["x"]})[0] == 403
    s, b = live.json("PATCH", f"/api/archives/{aid}/tags", token=live.tokens["certb-operator"], json_body={"tags": ["  delta ", "delta", "", " epsilon"]})
    assert s == 200 and b == {"tags": ["delta", "epsilon"]}
    assert live.rows("SELECT tags FROM print_archives WHERE id=:i", {"i": aid})[0][0] == "delta, epsilon"
    assert [a["id"] for a in live.json("GET", "/api/archives?tag=delta", token=admin(live))[1]["items"]] == [aid]


def test_F02_2_8_11_12_13_16_19_20_23_archive_pages_in_browser(live):
    r = live.browser("archives", user="certb-admin", search="CertB Archive 07", printer=live.ids["klipper"], status="failed", tag="alpha",
                     compare=["CertB Archive 52", "CertB Archive 50"])
    live.archive_ui = r
    assert [h.strip() for h in r["headers"]] == ["Compare", "", "Name", "Printer", "Status", "Duration", "Filament", "Date"]
    assert r["rowCount"] == 50 and r["page2Rows"] == live.archive_total - 51 and r["pager"] == "1 / 2" and r["pagerAfter"] == "2 / 2"
    assert any("page=2" in u and "per_page=50" in u for u in r["pageRequests"]), r["pageRequests"]
    first = r["firstRow"]
    assert first["img"] and first["badge"] and first["cells"][2] == "CertB 3D Part" and first["cells"][3] == RTSP and "1h" in first["cells"][5] and first["cells"][6] == "12.5g"
    assert r["afterSearch"] == ["CertB Archive 07"]
    assert r["afterPrinter"] and set(r["afterPrinter"]) == {KLIPPER}
    assert r["afterStatus"] and {s.strip().lower() for s in r["afterStatus"]} == {"failed"}
    assert any("search=CertB" in u for u in r["filterRequests"]) and any(f"printer_id={live.ids['klipper']}" in u for u in r["filterRequests"]) and any("status=failed" in u for u in r["filterRequests"])
    assert r["afterTag"] and all(int(n.split()[-1]) % 5 == 0 for n in r["afterTag"])
    rows = {row["cells"][0]: row for row in r["compareRows"]}
    assert set(rows) == {"Printer", "Status", "Duration", "Filament (g)", "Cost", "Started", "Completed"}
    assert not rows["Printer"]["highlighted"] and all(rows[k]["highlighted"] for k in ("Status", "Duration", "Filament (g)", "Cost", "Started", "Completed"))
    assert sorted(r["compareHeads"][1:]) == ["CertB Archive 50", "CertB Archive 52"]


def test_F02_2_8_23_tag_click_writes_tag_to_url(live):
    assert "tag=alpha" in live.archive_ui["urlAfterTag"], live.archive_ui["urlAfterTag"]


def test_F02_2_8_14_detail_modal_roles(live):
    target = "CertB Archive 13"
    op = live.browser("archives", user="certb-operator", detail=target, notes="Nozzle swapped mid-run", addTag="  reviewed ")
    assert "Printer" in op["detailText"] and "Duration" in op["detailText"] and "Filament" in op["detailText"] and "Completed" in op["detailText"]
    assert op["detailHasNotes"] == 1 and op["detailHasDelete"] == 0
    aid = live.archive_ids[target]
    assert op["notesPatch"] == [f"/api/archives/{aid}"] and live.rows("SELECT notes FROM print_archives WHERE id=:i", {"i": aid})[0][0] == "Nozzle swapped mid-run"
    assert op["tagPatch"] == [f"/api/archives/{aid}/tags"] and live.rows("SELECT tags FROM print_archives WHERE id=:i", {"i": aid})[0][0] == "reviewed"
    viewer = live.browser("archives", user="certb-viewer", detail=target)
    assert viewer["detailHasNotes"] == 0 and viewer["detailHasDelete"] == 0 and viewer["detailHasTagEditor"] == 0
    adm = live.browser("archives", user="certb-admin", detail="CertB Archive 54", deleteDetail=True)
    assert adm["detailHasDelete"] == 1 and adm["deleteRequests"] == [f"/api/archives/{live.archive_ids['CertB Archive 54']}"]
    assert live.json("GET", f"/api/archives/{live.archive_ids['CertB Archive 54']}", token=admin(live))[0] == 404


def _assert_interactive_viewer(r, fid_path):
    assert r["meshInfo"] == "8 vertices · 12 triangles" and any(fid_path in u for u in r["meshRequests"]), r["meshRequests"]
    assert r["webgl"]["hasContext"] and r["webgl"]["w"] > 0
    import numpy as np
    initial = _pixels(r["shots"]["initial"])
    assert len(np.unique(initial.reshape(-1, 3), axis=0)) > 20  # the cube is actually rendered, not a blank canvas
    for moved in ("rotated", "panned", "zoomed", "wireframe", "zoomIn", "touchRotated", "pinched"):
        assert _diff(r["shots"]["initial"] if moved != "pinched" else r["shots"]["touchBase"], r["shots"][moved]) > 0.5, moved
    assert r["pageScrollAfterWheel"] == 0
    assert all(any(w in b.lower() for b in r["viewerButtons"]) for w in ("wireframe", "zoom in", "zoom out", "reset"))


def test_F02_2_8_30_31_32_33_archive_3d_preview(live):
    r = live.browser("viewer3d", user="certb-viewer", **{"from": "archive", "name": "CertB 3D Part"})
    live.viewer_archive = r
    _assert_interactive_viewer(r, f"/api/print-files/{live.ids['file']}/mesh")


def test_F02_2_8_32_reset_restores_initial_view(live):
    r = live.viewer_archive
    assert _diff(r["shots"]["initial"], r["shots"]["reset"]) < 0.5


def test_F02_2_8_34_models_page_3d_preview(live):
    name = live.rows("SELECT name FROM models WHERE id=:i", {"i": live.ids["model"]})[0][0]
    r = live.browser("viewer3d", user="certb-viewer", **{"from": "models", "name": name})
    assert r["previewButtons"] >= 1
    _assert_interactive_viewer(r, f"/api/models/{live.ids['model']}/mesh")


def test_F02_2_8_26_28_29_reprint_from_archive_creates_pending_job(live):
    r = live.browser("archives", user="certb-operator", detail="CertB 3D Part", reprint={"printer": live.ids["rtsp"], "plate": None})
    live.reprint_ui = r
    assert r["detailHasReprint"] == 1 and r["reprintPost"] == [f"/api/archives/{live.ids['archive_3d']}/reprint"]
    job = live.rows("SELECT item_name,status,model_id,print_file_id,printer_id,plate_index FROM jobs WHERE item_name='Reprint: CertB 3D Part'")
    assert job == [["Reprint: CertB 3D Part", "pending", live.ids["model"], live.ids["file"], live.ids["rtsp"], 0]], job
    s, prev = live.json("GET", f"/api/archives/{live.ids['archive_3d']}/ams-preview", token=live.tokens["certb-viewer"])
    assert s == 200 and prev["required"] and prev["available"] == [] and prev["suggested_mapping"] == []


def test_F02_2_8_29_ams_preview_for_target_printer(live):
    s, prev = live.json("GET", f"/api/archives/{live.ids['archive_3d']}/ams-preview?printer_id={live.ids['rtsp']}", token=live.tokens["certb-viewer"])
    assert s == 200 and prev["available"], (s, prev)


def test_F02_2_8_26_reprint_shows_ams_mapping_preview(live):
    r = live.reprint_ui
    assert r["reprintAmsRequestsAfterPrinter"], r["reprintTextAfterPrinter"]


def test_F02_2_8_16_17_print_log_page_and_csv_download(live):
    r = live.browser("print_log", user="certb-viewer", status="cancelled")
    assert [h.strip() for h in r["headers"]][:7] == ["Date", "Name", "Printer", "User", "Status", "Duration", "Filament"]
    assert len(r["rows"]) == 50 and r["afterStatus"] and all("cancelled" in " ".join(row).lower() for row in r["afterStatus"])
    with open(r["csvPath"], newline="") as f:
        rows = list(csv.DictReader(f))
    assert r["csvName"] == "print_log.csv" and rows and all(x["Status"] == "cancelled" for x in rows)
    assert any("status=cancelled" in u for u in r["exportRequests"]), r["exportRequests"]


def test_F02_2_8_35_39_projects_api_crud_bulk_export_import(live):
    op, adm, view = live.tokens["certb-operator"], admin(live), live.tokens["certb-viewer"]
    s, p = live.json("POST", "/api/projects", token=op, json_body={"name": "CertB Brackets", "description": "Bracket batch", "color": "#22c55e"})
    assert s == 200 and p["status"] == "active"
    pid = p["id"]
    assert live.json("POST", "/api/projects", token=view, json_body={"name": "nope"})[0] == 403
    assert any(x["id"] == pid for x in live.json("GET", "/api/projects", token=view)[1])
    assert live.json("PUT", f"/api/projects/{pid}", token=op, json_body={"name": "CertB Brackets v2"})[0] == 200
    ids = [live.archive_ids[n] for n in ("CertB Archive 20", "CertB Archive 21", "CertB Archive 22")]
    s, b = live.json("POST", f"/api/projects/{pid}/archives", token=op, json_body={"archive_ids": ids + [ids[0]]})
    assert s == 200 and b == {"assigned": 3}
    s, detail = live.json("GET", f"/api/projects/{pid}", token=view)
    assert detail["name"] == "CertB Brackets v2" and sorted(a["id"] for a in detail["archives"]) == sorted(ids) and detail["archive_count"] == 3
    assert next(x for x in live.json("GET", "/api/projects", token=view)[1] if x["id"] == pid)["archive_count"] == 3
    s, zipped, headers = live.request("GET", f"/api/projects/{pid}/export", token=op)
    assert s == 200 and headers["content-type"] == "application/zip" and zipped[:2] == b"PK"
    with zipfile.ZipFile(io.BytesIO(zipped)) as z:
        names = z.namelist()
        manifest = json.loads(z.read(next(n for n in names if n.endswith(".json"))))
    blob = json.dumps(manifest)
    assert "CertB Brackets v2" in blob and all(f"CertB Archive {n}" in blob for n in ("20", "21", "22"))
    assert live.json("DELETE", f"/api/projects/{pid}", token=op)[0] == 403
    assert live.json("DELETE", f"/api/projects/{pid}", token=adm)[1] == {"status": "archived"}
    assert live.json("GET", f"/api/projects/{pid}", token=view)[1]["status"] == "archived"
    s, imp = live.upload("/api/projects/import", "project_export.zip", zipped, op)
    assert s == 200 and imp["status"] == "imported" and imp["archives"] == 3, imp
    s, restored = live.json("GET", f"/api/projects/{imp['id']}", token=view)
    assert restored["name"] == "CertB Brackets v2" and restored["status"] == "active"
    assert sorted(a["print_name"] for a in restored["archives"]) == ["CertB Archive 20", "CertB Archive 21", "CertB Archive 22"]
    live.project_manifest_names = names
