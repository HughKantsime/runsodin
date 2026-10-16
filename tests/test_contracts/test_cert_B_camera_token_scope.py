"""Cert B: a camera-only overlay token (scope read:cameras) authenticates nothing but the
overlay MJPEG relay, on the real app, independent of any generic scope gate.

Defence in depth: routes that declare no scope must not accept it either.
"""
import json
import os
import subprocess
import sys
import textwrap

import pytest
from pathlib import Path

from tests.test_contracts.test_route_auth_census import PUBLIC

ROOT = Path(__file__).resolve().parents[2]
RELAY = "GET /api/overlay/{printer_id}/camera"
PROBE = textwrap.dedent('''
import json, os, re, secrets, sys
from pathlib import Path
from sqlalchemy import create_engine, text
from core.schema import bootstrap_database
engine = create_engine(os.environ['DATABASE_URL'])
bootstrap_database(engine, Path('.'))
from core.auth import hash_password
raw = 'odin_' + secrets.token_urlsafe(32)
with engine.begin() as c:
    # Worst case: the token belongs to a superadmin.
    c.execute(text("INSERT INTO users(id,username,email,password_hash,role,is_active) VALUES(1,'admin','a@example.test','x','admin',1)"))
    c.execute(text("INSERT INTO api_tokens(user_id,name,token_hash,token_prefix,scopes) VALUES(1,'OBS',:h,:p,:s)"),
              {'h': hash_password(raw), 'p': raw[:10], 's': sys.argv[2]})
    c.execute(text("INSERT INTO printers(id,name,is_active,camera_enabled,camera_url,slot_count) VALUES(1,'P',1,1,'rtsp://192.0.2.1/cam',1)"))
from core.app import create_app
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
public = set(json.loads(sys.argv[1]))
app = create_app()
client = TestClient(app, raise_server_exceptions=False)
results = {}
for route in app.routes:
    if not isinstance(route, APIRoute):
        continue
    for method in sorted(route.methods - {'HEAD'}):
        key = method + ' ' + route.path.replace('/api/v1/', '/api/')
        if key in public:
            continue
        path = re.sub(r'\\{[^}:]+(:[^}]+)?\\}', '1', route.path)
        with_token = client.request(method, path, headers={'X-API-Key': raw}, json={}).status_code
        anonymous = client.request(method, path, json={}).status_code
        results[key + ' ' + route.path] = [with_token, anonymous]
print('CERTB=' + json.dumps(results))
''')


def test_camera_only_token_is_refused_on_every_route_but_the_relay(tmp_path):
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{tmp_path}/scope.db", "JWT_SECRET_KEY": "scope-test-key-only",
           "PYTHONPATH": str(ROOT / "backend"), "ODIN_DISABLE_BACKGROUND": "1"}
    env.pop("API_KEY", None)
    result = subprocess.run([sys.executable, "-c", PROBE, json.dumps(sorted(PUBLIC | {RELAY})), json.dumps(["read:cameras"])], cwd=ROOT / "backend",
                            env=env, capture_output=True, text=True, timeout=600)
    assert result.returncode == 0, result.stderr[-3000:]
    statuses = json.loads(next(l for l in result.stdout.splitlines() if l.startswith("CERTB="))[6:])
    assert len(statuses) > 300  # the whole API was probed
    # Refused outright, or treated exactly like an anonymous request (routes that serve
    # anonymous callers, e.g. logout or pre-setup network info, or reject the input first).
    accepted = {route: codes for route, codes in statuses.items() if codes[0] not in (401, 403) and codes[0] != codes[1]}
    assert accepted == {}, f"camera-only token accepted on {len(accepted)} routes: {sorted(accepted.items())[:40]}"
    assert sum(codes[0] in (401, 403) for codes in statuses.values()) > 300


from tests.test_contracts.test_api_token_delegation_behavior import token_client  # noqa: E402,F401
from tests.test_contracts.test_education_review_workflow import review_db  # noqa: E402,F401


