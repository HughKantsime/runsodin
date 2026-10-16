"""Cert G: capture producer wire fixtures from the real backend for UI auditors.

Every fixture is the literal JSON body returned by a real route of
``core.app.create_app()`` (real login/JWT, real RBAC, real SQLite schema) in an
isolated subprocess. Files land in artifacts/application-certification/cert-G/fixtures/.
"""
import json

import pytest

from tests.test_contracts.test_cert_G_harness import CERT_DIR, run_probe

FIXTURES = CERT_DIR / "fixtures"

PROBE = r'''
from datetime import datetime, timezone, timedelta
from modules.system import routes_admin
logdir = WORK / "logs"; logdir.mkdir(exist_ok=True)
for src in list(routes_admin._LOG_FILES):
    p = logdir / (src + ".log")
    p.write_text("".join(f"2026-10-14 10:00:{i:02d} [{lvl}] odin.{src}: cert-G {src} line {i}\n"
                         for i, lvl in enumerate(["INFO", "WARNING", "ERROR", "DEBUG", "INFO"])))
    routes_admin._LOG_FILES[src] = str(p)

admin_id = seed_user("cert-g-admin", "admin")
install_license("pro")
a = login("cert-g-admin")
OUT["auth_me"] = body(a.get("/api/auth/me"))
put = a.put("/api/auth/me/theme", json={"accent_color": "#22c55e", "sidebar_style": "light", "background": "default"})
OUT["theme_put_status"] = put.status_code
OUT["auth_me_theme"] = body(a.get("/api/auth/me/theme"))

ids = {}
for name, model in [("CertG Printing X1C", "X1 Carbon"), ("CertG Idle P1S", "P1S"), ("CertG Offline MK4", "MK4"), ("CertG Stale A1", "A1")]:
    r = a.post("/api/printers", json={"name": name, "model": model, "api_type": "bambu", "slot_count": 1})
    assert r.status_code == 201, r.text
    ids[name] = r.json()["id"]
now = datetime.now(timezone.utc).replace(tzinfo=None)
sql("UPDATE printers SET gcode_state='RUNNING', last_seen=:t, total_print_hours=150 WHERE id=:i", t=now, i=ids["CertG Printing X1C"])
sql("UPDATE printers SET gcode_state='IDLE', last_seen=:t, total_print_hours=50 WHERE id=:i", t=now, i=ids["CertG Idle P1S"])
sql("UPDATE printers SET gcode_state='OFFLINE', last_seen=:t WHERE id=:i", t=now - timedelta(hours=3), i=ids["CertG Offline MK4"])
sql("UPDATE printers SET gcode_state='IDLE', last_seen=:t WHERE id=:i", t=now - timedelta(days=2), i=ids["CertG Stale A1"])
OUT["printers"] = body(a.get("/api/printers?active_only=false"))

t = a.post("/api/maintenance/tasks", json={"name": "CertG Nozzle clean", "interval_print_hours": 100})
OUT["maint_task_status"] = t.status_code
OUT["maintenance_status"] = body(a.get("/api/maintenance/status"))

# Six searchable types, one query
fid = sql("INSERT INTO filament_library(brand,name,material) VALUES('CertGalaxy','Galaxy Black','PLA') RETURNING id")[0][0]
sql("INSERT INTO spools(filament_id,vendor) VALUES(:f,'CertGalaxy vendor')", f=fid)
sql("INSERT INTO models(name) VALUES('CertGalaxy bracket')")
sql("INSERT INTO jobs(item_name,status) VALUES('CertGalaxy job','pending')")
sql("INSERT INTO orders(order_number,customer_name,status) VALUES('CertGalaxy-1001','Fixture','pending')")
sql("INSERT INTO products(name,sku) VALUES('CertGalaxy kit','CG-1')")
a.post("/api/printers", json={"name": "CertGalaxy Printer", "model": "X1C", "api_type": "bambu", "slot_count": 1})
OUT["search_CertGalaxy"] = body(a.get("/api/search?q=CertGalaxy"))

OUT["logs_page"] = body(a.get("/api/admin/logs?source=backend&lines=3"))
OUT["logs_sources"] = {src: body(a.get(f"/api/admin/logs?source={src}&lines=200")) for src in routes_admin._LOG_FILES}
OUT["logs_unknown_source"] = body(a.get("/api/admin/logs?source=nonexistent"))

OUT["license_status_pro"] = body(a.get("/api/license"))
install_license(None)
OUT["license_status_community"] = body(client().get("/api/license"))
install_license("pro")
a.put("/api/settings/language", json={"language": "de"})
OUT["settings_language"] = body(a.get("/api/settings/language"))
a.put("/api/pricing-config", json={"ui_mode": "simple"})
OUT["pricing_config"] = body(a.get("/api/pricing-config"))
OUT["permissions"] = body(a.get("/api/permissions"))
OUT["setup_status"] = body(client().get("/api/setup/status"))
'''

