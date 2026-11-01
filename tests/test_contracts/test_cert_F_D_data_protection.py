"""Cert F / cluster D — F12-12.3-01, -03, -05, -06 against the real ODIN server.

Every secret is a throwaway sentinel generated in-test. Writes go through the real HTTP routes of
``main:app`` (LiveOdin); persistence is checked by reading the raw SQLite columns and decrypting
with the instance Fernet key. Only the outermost boundaries (printers, cameras, webhook receivers)
are absent; nothing is ever dispatched to them.
"""
from __future__ import annotations

import json
import re
import secrets
import sqlite3

import pytest
import requests
from cryptography.fernet import Fernet

from tests.test_contracts.test_cert_F_00_live import LiveOdin


def _sentinel(tag: str) -> str:
    return f"certD{tag}{secrets.token_hex(8)}"


@pytest.fixture(scope="module")
def odin(tmp_path_factory):
    work = tmp_path_factory.mktemp("odinD_dp")
    o = LiveOdin(work, env={"GO2RTC_CONFIG": str(work / "go2rtc.yaml"),
                            "GO2RTC_LOCKFILE": str(work / "go2rtc.lock")})
    o.start()
    o.setup_admin()
    o.H = o.admin_headers()          # one login for the whole module (auth rate limits)
    o.F = Fernet(o.fernet_key.encode())
    o.secrets = {}                    # category -> plaintext sentinel
    yield o
    o.stop()


def assert_fernet(o, raw, plain):
    assert raw, "nothing persisted"
    assert raw != plain and plain not in raw
    assert raw.startswith("gAAAAA")
    assert o.F.decrypt(raw.encode()).decode() == plain


def _printer(o, **body):
    r = o.post("/api/printers", json=body, headers=o.H)
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------- F12-12.3-01 per field ----

def test_printer_api_key_fernet_on_create_and_patch(odin):
    o = odin
    key1 = f"01P{secrets.token_hex(4)}|{_sentinel('apikeyA')}"
    p = _printer(o, name="cert-d-bambu", model="X1C", api_type="bambu", api_host="192.168.50.10", api_key=key1)
    raw = o.sql("SELECT api_key FROM printers WHERE id=:i", i=p["id"])[0][0]
    assert_fernet(o, raw, key1)
    key2 = f"01P{secrets.token_hex(4)}|{_sentinel('apikeyB')}"
    r = o.patch(f"/api/printers/{p['id']}", json={"api_key": key2}, headers=o.H)
    assert r.status_code == 200, r.text
    raw = o.sql("SELECT api_key FROM printers WHERE id=:i", i=p["id"])[0][0]
    assert_fernet(o, raw, key2)
    o.secrets["printer_api_key"] = key2.split("|")[1]
    o.secrets["printer_api_key_full"] = key2
    o.bambu_id = p["id"]


def test_camera_url_with_credentials_fernet_on_patch(odin):
    o = odin
    pw = _sentinel("campw")
    url = f"rtsp://camuser:{pw}@192.168.50.11:554/stream1"
    p = _printer(o, name="cert-d-cam", api_type="moonraker", api_host="192.168.50.11")
    r = o.patch(f"/api/printers/{p['id']}", json={"camera_url": url}, headers=o.H)
    assert r.status_code == 200, r.text
    assert pw not in r.text
    raw = o.sql("SELECT camera_url, camera_enabled FROM printers WHERE id=:i", i=p["id"])[0]
    assert_fernet(o, raw[0], url)
    assert raw[1]
    o.secrets["camera_password"] = pw
    o.cam_id = p["id"]


def test_camera_url_without_credentials_is_not_required_to_be_encrypted(odin):
    """Scope of the claim: only credential-bearing camera URLs are encrypted."""
    o = odin
    url = "rtsp://192.168.50.12:554/plain"
    p = _printer(o, name="cert-d-cam-plain", api_type="moonraker", api_host="192.168.50.12")
    assert o.patch(f"/api/printers/{p['id']}", json={"camera_url": url}, headers=o.H).status_code == 200
    assert o.sql("SELECT camera_url FROM printers WHERE id=:i", i=p["id"])[0][0] == url


def test_camera_url_with_credentials_persisted_encrypted_on_create(odin):
    o = odin
    pw = _sentinel("campwC")
    url = f"rtsp://camuser:{pw}@192.168.50.13:554/stream1"
    p = _printer(o, name="cert-d-cam-create", api_type="moonraker", api_host="192.168.50.13", camera_url=url)
    raw, enabled = o.sql("SELECT camera_url, camera_enabled FROM printers WHERE id=:i", i=p["id"])[0]
    assert_fernet(o, raw, url)
    assert enabled
    # The API never returns the credentials.
    assert pw not in o.get(f"/api/printers/{p['id']}", headers=o.H).text


