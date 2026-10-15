"""Cert G: backend halves of F16 interface claims through the real app factory.

Theme (16.1-03/04), fleet data (16.2-02), global search (16.2-03), language
(16.5-01), maintenance status (16.6-02), ui_mode (16.7-02), admin logs
(16.8-01..06) and support bundle (16.9-01..04). Real login JWTs, real routers,
bootstrapped SQLite. Hard-coded /data/... log paths are redirected to probe
files (filesystem boundary); nothing else is substituted.
"""
import base64
import io
import json
import secrets
import zipfile

import pytest

from tests.test_contracts.test_cert_G_harness import run_probe

SECRETS = {
    "ipv4": "192.168.77.41",
    # Secret-shaped values are generated per run (no secret-shaped literals in the repo).
    "password_label": "HunterPass-" + secrets.token_hex(3),
    "jwt": ".".join(base64.urlsafe_b64encode(part).decode().rstrip("=") for part in
                    (b'{"alg":"HS256"}', b'{"sub":"cert-g"}', secrets.token_bytes(24))),
    "odin_token": "odin_" + secrets.token_hex(12),
    "url_credentials": "CamPass-" + secrets.token_hex(3),
    "ipv6": "fd00:abcd:12::41",
    "prefixed_password_label": "SmtpSecret-" + secrets.token_hex(3),
}

