"""Cert F / cluster D — F12-12.3-02 Ed25519 license signing, no dev bypass.

Verifier tests use the real ``license_manager.verify_license_text``. The live-server tests run the
real ``main:app`` with the UNMODIFIED embedded trust root (LiveOdin license_tier=None, so no public key
substitution) and prove every license-loading path (startup file discovery, ODIN_LICENSE_FILE,
HTTP upload, HTTP activation via a stub license server) rejects a license signed by any other key.
"""
from __future__ import annotations

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

import license_manager
from tests.test_contracts.test_cert_F_00_live import LiveOdin, free_port


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _sign(key, payload: dict) -> str:
    raw = json.dumps(payload).encode()
    return _b64(raw) + "." + _b64(key.sign(raw))


def _pem(key) -> str:
    return key.public_key().public_bytes(serialization.Encoding.PEM,
                                         serialization.PublicFormat.SubjectPublicKeyInfo).decode()


PAYLOAD = {"tier": "enterprise", "licensee": "Cert D Synthetic", "expires_at": "2099-01-01"}


def test_embedded_trust_root_is_an_ed25519_public_key():
    key = serialization.load_pem_public_key(license_manager.ODIN_PUBLIC_KEY.encode())
    assert isinstance(key, Ed25519PublicKey)


def test_verifier_accepts_properly_signed_and_rejects_tamper_and_wrong_key(monkeypatch):
    issuer = Ed25519PrivateKey.generate()
    monkeypatch.setattr(license_manager, "ODIN_PUBLIC_KEY", _pem(issuer))
    good = _sign(issuer, PAYLOAD)
    info = license_manager.verify_license_text(good, installation_id_reader=lambda: "")
    assert info.valid and info.tier == "enterprise" and not info.error

    payload_b64, sig_b64 = good.split(".")
    tampered_payload = _b64(json.dumps({**PAYLOAD, "tier": "enterprise", "max_printers": 9999}).encode())
    for forged in (
        tampered_payload + "." + sig_b64,                                   # payload changed
        _sign(Ed25519PrivateKey.generate(), PAYLOAD),                       # wrong key
        payload_b64 + "." + _b64(b"\x00" * 64),                             # zero signature
        payload_b64 + ".",                                                  # empty signature
        payload_b64,                                                        # unsigned
    ):
        bad = license_manager.verify_license_text(forged, installation_id_reader=lambda: "")
        assert not bad.valid and bad.tier == "community", forged[:20]


def test_embedded_production_root_rejects_any_locally_generated_key():
    """No dev key ships: a license signed by a fresh key never validates against the shipped root."""
    for _ in range(3):
        forged = _sign(Ed25519PrivateKey.generate(), PAYLOAD)
        info = license_manager.verify_license_text(forged, installation_id_reader=lambda: "")
        assert not info.valid and info.error == "Invalid license signature"


class _StubLicenseServer(BaseHTTPRequestHandler):
    license_file = ""
    calls = []

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("content-length", 0))
        type(self).calls.append(json.loads(self.rfile.read(n) or b"{}"))
        body = json.dumps({"license_file": type(self).license_file}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture(scope="module")
def forged_license_server():
    port = free_port()
    _StubLicenseServer.license_file = _sign(Ed25519PrivateKey.generate(), PAYLOAD)
    srv = HTTPServer(("127.0.0.1", port), _StubLicenseServer)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{port}"
    srv.shutdown()


@pytest.fixture(scope="module")
def odin_unpatched(tmp_path_factory, forged_license_server):
    work = tmp_path_factory.mktemp("odinD_lic")
    forged_alt = work / "alt.license"
    forged_alt.write_text(_sign(Ed25519PrivateKey.generate(), PAYLOAD))
    o = LiveOdin(work, license_tier=None, env={"LICENSE_SERVER_URL": forged_license_server})
    # A forged license is planted in the default discovery location before startup.
    (work / "license" / "odin.license").write_text(_sign(Ed25519PrivateKey.generate(), PAYLOAD))
    assert "ODIN_CERT_TEST_LICENSE_PUBKEY" not in o.env
    o.start()
    o.setup_admin()
    o.H = o.admin_headers()
    o.forged_alt = forged_alt
    yield o
    o.stop()


def test_live_server_ignores_forged_license_on_disk(odin_unpatched):
    o = odin_unpatched
    lic = o.get("/api/license").json()
    assert lic["tier"] == "community"
    # A pro/enterprise-gated route stays locked.
    r = o.post("/api/webhooks", headers=o.H, json={"name": "x", "url": "https://discord.com/api/webhooks/1/x"})
    assert r.status_code == 403


def test_live_upload_of_forged_license_rejected(odin_unpatched):
    o = odin_unpatched
    forged = _sign(Ed25519PrivateKey.generate(), PAYLOAD)
    r = o.post("/api/license/upload", headers=o.H, files={"file": ("odin.license", forged.encode())})
    assert r.status_code == 400
    r = o.post("/api/license/upload", headers=o.H,
               files={"file": ("odin.json", json.dumps({"payload": forged.split(".")[0],
                                                        "signature": forged.split(".")[1]}).encode())})
    assert r.status_code == 400
    assert o.get("/api/license").json()["tier"] == "community"


def test_live_activation_with_forged_server_response_rejected(odin_unpatched):
    o = odin_unpatched
    r = o.post("/api/license/activate", headers=o.H, json={"key": "ODIN-CERT-D-SYNTH-0000"})
    assert _StubLicenseServer.calls, "activation never reached the (stub) license server"
    assert r.status_code == 400, r.text
    assert o.get("/api/license").json()["tier"] == "community"
    path = o.dir / "license" / "odin.license"
    assert not path.exists() or path.read_text() != _StubLicenseServer.license_file


def test_live_explicit_license_file_env_path_also_verified(tmp_path):
    o = LiveOdin(tmp_path, license_tier=None)
    o.env["ODIN_LICENSE_FILE"] = str(tmp_path / "explicit.license")
    (tmp_path / "explicit.license").write_text(_sign(Ed25519PrivateKey.generate(), PAYLOAD))
    o.start()
    try:
        assert o.get("/api/license").json()["tier"] == "community"
    finally:
        o.stop()


def test_live_properly_signed_license_accepted_by_running_server(tmp_path):
    """Positive control on the running server: with the trust root replaced by a known key
    (the only substitution), a license signed by that key is honored end to end."""
    o = LiveOdin(tmp_path, license_tier="enterprise")
    o.start()
    try:
        assert o.get("/api/license").json()["tier"] == "enterprise"
    finally:
        o.stop()