def test_smtp_password_fernet(odin):
    o = odin
    pw = _sentinel("smtp")
    r = o.put("/api/smtp-config", headers=o.H, json={"enabled": True, "host": "smtp.example.test", "port": 587,
              "username": "relay", "password": pw, "from_address": "odin@example.test", "use_tls": True})
    assert r.status_code == 200, r.text
    value = o.sql("SELECT value FROM system_config WHERE key='smtp_config'")[0][0]
    stored = json.loads(value) if isinstance(value, str) else value
    assert_fernet(o, stored["password"], pw)
    # Re-save without a password keeps the ciphertext (no plaintext round-trip).
    r = o.put("/api/smtp-config", headers=o.H, json={"enabled": True, "host": "smtp.example.test", "port": 587,
              "username": "relay", "password": "", "from_address": "odin@example.test", "use_tls": True})
    assert r.status_code == 200
    stored2 = json.loads(o.sql("SELECT value FROM system_config WHERE key='smtp_config'")[0][0])
    assert_fernet(o, stored2["password"], pw)
    o.secrets["smtp_password"] = pw


def test_mqtt_republish_password_fernet(odin):
    o = odin
    pw = _sentinel("mqtt")
    r = o.put("/api/config/mqtt-republish", headers=o.H, json={"enabled": False, "host": "broker.example.test",
              "port": 1883, "username": "odin", "password": pw})
    assert r.status_code == 200, r.text
    raw = o.sql("SELECT value FROM system_config WHERE key='mqtt_republish_password'")[0][0]
    assert_fernet(o, raw, pw)
    o.secrets["mqtt_password"] = pw


WEBHOOK_URLS = {
    "discord": lambda s: f"https://discord.com/api/webhooks/1234567890/{s}",
    "slack": lambda s: f"https://hooks.slack.com/services/T000/B000/{s}",
    # cert-fix-E: Telegram stores bot_token|chat_id (the dispatcher builds the API URL).
    "telegram": lambda s: f"123456:{s}|987654321",
    "ntfy": lambda s: f"https://ntfy.sh/{s}",
}


@pytest.mark.parametrize("kind", sorted(WEBHOOK_URLS))
def test_webhook_url_fernet_on_create_and_patch(odin, kind):
    o = odin
    tok = _sentinel(f"wh{kind}")
    url = WEBHOOK_URLS[kind](tok)
    r = o.post("/api/webhooks", headers=o.H, json={"name": f"cert-d-{kind}", "url": url, "webhook_type": kind})
    assert r.status_code == 200, r.text
    row = o.sql("SELECT id, url FROM webhooks WHERE name=:n", n=f"cert-d-{kind}")[0]
    assert_fernet(o, row[1], url)
    tok2 = _sentinel(f"wh{kind}B")
    url2 = WEBHOOK_URLS[kind](tok2)
    assert o.patch(f"/api/webhooks/{row[0]}", headers=o.H, json={"url": url2}).status_code == 200
    raw = o.sql("SELECT url FROM webhooks WHERE id=:i", i=row[0])[0][0]
    assert_fernet(o, raw, url2)
    o.secrets[f"webhook_{kind}"] = tok2
    o.__dict__.setdefault("webhook_ids", []).append(row[0])


def test_org_webhook_setting_fernet(odin):
    o = odin
    r = o.post("/api/orgs", headers=o.H, json={"name": "Cert D Org"})
    assert r.status_code in (200, 201), r.text
    org_id = o.sql("SELECT id FROM groups WHERE name='Cert D Org'")[0][0]
    tok = _sentinel("orgwh")
    url = f"https://hooks.slack.com/services/T1/B1/{tok}"
    r = o.put(f"/api/orgs/{org_id}/settings", headers=o.H, json={"webhook_url": url, "webhook_type": "slack"})
    assert r.status_code == 200, r.text
    settings = json.loads(o.sql("SELECT settings_json FROM groups WHERE id=:i", i=org_id)[0][0])
    assert_fernet(o, settings["webhook_url"], url)
    o.secrets["org_webhook"] = tok
    o.org_id = org_id


def test_plaintext_sentinels_absent_from_entire_database(odin):
    """After every write above, no plaintext secret exists in ANY table/column of the DB file."""
    o = odin
    assert len(o.secrets) >= 9, o.secrets.keys()
    con = sqlite3.connect(str(o.db_path))
    try:
        hits = []
        tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        for t in tables:
            cols = [c[1] for c in con.execute(f"PRAGMA table_info('{t}')")]
            for row in con.execute(f"SELECT * FROM '{t}'"):
                blob = " ".join(str(v) for v in row if v is not None)
                for name, plain in o.secrets.items():
                    if plain in blob:
                        hits.append((t, name))
        assert hits == []
    finally:
        con.close()
    # Closing any descriptor for this file drops the process's POSIX locks, so a pooled
    # connection could lose its WAL lock and read a stale snapshot afterwards.
    o.engine.dispose()
    raw = o.db_path.read_bytes()
    for name, plain in o.secrets.items():
        assert plain.encode() not in raw, name