PROBE = r'''
import threading, io, zipfile, base64 as b64
from datetime import datetime, timezone, timedelta
from modules.system import routes_admin, diagnostics
S = json.loads(os.environ["CERT_G_SECRETS"])
logdir = WORK / "logs"; logdir.mkdir(exist_ok=True)
for src in list(routes_admin._LOG_FILES):
    p = logdir / (src + ".log")
    p.write_text("".join(f"2026-10-14 10:00:{i:02d} [{lvl}] odin.{src}: {src}-marker line {i}\n"
                         for i, lvl in enumerate(["INFO", "WARNING", "ERROR", "DEBUG", "INFO", "INFO"])))
    routes_admin._LOG_FILES[src] = str(p)
backend_log = Path(routes_admin._LOG_FILES["backend"])
with backend_log.open("a") as f:
    f.write("2026-10-14 10:01:00 [INFO] odin.printers: Retrying MQTT connect after transient error\n")
    f.write("2026-10-14 10:01:01 [ERROR] odin.printers: connect to " + S["ipv4"] + " failed password=" + S["password_label"] + "\n")
    f.write("2026-10-14 10:01:02 [ERROR] odin.auth: rejected Bearer " + S["jwt"] + " and token " + S["odin_token"] + "\n")
    f.write("2026-10-14 10:01:03 [WARNING] odin.camera: rtsp://admin:" + S["url_credentials"] + "@camera.local/stream timeout\n")
    f.write("2026-10-14 10:01:04 [ERROR] odin.mqtt: broker [" + S["ipv6"] + "]:8883 unreachable\n")
    f.write("2026-10-14 10:01:05 [ERROR] odin.alerts: smtp send failed {'smtp_password': '" + S["prefixed_password_label"] + "'}\n")
_real_tail = diagnostics._read_tail
diagnostics._read_tail = lambda path, n: _real_tail(str(backend_log) if path == "/data/backend.log" else path, n)

admin_id = seed_user("cert-g-admin", "admin")
op_id = seed_user("cert-g-operator", "operator"); vw_id = seed_user("cert-g-viewer", "viewer")
install_license("pro")
a = login("cert-g-admin"); op = login("cert-g-operator"); vw = login("cert-g-viewer")

# ---- theme ----
T = OUT["theme"] = {}
T["default"] = body(vw.get("/api/auth/me/theme"))
pref = {"accent_color": "#0ea5e9", "sidebar_style": "light", "background": "default"}
r = vw.put("/api/auth/me/theme", json=pref); T["put"] = [r.status_code, body(r)]
other_device = login("cert-g-viewer")                       # second session/token for same account
T["other_device"] = body(other_device.get("/api/auth/me/theme"))
T["stored_column"] = sql("SELECT theme_json FROM users WHERE id=:i", i=vw_id)[0][0]
op.put("/api/auth/me/theme", json={"accent_color": "#ef4444", "sidebar_style": "dark"})
T["viewer_after_operator_put"] = body(vw.get("/api/auth/me/theme"))
T["operator"] = body(op.get("/api/auth/me/theme"))
T["anon"] = client().get("/api/auth/me/theme").status_code
r = op.put("/api/auth/me/theme", json={"accent_color": "red;background:url(https://evil.example/x)", "sidebar_style": 42})
T["invalid_put"] = r.status_code
T["non_object_put"] = op.put("/api/auth/me/theme", json=["not", "an", "object"]).status_code
# cert-fix-G regressions: the UI's light/dark mode field, unknown keys, legacy unvalidated rows
r = op.put("/api/auth/me/theme", json={"accent_color": "#ef4444", "sidebar_style": "dark", "mode": "light"}); T["mode_put"] = [r.status_code, body(r)]
T["mode_get"] = body(login("cert-g-operator").get("/api/auth/me/theme"))
T["unknown_key_put"] = op.put("/api/auth/me/theme", json={"accent_color": "#ef4444", "css": "x"}).status_code
T["bad_mode_put"] = op.put("/api/auth/me/theme", json={"mode": "sepia"}).status_code
sql("UPDATE users SET theme_json=:t WHERE id=:i", t=json.dumps({"accent_color": "red;background:url(https://evil.example/x)"}), i=op_id)
T["legacy_invalid_get"] = body(op.get("/api/auth/me/theme"))
sql("UPDATE users SET theme_json=:t WHERE id=:i", t=json.dumps({"accent_color": "#123456", "mode": "light", "css": "x", "sidebar_style": 42}), i=op_id)
T["legacy_mixed_get"] = body(op.get("/api/auth/me/theme"))

# ---- fleet data (sidebar derives counts from GET /api/printers) ----
now = datetime.now(timezone.utc).replace(tzinfo=None)
spec = [("Fleet Printing", "RUNNING", now, 150), ("Fleet Idle", "IDLE", now, 85), ("Fleet Offline", "OFFLINE", now - timedelta(hours=2), 10),
        ("Fleet Stale", "IDLE", now - timedelta(days=3), 0), ("Fleet Paused", "PAUSE", now, 0)]
for name, state, seen, hours in spec:
    r = a.post("/api/printers", json={"name": name, "model": "X1C", "api_type": "bambu", "slot_count": 1}); pid = r.json()["id"]
    sql("UPDATE printers SET gcode_state=:s, last_seen=:t, total_print_hours=:h WHERE id=:i", s=state, t=seen, h=hours, i=pid)
OUT["printers_viewer"] = [{"name": p["name"], "gcode_state": p["gcode_state"], "last_seen": p["last_seen"]} for p in vw.get("/api/printers?active_only=false").json()]

# ---- maintenance ----
a.post("/api/maintenance/tasks", json={"name": "Hours task", "interval_print_hours": 100})
OUT["maintenance"] = body(vw.get("/api/maintenance/status"))

# ---- search: six types ----
fid = sql("INSERT INTO filament_library(brand,name,material) VALUES('Zephyrine','Matte','PLA') RETURNING id")[0][0]
sql("INSERT INTO spools(filament_id,vendor) VALUES(:f,'Zephyrine Co')", f=fid)
sql("INSERT INTO models(name) VALUES('Zephyrine hinge')")
sql("INSERT INTO jobs(item_name,status) VALUES('Zephyrine hinge x4','pending')")
a.post("/api/orders", json={"order_number": "ZEPHYRINE-77", "customer_name": "Cert"})
a.post("/api/products", json={"name": "Zephyrine kit", "sku": "ZK-1"})
a.post("/api/printers", json={"name": "Zephyrine Bay 1", "model": "X1C", "api_type": "bambu", "slot_count": 1})
OUT["search_admin"] = body(a.get("/api/search?q=zephyrine"))
OUT["search_viewer"] = body(vw.get("/api/search?q=ZEPHYRINE"))
OUT["search_short"] = body(a.get("/api/search?q=z"))
OUT["search_anon"] = client().get("/api/search?q=zephyrine").status_code

# ---- language ----
L = OUT["language"] = {"default": body(vw.get("/api/settings/language"))}
L["puts"] = {lang: a.put("/api/settings/language", json={"language": lang}).status_code for lang in ["de", "ja", "es", "it", "en"]}
a.put("/api/settings/language", json={"language": "ja"})
L["persisted_new_session"] = body(login("cert-g-viewer").get("/api/settings/language"))
L["unsupported"] = a.put("/api/settings/language", json={"language": "fr"}).status_code
L["nonadmin"] = op.put("/api/settings/language", json={"language": "de"}).status_code

# ---- ui_mode ----
U = OUT["ui_mode"] = {"default": vw.get("/api/pricing-config").json().get("ui_mode")}
U["put_simple"] = a.put("/api/pricing-config", json={"ui_mode": "simple"}).status_code
U["after_simple_other_session"] = login("cert-g-viewer").get("/api/pricing-config").json().get("ui_mode")
cfg_before = a.get("/api/pricing-config").json()
U["put_advanced"] = a.put("/api/pricing-config", json={"ui_mode": "advanced"}).status_code
cfg_after = a.get("/api/pricing-config").json()
U["after_advanced"] = cfg_after.get("ui_mode")
U["other_keys_preserved"] = {k: v for k, v in cfg_before.items() if k != "ui_mode"} == {k: v for k, v in cfg_after.items() if k != "ui_mode"}
U["nonadmin_put"] = op.put("/api/pricing-config", json={"ui_mode": "simple"}).status_code

# ---- admin logs ----
G = OUT["logs"] = {}
G["access"] = {"operator": op.get("/api/admin/logs").status_code, "viewer": vw.get("/api/admin/logs").status_code,
               "anon": client().get("/api/admin/logs").status_code, "admin": a.get("/api/admin/logs").status_code,
               "stream_operator": op.get("/api/admin/logs/stream").status_code,
               "bundle_operator": op.get("/api/admin/support-bundle").status_code,
               "bundle_viewer": vw.get("/api/admin/support-bundle").status_code}
G["source_ids"] = sorted(routes_admin._LOG_FILES)
G["per_source"] = {s: a.get(f"/api/admin/logs?source={s}").json()["lines"] for s in routes_admin._LOG_FILES}
G["levels"] = {lvl: a.get(f"/api/admin/logs?source=mqtt&level={lvl}").json()["lines"] for lvl in ["ERROR", "WARNING", "INFO", "DEBUG", "error"]}
G["backend_error_level"] = a.get("/api/admin/logs?source=backend&level=ERROR").json()["lines"]
# Other real formats: Python logging without brackets, supervisord, go2rtc (zerolog INF/WRN/ERR)
with Path(routes_admin._LOG_FILES["supervisord"]).open("w") as f:
    f.write("2026-10-14 10:00:00,000 ERROR odin.api: printer connection refused while fetching info\n")
    f.write("2026-10-14 10:00:01,000 INFO spawned: 'backend' with pid 7\n")
    f.write("10:00:02.000 WRN go2rtc stream warning about an ERROR counter\n")
    f.write("10:00:03.000 ERR go2rtc stream failed\n")
    f.write("    continuation line mentioning ERROR without a level\n")
G["formats"] = {lvl: a.get(f"/api/admin/logs?source=supervisord&level={lvl}").json()["lines"] for lvl in ["ERROR", "WARNING", "INFO"]}
G["search"] = a.get("/api/admin/logs?source=moonraker&search=LINE%203").json()["lines"]
G["search_level"] = a.get("/api/admin/logs?source=backend&level=ERROR&search=odin.auth").json()["lines"]
G["backend_all_lines"] = a.get("/api/admin/logs?source=backend&lines=5000").json()["lines"]
pages = []; cursor = None
for _ in range(10):
    q = "/api/admin/logs?source=go2rtc&lines=2" + (f"&cursor={cursor}" if cursor else "")
    d = a.get(q).json(); pages.append(d["lines"]); cursor = d["next_cursor"]
    if not cursor: break
G["pages"] = pages
with Path(routes_admin._LOG_FILES["go2rtc"]).open("a") as f: f.write("2026-10-14 11:00:00 [INFO] appended after snapshot\n")
G["bad_cursor"] = a.get("/api/admin/logs?source=go2rtc&cursor=garbage").status_code

# SSE live tail
def _appender():
    time.sleep(1.5)
    with Path(routes_admin._LOG_FILES["vision"]).open("a") as f:
        f.write("2026-10-14 12:00:00 [INFO] vision live-one\n"); f.flush()
        f.write("2026-10-14 12:00:01 [ERROR] vision live-two at 10.9.8.7\n"); f.flush()
        f.write("2026-10-14 12:00:02 [ERROR] vision live-three\n"); f.flush()
import uvicorn, httpx
srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, lifespan="off", log_level="warning"))
threading.Thread(target=srv.run, daemon=True).start()
for _ in range(200):
    if srv.started: break
    time.sleep(0.05)
port = srv.servers[0].sockets[0].getsockname()[1]
events = []; sse_meta = {}
def _reader():
    try:
        with httpx.Client(timeout=httpx.Timeout(60.0)) as hc:
            with hc.stream("GET", f"http://127.0.0.1:{port}/api/admin/logs/stream?source=vision&level=ERROR",
                           headers={"Authorization": a.headers["Authorization"]}) as r:
                sse_meta["status"] = r.status_code; sse_meta["type"] = r.headers.get("content-type")
                for line in r.iter_lines():
                    if line.startswith("data: "):
                        events.append(json.loads(line[6:]))
    except Exception as e:
        sse_meta["error"] = repr(e)
threading.Thread(target=_reader, daemon=True).start()
for _ in range(100):
    if "status" in sse_meta or "error" in sse_meta: break
    time.sleep(0.1)
threading.Thread(target=_appender, daemon=True).start()
deadline = time.time() + 45
while len(events) < 2 and time.time() < deadline:
    time.sleep(0.2)
G["sse_status"] = sse_meta.get("status"); G["sse_type"] = sse_meta.get("type"); G["sse_error"] = sse_meta.get("error")
G["sse_events"] = events
G["sse_unknown_source"] = a.get("/api/admin/logs/stream?source=nope").status_code

# ---- support bundle ----
sql("INSERT INTO system_config(key,value) VALUES('spoolman_url', :v)", v=json.dumps("http://" + S["ipv4"] + ":7912"))
sql("INSERT INTO system_config(key,value) VALUES('mqtt_republish_host', :v)", v=json.dumps(S["ipv6"]))
sql("INSERT INTO system_config(key,value) VALUES('mqtt_republish_password', :v)", v=json.dumps(S["password_label"]))
sql("INSERT INTO system_config(key,value) VALUES('camera_default_stream', :v)", v=json.dumps("rtsp://admin:" + S["url_credentials"] + "@cam.local/s"))
sql("INSERT INTO system_config(key,value) VALUES('alert_relay', :v)", v=json.dumps({"smtp_password": S["prefixed_password_label"], "jwt": S["jwt"], "relay_token": S["odin_token"]}))
r = a.get("/api/admin/support-bundle")
B = OUT["bundle"] = {"status": r.status_code, "content_type": r.headers.get("content-type"),
                     "disposition": r.headers.get("content-disposition"), "streaming": "content-length" not in r.headers}
zf = zipfile.ZipFile(io.BytesIO(r.content))
B["members"] = zf.namelist()
B["files"] = {n: zf.read(n).decode() for n in zf.namelist()}
B["db_path_configured"] = os.environ["DATABASE_URL"]
'''


