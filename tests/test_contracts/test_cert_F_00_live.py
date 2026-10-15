"""Certification group F support: an owned, real ODIN server process (no tests here).

``LiveOdin`` bootstraps a temp SQLite database with the production bootstrap script, then runs
the real ``main:app`` under uvicorn on a free loopback port with generated throwaway keys. Only
the license trust root is substituted (a generated Ed25519 key replaces the embedded public key
inside the launcher process) so license-gated features can be exercised with a validly signed
license. Everything else (middleware, routers, DB schema, rate limiters, cookies) is production.

Throwaway credentials are generated per instance and never written to receipts.
"""
from __future__ import annotations

import base64
import json
import os
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path

import requests
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"

LAUNCHER = r'''
import os, sys
import license_manager
pub = os.environ.pop("ODIN_CERT_TEST_LICENSE_PUBKEY", "")
if pub:
    license_manager.ODIN_PUBLIC_KEY = pub
import uvicorn
uvicorn.run("main:app", host="127.0.0.1", port=int(sys.argv[1]), log_level="warning",
            proxy_headers=(os.environ.get("ODIN_CERT_PROXY_HEADERS") == "1"),
            forwarded_allow_ips=os.environ.get("ODIN_CERT_FORWARDED_ALLOW", "127.0.0.1"))
'''


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def strong_password() -> str:
    return "Cf" + secrets.token_hex(6) + "Az9"


class LiveOdin:
    def __init__(self, workdir: Path, env: dict | None = None, license_tier: str | None = "enterprise",
                 license_features: list[str] | None = None, unset: tuple[str, ...] = ()):
        self.dir = Path(workdir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.db_path = self.dir / "odin.db"
        data = self.dir / "data"
        for sub in ("data", "prints", "branding", "revisions", "edu", "license"):
            (self.dir / sub).mkdir(exist_ok=True)
        from cryptography.fernet import Fernet
        self.fernet_key = Fernet.generate_key().decode()
        self.jwt_secret = secrets.token_hex(32)
        base = {k: v for k, v in os.environ.items() if not k.startswith(("ODIN_", "DATABASE_", "API_KEY", "COOKIE_", "OIDC_", "TRUSTED_", "CORS_"))}
        base.pop("JWT_SECRET_KEY", None)
        base.pop("ENCRYPTION_KEY", None)
        base.update({
            "DATABASE_URL": f"sqlite:///{self.db_path}",
            "JWT_SECRET_KEY": self.jwt_secret,
            "ENCRYPTION_KEY": self.fernet_key,
            "COOKIE_SECURE": "false",
            "ODIN_DATA_DIR": str(data),
            "LICENSE_DIR": str(self.dir / "license"),
            "PRINT_FILE_STORAGE_ROOT": str(self.dir / "prints"),
            "ODIN_BRANDING_DIRECTORY": str(self.dir / "branding"),
            "MODEL_REVISION_STORAGE_ROOT": str(self.dir / "revisions"),
            "EDUCATION_UPLOAD_ROOT": str(self.dir / "edu"),
            "PYTHONPATH": str(BACKEND),
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        if env:
            base.update(env)
        for k in unset:
            base.pop(k, None)
        self.env = base
        self.license_tier = license_tier
        self.license_features = license_features
        self.proc = None

    # -- lifecycle ---------------------------------------------------------------------------
    def _write_license(self):
        if not self.license_tier:
            return
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        key = Ed25519PrivateKey.generate()
        pub = key.public_key().public_bytes(serialization.Encoding.PEM,
                                            serialization.PublicFormat.SubjectPublicKeyInfo).decode()
        payload = {"tier": self.license_tier, "licensee": "Cert F Synthetic", "expires_at": "2099-01-01"}
        if self.license_features is not None:
            payload["features"] = self.license_features
        raw = json.dumps(payload).encode()
        (self.dir / "license" / "odin.license").write_text(
            base64.urlsafe_b64encode(raw).decode() + "." + base64.urlsafe_b64encode(key.sign(raw)).decode())
        self.env["ODIN_CERT_TEST_LICENSE_PUBKEY"] = pub

    def start(self, timeout: float = 90):
        self._write_license()
        boot = subprocess.run([sys.executable, "-m", "scripts.bootstrap_database"], cwd=BACKEND, env=self.env,
                              capture_output=True, text=True, timeout=180)
        assert boot.returncode == 0, boot.stderr[-3000:]
        self.log = open(self.dir / "server.log", "w")
        self.proc = subprocess.Popen([sys.executable, "-c", LAUNCHER, str(self.port)], cwd=BACKEND, env=self.env,
                                     stdout=self.log, stderr=subprocess.STDOUT)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise AssertionError("server exited: " + (self.dir / "server.log").read_text()[-3000:])
            try:
                if requests.get(self.url + "/health", timeout=2).status_code == 200:
                    break
            except requests.RequestException:
                pass
            time.sleep(0.25)
        else:
            raise AssertionError("server did not become healthy")
        self.engine = create_engine(f"sqlite:///{self.db_path}")
        return self

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if getattr(self, "engine", None):
            self.engine.dispose()
        if getattr(self, "log", None):
            self.log.close()

    def restart(self):
        """Restart the server process against the same database (fresh in-memory state)."""
        self.stop()
        self.proc = None
        self.env.pop("ODIN_CERT_TEST_LICENSE_PUBKEY", None)
        self.port = free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        # Do not re-bootstrap with a new license key: re-sign with a fresh key instead.
        return self.start()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    # -- helpers -----------------------------------------------------------------------------
    def sql(self, statement: str, **params):
        with self.engine.begin() as c:
            return c.execute(text(statement), params).fetchall()

    def setup_admin(self, username: str = "certadmin"):
        password = strong_password()
        r = requests.post(self.url + "/api/setup/admin",
                          json={"username": username, "password": password, "email": f"{username}@example.test"}, timeout=20)
        assert r.status_code == 200, r.text
        self.admin = (username, password)
        self.admin_token = r.json()["access_token"]
        return username, password

    def login(self, username: str, password: str, **headers):
        return requests.post(self.url + "/api/auth/login", data={"username": username, "password": password},
                             headers=headers or None, timeout=20)

    def bearer(self, username: str, password: str) -> dict:
        r = self.login(username, password)
        assert r.status_code == 200, r.text
        return {"Authorization": "Bearer " + r.json()["access_token"]}

    def admin_headers(self) -> dict:
        return self.bearer(*self.admin)

    def create_user(self, username: str, role: str, password: str | None = None, **extra):
        password = password or strong_password()
        body = {"username": username, "email": f"{username}@example.test", "password": password, "role": role, **extra}
        r = requests.post(self.url + "/api/users", json=body, headers=self.admin_headers(), timeout=20)
        assert r.status_code in (200, 201), r.text
        return username, password, r.json()

    def get(self, path, **kw):
        kw.setdefault("timeout", 30)
        return requests.get(self.url + path, **kw)

    def post(self, path, **kw):
        kw.setdefault("timeout", 30)
        return requests.post(self.url + path, **kw)

    def put(self, path, **kw):
        kw.setdefault("timeout", 30)
        return requests.put(self.url + path, **kw)

    def patch(self, path, **kw):
        kw.setdefault("timeout", 30)
        return requests.patch(self.url + path, **kw)

    def delete(self, path, **kw):
        kw.setdefault("timeout", 30)
        return requests.delete(self.url + path, **kw)
