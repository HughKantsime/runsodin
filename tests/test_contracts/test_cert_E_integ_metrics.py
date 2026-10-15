"""Cert-E integ: F14 14.2 Prometheus metrics.

Printer telemetry enters through the shipped Bambu monitor from a captured LAN report
(printer socket simulated, test_cert_A_harness); the real /metrics route is scraped with a
real scoped API token and parsed by the reference prometheus_client text parser. The
mounted path is checked on the real create_app() in an isolated process.
"""
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from prometheus_client.parser import text_string_to_metric_families
from sqlalchemy import text

from tests.test_contracts.test_api_token_delegation_behavior import token_client  # noqa: F401
from tests.test_contracts.test_cert_A_harness import (  # noqa: F401
    broker_socket, fernet_env, load_frames, replay, review_db, wire_monitor,
)

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def scrape(token_client):  # noqa: F811
    from modules.system.routes_config import router
    client, issue = token_client
    client.app.include_router(router)
    headers = issue(["read:analytics"])

    def get():
        response = client.get("/metrics", headers=headers)
        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith("text/plain; version=0.0.4")
        families = list(text_string_to_metric_families(response.text))
        return response, families
    return get


def _samples(families, name, printer_id=None):
    return [s for f in families for s in f.samples
            if s.name == name and (printer_id is None or s.labels.get("printer_id") == str(printer_id))]


def _frames_running():
    frames = load_frames("bambu-a1-happy-path")
    state = {}
    for index, (_, payload) in enumerate(frames):
        state.update(payload.get("print", {}))
        if state.get("gcode_state") == "RUNNING" and (state.get("mc_percent") or 0) >= 10:
            return frames[: index + 1], state["mc_percent"]
    raise AssertionError("capture never reached 10% while RUNNING")


def test_live_bambu_temperatures_and_fleet_state_are_exposed(wire_monitor, scrape, review_db):
    monitor, sock, clock, _ = wire_monitor
    replay(sock, clock, _frames_running()[0])
    review_db.expire_all()
    row = review_db.execute(text("SELECT nozzle_temp,bed_temp,gcode_state FROM printers WHERE id=301")).one()
    assert row.gcode_state == "RUNNING" and row.nozzle_temp and row.bed_temp
    _, families = scrape()
    nozzle = _samples(families, "odin_printer_nozzle_temp_celsius", 301)
    bed = _samples(families, "odin_printer_bed_temp_celsius", 301)
    assert [s.value for s in nozzle] == [pytest.approx(row.nozzle_temp)]
    assert [s.value for s in bed] == [pytest.approx(row.bed_temp)]
    assert nozzle[0].labels["printer"] == "Cert Bambu"
    assert _samples(families, "odin_printers_printing")[0].value >= 1
    assert _samples(families, "odin_printers_online")[0].value >= 1
    assert _samples(families, "odin_printer_print_hours_total", 301)
    assert _samples(families, "odin_printer_print_count_total", 301)


@pytest.mark.xfail(strict=True, reason=(
    "DEFECT: backend/modules/system/routes_config.py:436 reads printers.print_progress, a column the "
    "printers table does not have (progress lives on print_jobs.progress_percent); a printer RUNNING at "
    ">=10% per its live report never emits odin_printer_progress, although HELP/TYPE advertise it"))
def test_live_print_progress_is_exposed(wire_monitor, scrape, review_db):
    monitor, sock, clock, _ = wire_monitor
    frames, expected = _frames_running()
    replay(sock, clock, frames)
    _, families = scrape()
    progress = _samples(families, "odin_printer_progress", 301)
    assert [s.value for s in progress] == [expected]


def test_printers_table_has_no_print_progress_column(review_db):
    """Supports the DEFECT above with the real bootstrapped schema."""
    cols = {r[1] for r in review_db.execute(text("PRAGMA table_info(printers)"))}
    assert "print_progress" not in cols and {"nozzle_temp", "bed_temp"} <= cols


EXPECTED_FAMILIES = {
    "odin_printers_total", "odin_printers_online", "odin_printers_printing", "odin_printers_idle",
    "odin_printers_error", "odin_printer_nozzle_temp_celsius", "odin_printer_bed_temp_celsius",
    "odin_printer_progress", "odin_printer_print_hours", "odin_printer_print_count",
    "odin_jobs_by_status", "odin_queue_depth", "odin_spools_total", "odin_spools_low",
    "odin_orders_by_status", "odin_alerts_unread",
}