@pytest.fixture(scope="module")
def ux(tmp_path_factory):
    return run_probe("ux-backend", PROBE, tmp_path_factory.mktemp("cert-g-ux"), {"CERT_G_SECRETS": json.dumps(SECRETS)}, timeout=240)


def test_no_network_attempts(ux):
    assert ux["_net_attempts"] == []


# ---------- F16-16.1-03 / 16.1-04 theme ----------
def test_theme_default_and_put_get_roundtrip(ux):
    t = ux["theme"]
    assert t["default"] == {"accent_color": "#6366f1", "sidebar_style": "dark", "background": "default"}
    assert t["put"] == [200, {"accent_color": "#0ea5e9", "sidebar_style": "light", "background": "default"}]
    assert json.loads(t["stored_column"])["accent_color"] == "#0ea5e9"


def test_theme_persists_across_devices_and_is_per_user(ux):
    t = ux["theme"]
    assert t["other_device"]["accent_color"] == "#0ea5e9" and t["other_device"]["sidebar_style"] == "light"
    assert t["viewer_after_operator_put"]["accent_color"] == "#0ea5e9"
    assert t["operator"]["accent_color"] == "#ef4444"
    assert t["anon"] == 401


def test_theme_put_validates_accent_and_sidebar(ux):
    assert ux["theme"]["invalid_put"] == 422 and ux["theme"]["non_object_put"] == 422


