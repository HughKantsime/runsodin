"""Cert F / cluster C: IP allowlisting (F12-12.2-01/02), /label perimeter matching (F12-12.6-01) and branding
mutation authority (F12-12.6-02) on real ODIN servers.

Server ``odin``: proxy-header trust for loopback so requests carry synthetic client IPs via X-Forwarded-For
(uvicorn resolves request.client.host from it, exactly what the production middleware reads).
Server ``keyed``: API_KEY set, so the real perimeter middleware is active.
"""
from __future__ import annotations

import io
import itertools
import secrets

import pytest

from tests.test_contracts.test_cert_F_00_live import LiveOdin, strong_password

PERIMETER = "Invalid or missing API key"
_ips = (f"198.19.{a}.{b}" for a, b in itertools.product(range(1, 200), range(1, 250)))


def fresh_ip() -> str:
    return next(_ips)


def at(ip: str, h: dict | None = None) -> dict:
    return {**(h or {}), "X-Forwarded-For": ip}


def png() -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (2, 2), (10, 20, 30)).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture(scope="module")
def odin(tmp_path_factory):
    o = LiveOdin(tmp_path_factory.mktemp("odin_perim"), env={"ODIN_CERT_PROXY_HEADERS": "1"}).start()
    o.setup_admin()
    o.ah = {"Authorization": "Bearer " + o.login(*o.admin, **at(fresh_ip())).json()["access_token"]}
    o.role_h = {}
    for role in ("viewer", "operator"):
        pw = strong_password()
        r = o.post("/api/users", json={"username": f"p_{role}", "email": f"p_{role}@example.test", "password": pw,
                                       "role": role}, headers=o.ah)
        assert r.status_code == 200, r.text
        o.role_h[role] = {"Authorization": "Bearer " + o.login(f"p_{role}", pw, **at(fresh_ip())).json()["access_token"]}
    o.role_h["admin"] = o.ah
    o.role_h["none"] = {}
    yield o
    o.stop()


def put_allowlist(o, body, ip=None):
    h = o.ah if ip is None else at(ip, o.ah)
    return o.put("/api/config/ip-allowlist", json=body, headers=h)


# ============================================================ F12-12.2 IP allowlist

def test_allowlist_disabled_by_default_allows_any_ip(odin):
    assert odin.get("/api/config/ip-allowlist", headers=odin.ah).json() == {"enabled": False, "cidrs": [], "mode": "api_and_ui"}
    assert odin.get("/api/printers", headers=at("10.99.0.1", odin.ah)).status_code == 200


def test_lockout_protection_appends_callers_exact_address(odin):
    r = put_allowlist(odin, {"enabled": True, "cidrs": ["10.20.0.0/16", "2001:db8::/32"]}, ip="10.30.0.9")
    assert r.status_code == 200, r.text
    assert r.json()["cidrs"] == ["10.20.0.0/16", "2001:db8::/32", "10.30.0.9/32"]
    # the saving administrator is not locked out of the API
    assert odin.get("/api/config/ip-allowlist", headers=at("10.30.0.9", odin.ah)).json()["cidrs"][-1] == "10.30.0.9/32"
    assert odin.get("/api/printers", headers=at("10.30.0.9", odin.ah)).status_code == 200


def test_lockout_protection_does_not_duplicate_already_covered_caller(odin):
    r = put_allowlist(odin, {"enabled": True, "cidrs": ["10.20.0.0/16", "2001:db8::/32", "10.30.0.0/24"]}, ip="10.30.0.9")
    assert r.json()["cidrs"] == ["10.20.0.0/16", "2001:db8::/32", "10.30.0.0/24"]


def test_lockout_protection_ipv6_caller_gets_128(odin):
    r = put_allowlist(odin, {"enabled": True, "cidrs": ["10.20.0.0/16", "10.30.0.0/24"]}, ip="2001:db9::7")
    assert r.status_code == 403  # caller not currently allowed -> middleware refuses before the handler
    r = put_allowlist(odin, {"enabled": True, "cidrs": ["10.20.0.0/16", "10.30.0.0/24"]}, ip="2001:db8::7")
    assert r.status_code == 200 and r.json()["cidrs"] == ["10.20.0.0/16", "10.30.0.0/24", "2001:db8::7/128"]


