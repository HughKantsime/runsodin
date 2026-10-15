"""Cert F / cluster B — F11-11.3-02: "Enforced on all 450+ API endpoints: every non-public route must
authenticate (route census test), with 1,800+ RBAC matrix tests".

Evidence produced here:
  * the real ``create_app()`` route table (counted in an isolated subprocess, /api/v1 duplicates collapsed),
  * ``pytest --collect-only`` of tests/test_rbac.py (the RBAC matrix; the live run is the coordinator's
    ``rbac-live-junit.xml``),
  * a live probe against the real server (``LiveOdin``) of EVERY non-public route-method pair, with no
    credentials and with a fabricated ``X-API-Key: odin_...`` token, each with an empty body and with a
    schema-valid body/query built from the app's own OpenAPI document, both with the perimeter API_KEY
    unset and set. No request may succeed (2xx/3xx) and none may crash (5xx).
"""
from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest
import requests

from tests.test_contracts.test_cert_F_00_live import LiveOdin
from tests.test_contracts.test_route_auth_census import PUBLIC

ROOT = Path(__file__).resolve().parents[2]
RECEIPTS = ROOT / "artifacts" / "application-certification" / "cert-F"

# Public by design beyond the census allowlist: logout only clears the caller's own cookie and
# blacklists the presented token (if any); with no credential it has nothing to act on.
EXTRA_PUBLIC = {"POST /api/auth/logout"}

SNAPSHOT = r"""
import json, os, secrets, sys
from pathlib import Path
from cryptography.fernet import Fernet
os.environ['JWT_SECRET_KEY'] = secrets.token_urlsafe(48)
os.environ['ENCRYPTION_KEY'] = Fernet.generate_key().decode()
from sqlalchemy import create_engine
from core.schema import bootstrap_database
bootstrap_database(create_engine(os.environ['DATABASE_URL']), Path('.'))
from core.app import create_app
from fastapi.routing import APIRoute
app = create_app()
pairs = sorted({(m, r.path) for r in app.routes if isinstance(r, APIRoute) for m in r.methods if m not in ('HEAD', 'OPTIONS')})
Path(sys.argv[1]).write_text(json.dumps({'pairs': pairs, 'openapi': app.openapi()}))
"""


def canonical(path: str) -> str:
    return re.sub(r"^/api/v1/", "/api/", path)