def test_theme_mode_roundtrip_and_strict_schema(ux):
    t = ux["theme"]
    assert t["mode_put"] == [200, {"accent_color": "#ef4444", "sidebar_style": "dark", "mode": "light"}]
    assert t["mode_get"] == {"accent_color": "#ef4444", "sidebar_style": "dark", "background": "default", "mode": "light"}
    assert t["unknown_key_put"] == 422 and t["bad_mode_put"] == 422
    # A row stored before validation existed is never echoed to the UI.
    assert t["legacy_invalid_get"] == {"accent_color": "#6366f1", "sidebar_style": "dark", "background": "default"}
    # Reads drop unknown/invalid legacy fields but keep the valid ones (writes stay strict).
    assert t["legacy_mixed_get"] == {"accent_color": "#123456", "sidebar_style": "dark", "background": "default", "mode": "light"}


# ---------- F16-16.2-02 fleet data ----------
def test_printer_list_supplies_state_and_last_seen_for_fleet_counts(ux):
    rows = {p["name"]: p for p in ux["printers_viewer"]}
    assert rows["Fleet Printing"]["gcode_state"] == "RUNNING"
    assert rows["Fleet Idle"]["gcode_state"] == "IDLE"
    assert rows["Fleet Offline"]["gcode_state"] == "OFFLINE"
    assert rows["Fleet Paused"]["gcode_state"] == "PAUSE"
    assert all(rows[n]["last_seen"] for n in rows if n.startswith("Fleet"))