def test_camera_token_is_refused_by_get_current_user_and_expires_by_default(token_client, review_db):
    from datetime import datetime, timedelta, timezone
    client, issue = token_client
    camera = issue(["read:cameras"])
    # get_current_user refuses it even on /tokens, which declares no scope.
    assert client.get("/tokens", headers=camera).status_code == 401
    assert client.post("/tokens", headers=camera, json={"name": "child", "scopes": ["read:cameras"]}).status_code == 401
    # A camera-only token minted without an expiry gets the default lifetime; others are unchanged.
    session = issue(["admin"])
    minted = client.post("/tokens", headers=session, json={"name": "OBS overlay: Bay 7 (#7)", "scopes": ["read:cameras"]})
    assert minted.status_code == 200
    expires = datetime.fromisoformat(minted.json()["expires_at"])
    assert abs(expires - (datetime.now(timezone.utc) + timedelta(days=365))) < timedelta(minutes=5)
    other = client.post("/tokens", headers=session, json={"name": "CI", "scopes": ["read:printers"]})
    assert other.status_code == 200 and other.json()["expires_at"] is None


def _probe(tmp_path, scopes):
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{tmp_path}/scope.db", "JWT_SECRET_KEY": "scope-test-key-only",
           "PYTHONPATH": str(ROOT / "backend")}
    env.pop("API_KEY", None)
    result = subprocess.run([sys.executable, "-c", PROBE, json.dumps(sorted(PUBLIC | {RELAY})), json.dumps(scopes)],
                            cwd=ROOT / "backend", env=env, capture_output=True, text=True, timeout=600)
    assert result.returncode == 0, result.stderr[-3000:]
    return json.loads(next(l for l in result.stdout.splitlines() if l.startswith("CERTB=")))


# Routes whose declared scope a read:analytics token legitimately satisfies.
ANALYTICS_ROUTES = ("/api/analytics", "/api/reports", "/api/stats", "/api/chargeback", "/api/education/usage",
                    "/api/v1/analytics", "/api/v1/reports", "/api/v1/stats", "/api/v1/chargeback")


@pytest.mark.xfail(strict=True, reason="needs cert-fix-F generic scope gate")
def test_narrow_scoped_token_authenticates_only_routes_within_its_scope(tmp_path):
    """Systemic (pre-existing): a scoped token passes get_current_user on routes that declare
    no scope. With a generic read/write gate, a read:analytics token reaches nothing else."""
    statuses = _probe(tmp_path, ["read:analytics"])
    accepted = {route: codes for route, codes in statuses.items()
                if codes[0] not in (401, 403) and codes[0] != codes[1]
                and not route.split(" ")[-1].startswith(ANALYTICS_ROUTES)}
    assert accepted == {}, f"{len(accepted)} routes accept a read:analytics token: {sorted(accepted)[:30]}"


@pytest.mark.parametrize("scopes,status", [(["read:cameras"], 200), (["read:cameras", "read"], 422),
                                           (["read:cameras", "admin"], 422), (["read:cameras", "write"], 422),
                                           (["read:cameras", "read:printers"], 422), (["read:cameras", "write:jobs"], 422),
                                           (["read:cameras", "agent:read"], 422), (["read:cameras", "read:cameras"], 200)])
def test_camera_scope_is_the_token_s_only_scope(token_client, scopes, status):
    client, issue = token_client
    response = client.post("/tokens", headers=issue(["admin"]), json={"name": "combo", "scopes": scopes})
    assert response.status_code == status, response.text
    if status == 200:
        assert response.json()["expires_at"] is not None  # read:cameras always gets an expiry


def test_camera_token_expiry_defaults_to_a_year_and_longer_is_refused(token_client):
    from datetime import datetime, timedelta, timezone
    client, issue = token_client
    minted = client.post("/tokens", headers=issue(["admin"]), json={"name": "OBS", "scopes": ["read:cameras"]})
    expires = datetime.fromisoformat(minted.json()["expires_at"])
    assert abs(expires - (datetime.now(timezone.utc) + timedelta(days=365))) < timedelta(minutes=5)
    exact = client.post("/tokens", headers=issue(["admin"]), json={"name": "OBS 365", "scopes": ["read:cameras"], "expires_days": 365})
    assert exact.status_code == 200, exact.text
    # Longer than the default is refused, not clamped.
    for days in (366, 3650):
        longer = client.post("/tokens", headers=issue(["admin"]), json={"name": "OBS long", "scopes": ["read:cameras"], "expires_days": days})
        assert longer.status_code == 422 and "365" in longer.text, longer.text