def test_invalid_cidr_rejected_and_config_unchanged(odin):
    before = odin.get("/api/config/ip-allowlist", headers=at("10.30.0.9", odin.ah)).json()
    r = put_allowlist(odin, {"enabled": True, "cidrs": ["10.20.0.0/33"]}, ip="10.30.0.9")
    assert r.status_code == 400 and "Invalid CIDR" in r.json()["detail"]
    assert odin.get("/api/config/ip-allowlist", headers=at("10.30.0.9", odin.ah)).json() == before


@pytest.mark.parametrize("ip,allowed", [
    ("10.20.5.5", True), ("10.20.255.254", True), ("10.21.0.1", False), ("10.19.255.255", False),
    ("10.30.0.200", True), ("10.31.0.1", False), ("2001:db8::7", True), ("2001:db8::8", False),
    ("192.0.2.1", False),
])
def test_cidr_membership_decides_api_access(odin, ip, allowed):
    r = odin.get("/api/printers", headers=at(ip, odin.ah))
    if allowed:
        assert r.status_code == 200, r.text
    else:
        assert r.status_code == 403 and r.json()["detail"] == "IP address not allowed"


def test_denied_ip_is_refused_before_authentication(odin):
    r = odin.get("/api/printers", headers=at("192.0.2.1"))
    assert r.status_code == 403 and r.json()["detail"] == "IP address not allowed"


ALLOWLIST_EXEMPT_DEFECT = (
    "DEFECT: backend/core/app.py:506-519 skips the IP allowlist (checked at :521-552) for every path ending in "
    "'/label' or '/labels/batch', all '/api/auth*' and '/api/setup*' routes, '/api/overlay/*' and '/api/license'. "
    "Scenario: allowlist enabled for the school LAN only; an outsider from a non-allowlisted address with a "
    "stolen/phished password (or session token) still calls POST /api/auth/login, /api/auth/me, "
    "/api/auth/ws-token (then the exempt /ws telemetry socket), GET /api/spools/{id}/label and "
    "POST /api/vision/training-data/{id}/label, so the allowlist neither blocks credential use nor those APIs.")


@pytest.mark.xfail(strict=True, reason=ALLOWLIST_EXEMPT_DEFECT)
def test_denied_ip_cannot_log_in(odin):
    r = odin.login(*odin.admin, **at("192.0.2.66"))
    assert r.status_code == 403, r.status_code


@pytest.mark.xfail(strict=True, reason=ALLOWLIST_EXEMPT_DEFECT)
@pytest.mark.parametrize("method,path", [
    ("get", "/api/auth/me"),
    ("post", "/api/auth/ws-token"),
    ("get", "/api/spools/1/label"),
    ("post", "/api/vision/training-data/1/label"),
])
def test_denied_ip_cannot_use_exempt_authenticated_api(odin, method, path):
    r = getattr(odin, method)(path, headers=at("192.0.2.67", odin.ah), json={} if method == "post" else None)
    assert r.status_code == 403, (path, r.status_code, r.text[:200])


def test_disabling_allowlist_restores_access(odin):
    r = put_allowlist(odin, {"enabled": False, "cidrs": []}, ip="10.30.0.9")
    assert r.status_code == 200 and r.json()["enabled"] is False
    assert odin.get("/api/printers", headers=at("192.0.2.1", odin.ah)).status_code == 200


def test_allowlist_config_requires_superadmin(odin):
    for who in ("none", "viewer", "operator"):
        assert odin.get("/api/config/ip-allowlist", headers=odin.role_h[who]).status_code in (401, 403)
        assert odin.put("/api/config/ip-allowlist", json={"enabled": True, "cidrs": ["10.0.0.0/8"]},
                        headers=odin.role_h[who]).status_code in (401, 403)
    assert odin.get("/api/config/ip-allowlist", headers=odin.ah).json()["enabled"] is False


# ============================================================ F12-12.6-02 branding

BRANDING_MUTATIONS = [
    ("put", "/api/branding", dict(json={"app_name": "Cert C Farm"})),
    ("post", "/api/branding/logo", "logo"),
    ("post", "/api/branding/favicon", "favicon"),
    ("delete", "/api/branding/logo", {}),
    ("delete", "/api/branding/favicon", {}),
]


def _branding_call(o, method, path, kw, headers):
    if isinstance(kw, str):
        kw = {"files": {"file": (f"{kw}.png", png(), "image/png")}}
    return getattr(o, method)(path, headers=headers, **kw)


def test_branding_get_is_public(odin):
    r = odin.get("/api/branding")
    assert r.status_code == 200 and "app_name" in r.json()