# ---------- F16-16.2-03 search ----------
@pytest.mark.parametrize("category,singular", [("printers", "printer"), ("jobs", "job"), ("models", "model"),
                                               ("spools", "spool"), ("orders", "order"), ("products", "product")])
def test_global_search_finds_each_type(ux, category, singular):
    for key in ("search_admin", "search_viewer"):
        items = ux[key][category]
        assert len(items) == 1 and items[0]["type"] == singular and "zephyrine" in items[0]["name"].lower(), (key, items)


def test_search_requires_two_chars_and_auth(ux):
    assert all(v == [] for v in ux["search_short"].values())
    assert ux["search_anon"] == 401


# ---------- F16-16.5-01 language ----------
def test_language_settings_endpoint(ux):
    L = ux["language"]
    assert L["default"] == {"language": "en"}
    assert L["puts"] == {"de": 200, "ja": 200, "es": 200, "it": 200, "en": 200}
    assert L["persisted_new_session"] == {"language": "ja"}
    assert L["unsupported"] == 400 and L["nonadmin"] == 403


# ---------- F16-16.6-02 maintenance ----------
def test_maintenance_status_progress_and_overdue(ux):
    by = {p["printer_name"]: p for p in ux["maintenance"]}
    t = by["Fleet Printing"]["tasks"][0]
    assert t["status"] == "overdue" and t["progress_percent"] == 150.0 and by["Fleet Printing"]["overall_status"] == "overdue"
    t = by["Fleet Idle"]["tasks"][0]
    assert t["status"] == "due_soon" and t["progress_percent"] == 85.0
    assert by["Fleet Offline"]["tasks"][0]["status"] == "ok"


# ---------- F16-16.7-02 ui_mode ----------
def test_ui_mode_persisted_system_wide(ux):
    u = ux["ui_mode"]
    assert u["put_simple"] == 200 and u["after_simple_other_session"] == "simple"
    assert u["put_advanced"] == 200 and u["after_advanced"] == "advanced"
    assert u["other_keys_preserved"] is True
    assert u["nonadmin_put"] == 403


# ---------- F16-16.8 logs ----------
def test_logs_admin_only(ux):
    acc = ux["logs"]["access"]
    assert acc["admin"] == 200
    assert acc["operator"] == 403 and acc["viewer"] == 403 and acc["anon"] == 401
    assert acc["stream_operator"] == 403


CLAIMED_SOURCES = ["backend", "mqtt", "moonraker", "prusalink", "elegoo", "go2rtc", "supervisord", "timelapse", "vision"]


@pytest.mark.parametrize("source", CLAIMED_SOURCES)
def test_each_claimed_log_source_serves_its_file(ux, source):
    lines = ux["logs"]["per_source"][source]
    assert lines and all(f"{source}-marker" in l for l in lines[:6])


def test_backend_exposes_ten_sources_not_nine(ux):
    """Mismatch record: backend accepts the 9 claimed sources plus 'reports' (report_runner.log)."""
    assert ux["logs"]["source_ids"] == sorted(CLAIMED_SOURCES + ["reports"])


@pytest.mark.parametrize("level", ["ERROR", "WARNING", "INFO", "DEBUG"])
def test_level_filter_returns_that_level(ux, level):
    lines = ux["logs"]["levels"][level]
    assert lines and all(f"[{level}]" in l for l in lines)
    assert ux["logs"]["levels"]["error"] == ux["logs"]["levels"]["ERROR"]