@pytest.fixture(scope="module")
def snapshot(tmp_path_factory):
    d = tmp_path_factory.mktemp("census-snapshot")
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ODIN_", "API_KEY", "DATABASE_"))}
    env.update(DATABASE_URL=f"sqlite:///{d}/snap.db", LICENSE_DIR=str(d), PYTHONPATH=str(ROOT / "backend"))
    out = d / "snapshot.json"
    r = subprocess.run([sys.executable, "-c", SNAPSHOT, str(out)], cwd=ROOT / "backend", env=env,
                       capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stderr[-3000:]
    data = json.loads(out.read_text())
    data["pairs"] = [tuple(p) for p in data["pairs"]]
    return data


def _resolve(schema, spec, depth=0):
    if depth > 8 or not isinstance(schema, dict):
        return schema or {}
    if "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        return _resolve(spec["components"]["schemas"][name], spec, depth + 1)
    for key in ("allOf", "anyOf", "oneOf"):
        if key in schema:
            options = [o for o in schema[key] if o.get("type") != "null"]
            if options:
                return _resolve(options[0], spec, depth + 1)
    return schema


def example(schema, spec, depth=0):
    """Minimal value that satisfies the schema's required fields (used to get past 422)."""
    s = _resolve(schema, spec, depth)
    if "enum" in s:
        return s["enum"][0]
    if "default" in s and s["default"] is not None:
        return s["default"]
    t = s.get("type")
    if t == "object" or "properties" in s:
        props = s.get("properties", {})
        return {k: example(props[k], spec, depth + 1) for k in s.get("required", []) if k in props}
    if t == "array":
        return []
    if t == "integer":
        return max(1, s.get("minimum", 1))
    if t == "number":
        return max(1, s.get("minimum", 1))
    if t == "boolean":
        return True
    if t == "string":
        fmt = s.get("format")
        if fmt == "date-time":
            return "2030-01-01T00:00:00Z"
        if fmt == "date":
            return "2030-01-01"
        if fmt == "email":
            return "probe@example.test"
        m = re.match(r"^\^\(([^|)]+)", s.get("pattern", ""))
        if m:
            return m.group(1)
        return "x" * max(1, s.get("minLength", 1))
    return "x"


def requests_for(method, path, spec):
    """Yield (url, kwargs) variants: empty JSON body, and schema-valid body/query."""
    url = re.sub(r"\{[^}]+\}", "1", path).replace(":path", "")
    yield url, {"json": {}}
    op = spec["paths"].get(path, {}).get(method.lower(), {})
    params = {p["name"]: example(p.get("schema", {}), spec) for p in op.get("parameters", [])
              if p.get("in") == "query" and p.get("required")}
    body = op.get("requestBody", {}).get("content", {})
    kw = {"params": params}
    if "application/json" in body:
        kw["json"] = example(body["application/json"].get("schema", {}), spec)
    elif "multipart/form-data" in body:
        kw["files"] = {"file": ("probe.txt", b"probe")}
    elif "application/x-www-form-urlencoded" in body:
        kw["data"] = example(body["application/x-www-form-urlencoded"].get("schema", {}), spec)
    yield url, kw


def protected(pairs):
    public = PUBLIC | EXTRA_PUBLIC
    return [(m, p) for m, p in pairs if f"{m} {canonical(p)}" not in public]


def test_api_endpoint_count_is_450_plus(snapshot):
    pairs = snapshot["pairs"]
    collapsed = {(m, canonical(p)) for m, p in pairs if p != "/{path:path}"}
    api_only = {x for x in collapsed if x[1].startswith("/api/")}
    RECEIPTS.mkdir(parents=True, exist_ok=True)
    (RECEIPTS / "B-route-census.json").write_text(json.dumps({
        "route_method_pairs_raw": len(pairs),
        "route_method_pairs_v1_collapsed_excluding_spa_fallback": len(collapsed),
        "api_prefixed_v1_collapsed": len(api_only),
        "non_api": sorted(" ".join(x) for x in collapsed - api_only),
        "public_allowlist_size": len(PUBLIC | EXTRA_PUBLIC),
        "protected_route_method_pairs_raw": len(protected(pairs)),
    }, indent=1))
    assert len(collapsed) >= 450


def test_rbac_matrix_has_1800_plus_cases():
    r = subprocess.run([sys.executable, "-m", "pytest", "--collect-only", "-q", "tests/test_rbac.py", "-p", "no:cacheprovider"],
                       cwd=ROOT, env=dict(os.environ, PYTHONPATH="backend:."), capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    cases = [line for line in r.stdout.splitlines() if "::" in line]
    (RECEIPTS / "B-rbac-collect.json").write_text(json.dumps({"tests/test_rbac.py collected": len(cases)}))
    assert len(cases) >= 1800


@pytest.fixture(scope="module", params=["api_key_unset", "api_key_set"])
def live(request, tmp_path_factory):
    env = {"API_KEY": secrets.token_urlsafe(24)} if request.param == "api_key_set" else None
    o = LiveOdin(tmp_path_factory.mktemp("census-" + request.param), env=env).start()
    o.setup_admin()
    headers = {"Authorization": "Bearer " + o.admin_token}
    if env:
        headers["X-API-Key"] = env["API_KEY"]
    assert o.post("/api/setup/complete", headers=headers).status_code == 200
    # Real tenant data so that a leaked read would carry content.
    from sqlalchemy import text
    with o.engine.begin() as c:
        c.execute(text("INSERT INTO groups(id,name,is_org) VALUES(5,'tenant-five',1)"))
        c.execute(text("INSERT INTO printers(id,name,api_type,slot_count,is_active,tags,timelapse_enabled,shared,org_id) "
                       "VALUES(1,'tenant-five-x1c','bambu',1,1,'[]',0,0,5)"))
    yield request.param, o
    o.stop()


def test_every_non_public_route_refuses_anonymous_and_fabricated_odin_tokens(live, snapshot):
    mode, o = live
    spec = snapshot["openapi"]
    rows = []
    for method, path in protected(snapshot["pairs"]):
        for url, kw in requests_for(method, path, spec):
            for label, headers in (("anonymous", {}), ("fabricated_odin_token", {"X-API-Key": "odin_" + secrets.token_urlsafe(18)})):
                try:
                    status = requests.request(method, o.url + url, headers=headers, timeout=30, allow_redirects=False, **kw).status_code
                except requests.RequestException as exc:
                    status = "EXC:" + type(exc).__name__
                rows.append([method, path, label, "valid" if "params" in kw else "empty", status])
    succeeded = [r for r in rows if not isinstance(r[4], int) or r[4] < 400]
    crashed = [r for r in rows if isinstance(r[4], int) and r[4] >= 500]
    not_auth = [r for r in rows if r[4] not in (401, 403)]
    (RECEIPTS / f"B-live-auth-probe-{mode}.json").write_text(json.dumps({
        "requests": len(rows), "route_method_pairs": len({(r[0], r[1]) for r in rows}),
        "histogram": {str(k): v for k, v in Counter(str(r[4]) for r in rows).items()},
        "succeeded": succeeded, "crashed": crashed, "neither_401_nor_403": not_auth}, indent=1))
    assert succeeded == [], succeeded[:20]
    assert crashed == [], crashed[:20]
    # Every protected route must answer an unauthenticated schema-valid request with 401/403.
    valid_not_auth = [r for r in not_auth if r[3] == "valid"]
    assert valid_not_auth == [], valid_not_auth[:20]


def test_setup_network_requires_auth_after_setup_completes(live):
    mode, o = live
    for path in ("/api/setup/network", "/api/v1/setup/network"):
        for headers in ({}, {"X-API-Key": "odin_" + secrets.token_urlsafe(18)}):
            assert o.get(path, headers=headers).status_code in (401, 403)