@pytest.mark.parametrize("who,expected", [("none", 401), ("viewer", 403), ("operator", 403)])
@pytest.mark.parametrize("idx", range(len(BRANDING_MUTATIONS)))
def test_branding_mutations_refused_for_non_admin(odin, who, expected, idx):
    before = odin.sql("SELECT * FROM branding")
    method, path, kw = BRANDING_MUTATIONS[idx]
    r = _branding_call(odin, method, path, kw, odin.role_h[who])
    assert r.status_code == expected, (who, path, r.status_code, r.text)
    assert odin.sql("SELECT * FROM branding") == before


def test_branding_mutations_succeed_for_admin_and_persist(odin):
    codes = {}
    for method, path, kw in BRANDING_MUTATIONS[:3]:
        r = _branding_call(odin, method, path, kw, odin.ah)
        codes[path] = r.status_code
    assert set(codes.values()) == {200}, codes
    pub = odin.get("/api/branding").json()
    assert pub["app_name"] == "Cert C Farm" and pub["logo_url"] and pub["favicon_url"]
    assert odin.get(pub["logo_url"]).status_code == 200
    for method, path, kw in BRANDING_MUTATIONS[3:]:
        assert _branding_call(odin, method, path, kw, odin.ah).status_code == 200
    pub = odin.get("/api/branding").json()
    assert pub["logo_url"] is None and pub["favicon_url"] is None


# ============================================================ API_KEY perimeter: /label + branding

@pytest.fixture(scope="module")
def keyed(tmp_path_factory):
    key = secrets.token_hex(24)
    o = LiveOdin(tmp_path_factory.mktemp("odin_keyed"), env={"API_KEY": key}).start()
    o.setup_admin()
    o.key = key
    yield o
    o.stop()


@pytest.mark.parametrize("path", [
    "/api/printers/1/relabel", "/api/labelx", "/api/spools/1/label-maker", "/api/spools/1/labelled",
    "/api/spools/1/label/extra", "/api/spools/labels/batch-extra", "/api/spools/labels", "/api/v1/printers/1/relabel",
    "/api/printers",
])
def test_label_lookalike_paths_stay_behind_api_key_perimeter(keyed, path):
    r = keyed.get(path)
    assert r.status_code == 401 and r.json()["detail"] == PERIMETER, (path, r.status_code, r.text)


@pytest.mark.parametrize("method,path", [
    ("get", "/api/spools/1/label"), ("get", "/api/v1/spools/1/label"), ("get", "/api/spools/labels/batch?spool_ids=1"),
    ("post", "/api/vision/training-data/1/label"),
])
def test_real_label_routes_pass_perimeter_but_still_require_route_auth(keyed, method, path):
    r = getattr(keyed, method)(path)
    assert r.status_code == 401 and r.json()["detail"] == "Not authenticated", (path, r.status_code, r.text)


@pytest.mark.parametrize("path", ["/api/label", "/api/printers/label", "/api/models/label", "/api/jobs/label"])
def test_label_exemption_is_suffix_match_not_exact_route_match(keyed, path):
    """Mismatch evidence: non-label routes whose path merely ends in '/label' skip the perimeter."""
    r = keyed.get(path)
    assert r.json().get("detail") != PERIMETER, (path, r.status_code, r.text)
    assert r.status_code in (401, 404, 405, 422)  # decided by routing / route-level auth, not the API key


def test_valid_api_key_reaches_label_route(keyed):
    r = keyed.get("/api/spools/999/label", headers={"X-API-Key": keyed.key})
    assert r.status_code == 404, r.text  # route reached and resolved the (absent) spool


def test_keyed_branding_get_public_and_mutations_need_credentials(keyed):
    assert keyed.get("/api/branding").status_code == 200
    for method, path, kw in BRANDING_MUTATIONS:
        r = _branding_call(keyed, method, path, kw, {})
        assert r.status_code == 401 and r.json()["detail"] == PERIMETER, (method, path, r.text)
    # POST/PUT/DELETE on the public GET path are not exempt either
    assert keyed.post("/api/branding", json={}).json()["detail"] == PERIMETER
    assert keyed.delete("/api/branding").json()["detail"] == PERIMETER
    r = keyed.put("/api/branding", json={"app_name": "Keyed Farm"}, headers={"X-API-Key": keyed.key})
    assert r.status_code == 200 and keyed.get("/api/branding").json()["app_name"] == "Keyed Farm"