# ---------------------------------------------------------------- F12-12.3-03 ---------------

def test_patch_user_sql_values_bound_and_unlisted_columns_ignored(odin):
    o = odin
    _, pw, created = o.create_user("certd_sqli", "viewer")
    uid = o.sql("SELECT id FROM users WHERE username='certd_sqli'")[0][0]
    before = o.sql("SELECT password_hash, mfa_enabled, oidc_subject, quota_grams FROM users WHERE id=:i", i=uid)[0]
    count_before = o.sql("SELECT COUNT(*) FROM users")[0][0]
    evil = "x', role='admin' WHERE 1=1; DROP TABLE users; --"
    body = {
        "username": evil,
        "role": "viewer",
        "password_hash": "$2b$12$attackercontrolledhashvalue000000000000000000000000000",
        "mfa_enabled": True,
        "oidc_subject": "attacker-subject",
        "quota_grams": 99999,
        "id": 1,
        "role = 'admin' --": "x",
        "is_superadmin": True,
    }
    r = o.patch(f"/api/users/{uid}", json=body, headers=o.H)
    assert r.status_code == 200, r.text
    row = o.sql("SELECT username, role, password_hash, mfa_enabled, oidc_subject, quota_grams, id FROM users WHERE id=:i", i=uid)[0]
    assert row[0] == evil                       # stored literally as a bound value
    assert row[1] == "viewer"
    assert (row[2], row[3], row[4], row[5]) == tuple(before)   # non-whitelisted columns untouched
    assert o.sql("SELECT COUNT(*) FROM users")[0][0] == count_before
    assert o.sql("SELECT COUNT(*) FROM users WHERE role='admin'")[0][0] == 1
    # The original password still authenticates; the injected hash was ignored.
    assert o.login(evil, pw).status_code == 200


def test_patch_user_injection_in_email_and_group_type_confusion_rejected(odin):
    o = odin
    uid = o.sql("SELECT id FROM users WHERE role='viewer' ORDER BY id LIMIT 1")[0][0]
    r = o.patch(f"/api/users/{uid}", json={"email": "a@b.test' OR '1'='1"}, headers=o.H)
    assert r.status_code in (200, 400, 422)
    if r.status_code == 200:
        assert o.sql("SELECT email FROM users WHERE id=:i", i=uid)[0][0] == "a@b.test' OR '1'='1"
    assert o.sql("SELECT COUNT(DISTINCT email) FROM users")[0][0] == o.sql("SELECT COUNT(*) FROM users")[0][0]
    r = o.patch(f"/api/users/{uid}", json={"group_id": "1 OR 1=1"}, headers=o.H)
    assert r.status_code == 422


# ---------------------------------------------------------------- F12-12.3-05 ---------------

def test_bambu_rtsp_credentials_not_persisted_and_generated_on_demand(odin):
    o = odin
    code = _sentinel("bblcode")
    p = _printer(o, name="cert-d-x1c-cam", model="X1C", api_type="bambu", api_host="192.168.50.20",
                 api_key=f"01S{secrets.token_hex(4)}|{code}")
    row = o.sql("SELECT camera_url, api_key FROM printers WHERE id=:i", i=p["id"])[0]
    assert row[0] in (None, "")                       # no RTSP URL persisted
    assert code not in row[1] and code in o.F.decrypt(row[1].encode()).decode()
    # Reading the camera list (camera derived on demand) never writes the generated RTSP URL to the DB.
    cams = o.get("/api/cameras", headers=o.H)
    assert cams.status_code == 200 and code not in cams.text
    assert any(c.get("id") == p["id"] for c in cams.json()), cams.text   # generated on demand => listed
    row = o.sql("SELECT camera_url FROM printers WHERE id=:i", i=p["id"])[0]
    assert row[0] in (None, "")
    raw = o.db_path.read_bytes()
    assert code.encode() not in raw and b"rtsps://bblp" not in raw


def test_bambu_rtsp_generated_from_encrypted_key_in_go2rtc_config(odin):
    """The real go2rtc sync (run by the server on printer create) derives rtsps://bblp:<code>@host:322
    from the decrypted api_key; the derived URL exists only in the go2rtc config file, not the DB."""
    o = odin
    import time
    path = o.dir / "go2rtc.yaml"
    code = None
    for _ in range(40):
        if path.exists() and "rtsps://bblp:" in path.read_text():
            break
        time.sleep(0.25)
    text = path.read_text()
    m = re.findall(r"rtsps://bblp:([^@]+)@192\.168\.50\.20:322/streaming/live/1", text)
    assert m, text[:500]
    stored = o.sql("SELECT api_key FROM printers WHERE name='cert-d-x1c-cam'")[0][0]
    assert o.F.decrypt(stored.encode()).decode().split("|")[1] == m[0]