ROUTES = {
    "auth_me.json": "GET /api/auth/me (admin, password login JWT)",
    "auth_me_theme.json": "GET /api/auth/me/theme after PUT {accent_color:#22c55e, sidebar_style:light, background:default}",
    "printers.json": "GET /api/printers?active_only=false (4 printers: RUNNING fresh, IDLE fresh, OFFLINE, IDLE stale last_seen 2 days)",
    "maintenance_status.json": "GET /api/maintenance/status (task interval 100h; printer at 150h -> overdue, 50h -> ok)",
    "search_CertGalaxy.json": "GET /api/search?q=CertGalaxy (one match in each of six categories)",
    "logs_page.json": "GET /api/admin/logs?source=backend&lines=3 (first page; next_cursor set)",
    "logs_sources.json": "GET /api/admin/logs?source=<id> for every backend-accepted source id (frontend source list is static in LogViewer.tsx; no source-list endpoint exists)",
    "logs_unknown_source.json": "GET /api/admin/logs?source=nonexistent",
    "license_status_community.json": "GET /api/license (unauthenticated) with no license file",
    "license_status_pro.json": "GET /api/license with a signed Pro license installed",
    "settings_language.json": "GET /api/settings/language after PUT {language:de}",
    "pricing_config.json": "GET /api/pricing-config after PUT {ui_mode:simple}",
    "permissions.json": "GET /api/permissions (admin)",
    "setup_status.json": "GET /api/setup/status (unauthenticated, admin exists)",
}


@pytest.fixture(scope="module")
def wire(tmp_path_factory):
    return run_probe("wire-fixtures", PROBE, tmp_path_factory.mktemp("cert-g-wire"))


def test_capture_wire_fixtures(wire):
    FIXTURES.mkdir(parents=True, exist_ok=True)
    for filename in ROUTES:
        data = wire[filename[:-5]]
        (FIXTURES / filename).write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    lines = ["# Cert G producer wire fixtures", "",
             "Captured by tests/test_contracts/test_cert_G_wire_fixtures.py from the real",
             "`core.app.create_app()` (real password login, RBAC, bootstrapped SQLite schema,",
             "signed throwaway-key licenses, network blocked). Each file is the literal response body.", ""]
    lines += [f"- `{name}` — {route}" for name, route in ROUTES.items()]
    (FIXTURES / "README.md").write_text("\n".join(lines) + "\n")
    assert wire["_net_attempts"] == []


def test_fixture_shapes_are_meaningful(wire):
    assert wire["auth_me"]["role"] == "admin"
    assert wire["auth_me_theme"]["accent_color"] == "#22c55e"
    states = {p["name"]: p["gcode_state"] for p in wire["printers"]}
    assert states["CertG Printing X1C"] == "RUNNING" and states["CertG Offline MK4"] == "OFFLINE"
    assert all("last_seen" in p for p in wire["printers"])
    assert {k: len(v) for k, v in wire["search_CertGalaxy"].items()} == {k: 1 for k in ("models", "jobs", "spools", "printers", "orders", "products")}
    assert wire["license_status_pro"]["tier"] == "pro" and wire["license_status_community"]["tier"] == "community"
    assert wire["settings_language"] == {"language": "de"}
    assert wire["pricing_config"]["ui_mode"] == "simple"
    statuses = {p["printer_name"] if "printer_name" in p else p.get("name"): p for p in wire["maintenance_status"]}
    assert any(t["status"] == "overdue" for p in wire["maintenance_status"] for t in p["tasks"])