def test_error_level_filter_excludes_info_lines_mentioning_error(ux):
    assert not any("[INFO]" in l for l in ux["logs"]["backend_error_level"])


def test_level_filter_reads_the_level_token_of_each_format(ux):
    f = ux["logs"]["formats"]
    assert f["ERROR"] == ["2026-10-14 10:00:00,000 ERROR odin.api: printer connection refused while fetching info",
                          "10:00:03.000 ERR go2rtc stream failed"]
    assert f["WARNING"] == ["10:00:02.000 WRN go2rtc stream warning about an ERROR counter"]
    assert f["INFO"] == ["2026-10-14 10:00:01,000 INFO spawned: 'backend' with pid 7"]


def test_text_search_case_insensitive_and_combines_with_level(ux):
    assert ux["logs"]["search"] == ["2026-10-14 10:00:03 [DEBUG] odin.moonraker: moonraker-marker line 3"]
    s = ux["logs"]["search_level"]
    assert len(s) == 1 and "odin.auth" in s[0] and "[ERROR]" in s[0]


def test_static_pagination_walks_all_lines_once(ux):
    pages = ux["logs"]["pages"]
    assert [len(p) for p in pages] == [2, 2, 2]
    flat = [l for p in reversed(pages) for l in p]
    assert flat == [f"2026-10-14 10:00:{i:02d} [{lvl}] odin.go2rtc: go2rtc-marker line {i}"
                    for i, lvl in enumerate(["INFO", "WARNING", "ERROR", "DEBUG", "INFO", "INFO"])]
    assert ux["logs"]["bad_cursor"] == 400


def test_sse_stream_delivers_new_lines_live_filtered_and_sanitized(ux):
    g = ux["logs"]
    assert g["sse_status"] == 200 and g["sse_type"].startswith("text/event-stream")
    assert len(g["sse_events"]) == 2
    assert "live-two" in g["sse_events"][0] and "10.9.8.7" not in g["sse_events"][0]
    assert "live-three" in g["sse_events"][1]
    assert g["sse_unknown_source"] == 404


def test_static_log_lines_are_sanitized(ux):
    text = "\n".join(ux["logs"]["backend_all_lines"])
    for k in ("ipv4", "password_label", "jwt", "odin_token", "url_credentials"):
        assert SECRETS[k] not in text, k


# ---------- F16-16.9 support bundle ----------
SIX = {"system_info.json", "connectivity.json", "settings_safe.json", "recent_errors.txt", "db_health.json", "bundle_metadata.json"}


def test_bundle_admin_only(ux):
    assert ux["logs"]["access"]["bundle_operator"] == 403 and ux["logs"]["access"]["bundle_viewer"] == 403


def test_bundle_is_streamed_zip_attachment(ux):
    b = ux["bundle"]
    assert b["status"] == 200 and b["content_type"] == "application/zip"
    assert b["disposition"].startswith("attachment; filename=odin-support-bundle-") and b["disposition"].endswith(".zip")
    assert b["streaming"] is True


def test_bundle_contains_exactly_the_six_files(ux):
    b = ux["bundle"]
    assert set(b["members"]) == SIX
    for name in SIX - {"recent_errors.txt"}:
        json.loads(b["files"][name])
    info = json.loads(b["files"]["system_info.json"])
    assert info["printer_count"] == 6 and info["user_count"] == 3
    assert {p["name"] for p in json.loads(b["files"]["connectivity.json"])} >= {"Fleet Printing", "Fleet Offline"}
    assert "[ERROR]" in b["files"]["recent_errors.txt"]
    assert set(json.loads(b["files"]["bundle_metadata.json"])) >= {"generated_at", "odin_version"}


def test_bundle_db_health_reports_the_configured_database(ux):
    health = json.loads(ux["bundle"]["files"]["db_health.json"])
    assert health.get("integrity") == "ok" and health["table_row_counts"]["printers"] == 6


@pytest.mark.parametrize("kind", ["ipv4", "password_label", "jwt", "odin_token", "url_credentials"])
def test_bundle_redacts_secret_in_every_file(ux, kind):
    for name, content in ux["bundle"]["files"].items():
        assert SECRETS[kind] not in content, name
    assert "[REDACTED]" in ux["bundle"]["files"]["recent_errors.txt"]


@pytest.mark.parametrize("kind", ["ipv6", "prefixed_password_label"])
def test_bundle_redacts_ipv6_and_prefixed_secret_labels(ux, kind):
    for name, content in ux["bundle"]["files"].items():
        assert SECRETS[kind] not in content, name