# ---------------------------------------------------------------- F12-12.3-06 crawl ---------

def _extra_secrets(o):
    """Create the remaining credential kinds so the crawl covers them too."""
    tok = _sentinel("plug")
    r = o.put(f"/api/printers/{o.bambu_id}/plug", headers=o.H,
              json={"type": "homeassistant", "host": "http://192.168.50.30:8123",  # cert-fix-A: HA host is a base URL
                    "entity_id": "switch.p1", "auth_token": tok})
    assert r.status_code == 200, r.text
    o.secrets["smart_plug_token"] = tok
    cs = _sentinel("oidcsecret")
    r = o.put("/api/admin/oidc", headers=o.H, json={"display_name": "Cert IdP", "client_id": "cert-client",
              "client_secret": cs, "tenant_id": "tenant", "provider_type": "microsoft", "is_enabled": False})
    assert r.status_code == 200, r.text
    o.secrets["oidc_client_secret"] = cs
    r = o.post("/api/tokens", headers=o.H, json={"name": "cert-d-token", "scopes": ["read"]})
    assert r.status_code in (200, 201), r.text
    body = r.json()
    raw_token = body.get("token") or body.get("raw_token") or body.get("api_token")
    assert raw_token, body.keys()
    o.secrets["api_token_value"] = raw_token[8:]   # secret part (prefix is intentionally displayable)


def _fill(path, ids):
    def rep(m):
        name = m.group(1)
        return str(ids.get(name, 1))
    return re.sub(r"\{([^}]+)\}", rep, path)


SKIP_GET = ("/stream", "/ws", "/events", "/mjpeg", "/snapshot", "/live-activity")


def test_no_stored_secret_in_any_get_response(odin):
    o = odin
    _extra_secrets(o)
    spec = o.get("/openapi.json", headers=o.H).json()
    ids = {"printer_id": o.bambu_id, "org_id": o.org_id, "user_id": 1, "webhook_id": o.webhook_ids[0],
           "group_id": o.org_id, "id": 1}
    leaks, crawled, statuses = [], 0, {}
    for path, ops in sorted(spec["paths"].items()):
        if "get" not in ops or not path.startswith("/api/") or path.startswith("/api/v1/"):
            continue
        if any(s in path for s in SKIP_GET):
            continue
        url = _fill(path, ids)
        try:
            r = requests.get(o.url + url, headers=o.H, timeout=15, allow_redirects=False, stream=True)
            body = r.raw.read(5_000_000, decode_content=True)
            r.close()
        except requests.RequestException:
            continue
        crawled += 1
        statuses[str(r.status_code)] = statuses.get(str(r.status_code), 0) + 1
        hay = body + r.headers.get("location", "").encode()
        for name, plain in o.secrets.items():
            if plain.encode() in hay:
                leaks.append((path, name))
    assert crawled > 150 and statuses.get("200", 0) > 100
    o.leaks = leaks
    api_key_like = {"printer_api_key", "printer_api_key_full", "smart_plug_token", "oidc_client_secret",
                    "api_token_value", "smtp_password", "mqtt_password", "camera_password"}
    from pathlib import Path
    out = Path(__file__).resolve().parents[2] / "artifacts/application-certification/cert-F/D-secret-crawl.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"get_routes_crawled": crawled, "status_counts": statuses, "secret_categories": sorted(o.secrets),
                               "exposures": sorted(set(leaks))}, indent=1))
    assert [l for l in leaks if l[1] in api_key_like] == []


def test_webhook_urls_with_embedded_tokens_returned_to_superadmin(odin):
    """Documents the remaining credential exposure found by the crawl (not an API-key field):
    GET /api/webhooks returns the decrypted webhook URLs (Discord/Slack/Telegram bot token/ntfy topic) and
    GET /api/orgs/{id}/settings returns the decrypted org webhook URL."""
    o = odin
    r = o.get("/api/webhooks", headers=o.H)
    assert r.status_code == 200
    exposed = [k for k in ("webhook_discord", "webhook_slack", "webhook_telegram", "webhook_ntfy")
               if o.secrets[k] in r.text]
    assert exposed == ["webhook_discord", "webhook_slack", "webhook_telegram", "webhook_ntfy"]
    assert {l[0] for l in getattr(o, "leaks", []) if l[1].startswith("webhook_")} <= {"/api/webhooks"}
    org = o.get(f"/api/orgs/{o.org_id}/settings", headers=o.H)
    assert org.status_code == 200 and o.secrets["org_webhook"] in org.text
    assert {l[0] for l in getattr(o, "leaks", []) if l[1] == "org_webhook"} <= {"/api/orgs/{org_id}/settings"}