def test_exposition_family_inventory_has_no_system_health_metrics(scrape, review_db):
    """'System health' is not exposed: no process/DB/monitor/uptime/version family exists."""
    review_db.execute(text("INSERT INTO jobs(item_name,status) VALUES('Synthetic metrics job','pending')"))
    review_db.commit()
    _, families = scrape()
    names = {f.name.removesuffix("_total") for f in families}  # parser naming of empty vs populated counters differs
    expected = {n.removesuffix("_total") for n in EXPECTED_FAMILIES}
    assert names == expected, names ^ expected
    assert _samples(families, "odin_jobs_by_status")
    health_words = ("up", "health", "uptime", "database", "db_", "process", "monitor", "version", "build_info")
    assert not [n for n in names if any(w in n.removeprefix("odin_") for w in health_words)]


PROBE = textwrap.dedent('''
import json, os, secrets
from pathlib import Path
from sqlalchemy import create_engine, text
from core.schema import bootstrap_database
engine = create_engine(os.environ['DATABASE_URL'])
bootstrap_database(engine, Path('.'))
from core.auth import hash_password
raw = 'odin_' + secrets.token_urlsafe(32)
with engine.begin() as c:
    c.execute(text("INSERT INTO users(id,username,email,password_hash,role,is_active) VALUES(1,'synthetic-admin','a@example.test','x','admin',1)"))
    c.execute(text("INSERT INTO api_tokens(user_id,name,token_hash,token_prefix,scopes) VALUES(1,'Synthetic scrape',:h,:p,:s)"),
              {'h': hash_password(raw), 'p': raw[:10], 's': json.dumps(['read:analytics'])})
    c.execute(text("INSERT INTO printers(id,name,model,is_active,api_type,nozzle_temp,bed_temp,last_seen,gcode_state) VALUES(5,'Synthetic scrape printer','X1C',1,'bambu',215.5,60.0,CURRENT_TIMESTAMP,'RUNNING')"))
from fastapi.testclient import TestClient
from core.app import create_app
client = TestClient(create_app())
out = {}
for path in ('/metrics', '/api/metrics', '/api/v1/metrics'):
    r = client.get(path, headers={'X-API-Key': raw}, follow_redirects=False)
    out[path] = {'status': r.status_code, 'type': r.headers.get('content-type', ''), 'body': r.text[:20000]}
for path in ('/api/docs', '/api/redoc'):
    first = client.get(path, follow_redirects=False)
    page = client.get(path)
    out[path] = {'status': first.status_code, 'location': first.headers.get('location'),
                 'final_status': page.status_code, 'final_url': str(page.url), 'body': page.text[:5000]}
print('CERTE-METRICS ' + json.dumps(out))
''')


@pytest.fixture(scope="module")
def mounted(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("certe-metrics-app")
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{tmp}/m.db", "JWT_SECRET_KEY": "certe-metrics-test-only",
           "PYTHONPATH": str(ROOT / "backend")}
    env.pop("API_KEY", None)
    result = subprocess.run([sys.executable, "-c", PROBE], cwd=ROOT / "backend", env=env,
                            capture_output=True, text=True, timeout=240)
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-3000:]
    line = [l for l in result.stdout.splitlines() if l.startswith("CERTE-METRICS ")][-1]
    return json.loads(line[len("CERTE-METRICS "):])


@pytest.mark.parametrize("path", ["/api/metrics", "/api/v1/metrics"])
def test_real_app_serves_prometheus_exposition_under_api_prefix(mounted, path):
    r = mounted[path]
    assert r["status"] == 200 and r["type"].startswith("text/plain; version=0.0.4")
    families = list(text_string_to_metric_families(r["body"]))
    assert [s.value for s in _samples(families, "odin_printer_nozzle_temp_celsius", 5)] == [215.5]
    assert _samples(families, "odin_printers_total")[0].value == 1


def test_real_app_root_metrics_path_is_not_the_prometheus_endpoint(mounted):
    """The advertised bare `/metrics` path is not the exposition on the shipped app."""
    r = mounted["/metrics"]
    assert not (r["status"] == 200 and r["type"].startswith("text/plain; version=0.0.4")), r["status"]
    assert "odin_printers_total" not in r["body"]


def test_real_app_swagger_ui_at_api_docs(mounted):
    r = mounted["/api/docs"]
    assert r["status"] in (307, 308) and r["location"].endswith("/api/v1/docs")
    assert r["final_status"] == 200 and "swagger-ui" in r["body"].lower()
    assert "/openapi.json" in r["body"]


def test_real_app_redoc_at_api_redoc(mounted):
    r = mounted["/api/redoc"]
    assert r["status"] in (307, 308) and r["location"].endswith("/api/v1/redoc")
    assert r["final_status"] == 200
    body = r["body"].lower()
    assert "<redoc" in body and "redoc.standalone.js" in body and "/openapi.json" in body
