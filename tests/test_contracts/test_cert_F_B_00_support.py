"""Cert F / cluster B shared support (no tests): a LiveOdin variant and HTTP helpers.

``LiveOdinB`` is the shared real-server helper with two additions: the license signing key is kept
(so an admin can upload a *valid* replacement license through the real route), and uvicorn trusts
``X-Forwarded-For`` from loopback so each login can come from its own synthetic client address
(slowapi's 10/minute login limit is per client address; the limit itself is not under test here).
"""
from __future__ import annotations

import base64
import itertools
import json
import secrets

import requests
from sqlalchemy import text

from tests.test_contracts.test_cert_F_00_live import LiveOdin, strong_password

_ips = itertools.count(10)


def next_ip() -> str:
    n = next(_ips)
    return f"10.{(n >> 16) & 255}.{(n >> 8) & 255}.{n & 255}"


class LiveOdinB(LiveOdin):
    def __init__(self, workdir, env=None, **kw):
        env = dict(env or {})
        env.setdefault("ODIN_CERT_PROXY_HEADERS", "1")
        env.setdefault("ENV_FILE_PATH", str(workdir / "odin.env"))
        super().__init__(workdir, env=env, **kw)

    def _write_license(self):
        if not self.license_tier:
            return
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        self._license_key = Ed25519PrivateKey.generate()
        pub = self._license_key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()
        (self.dir / "license" / "odin.license").write_text(self.sign_license(self.license_tier, "Cert F Synthetic"))
        self.env["ODIN_CERT_TEST_LICENSE_PUBKEY"] = pub

    def sign_license(self, tier: str, licensee: str) -> str:
        raw = json.dumps({"tier": tier, "licensee": licensee, "expires_at": "2099-01-01"}).encode()
        return base64.urlsafe_b64encode(raw).decode() + "." + base64.urlsafe_b64encode(self._license_key.sign(raw)).decode()

    # -- helpers ---------------------------------------------------------------------------
    def exec(self, statement: str, **params):
        with self.engine.begin() as c:
            c.execute(text(statement), params)

    def one(self, statement: str, **params):
        with self.engine.begin() as c:
            return c.execute(text(statement), params).fetchone()

    def all(self, statement: str, **params):
        with self.engine.begin() as c:
            return c.execute(text(statement), params).fetchall()

    def login_ip(self, username: str, password: str, user_agent: str = "cert-b-client/1.0", ip: str | None = None):
        return requests.post(self.url + "/api/auth/login", data={"username": username, "password": password},
                             headers={"X-Forwarded-For": ip or next_ip(), "User-Agent": user_agent}, timeout=20)

    def token(self, username: str, password: str, **kw) -> str:
        r = self.login_ip(username, password, **kw)
        assert r.status_code == 200, r.text
        return r.json()["access_token"]

    def boot_admin(self):
        """Setup admin, then a real password login (recorded session) for admin calls."""
        self.setup_admin()
        self.admin_bearer = {"Authorization": "Bearer " + self.token(*self.admin)}
        return self.admin_bearer

    def mkuser(self, username: str, role: str, login: bool = True, **extra):
        password = strong_password()
        body = {"username": username, "email": f"{username}@example.test", "password": password, "role": role, **extra}
        r = requests.post(self.url + "/api/users", json=body, headers=self.admin_bearer, timeout=20)
        assert r.status_code in (200, 201), r.text
        uid = self.one("SELECT id FROM users WHERE username=:u", u=username).id
        headers = {"Authorization": "Bearer " + self.token(username, password)} if login else None
        return {"id": uid, "username": username, "password": password, "headers": headers}

    def mint(self, headers: dict, scopes: list[str], name: str | None = None) -> dict:
        r = requests.post(self.url + "/api/tokens", json={"name": name or "cert-b-" + secrets.token_hex(3), "scopes": scopes},
                          headers={**headers, "X-Forwarded-For": next_ip()}, timeout=20)
        assert r.status_code == 200, r.text
        return r.json()
