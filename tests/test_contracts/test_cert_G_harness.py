"""Cert group G harness: the real ODIN app factory in an isolated subprocess.

Each probe runs ``core.app.create_app()`` against its own freshly bootstrapped
SQLite database (real schema via ``bootstrap_database``) and its own
LICENSE_DIR. Requests go through the real middleware, routers, RBAC and real
password login (JWT). Only the outermost boundaries are substituted:

* the trusted license verifier key (``license_manager.ODIN_PUBLIC_KEY``) is set
  to a throwaway Ed25519 key generated in-process so the test can sign
  licenses (the production private key is not available);
* every non-loopback connect/sendto/bind and every non-localhost DNS lookup is
  refused and recorded (``NET_ATTEMPTS``), proving no phone-home during any probe
  (loopback is allowed only so a probe can run a real uvicorn server for SSE);
* ``httpx.AsyncClient`` is replaced only where a probe stubs the license portal;
* hard-coded ``/data/...`` log paths are redirected to probe-owned temp files.

This module contains no tests itself; it is imported by test_cert_G_*.py.
"""
import json
import os
import secrets
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CERT_DIR = ROOT / "artifacts" / "application-certification" / "cert-G"

PRELUDE = r'''
import os, sys, json, base64, secrets, socket, time
from pathlib import Path
WORK = Path(os.environ["CERT_G_WORK"])
NET_ATTEMPTS = []
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}
def _is_loopback(addr):
    host = addr[0] if isinstance(addr, tuple) else addr
    return isinstance(host, str) and host in _LOOPBACK
_orig_socket = socket.socket
class _GuardedSocket(_orig_socket):
    """Only loopback traffic is permitted (in-probe uvicorn for real SSE); anything else is recorded and refused."""
    def connect(self, addr):
        if self.family in (socket.AF_INET, socket.AF_INET6) and not _is_loopback(addr):
            NET_ATTEMPTS.append(("connect", repr(addr))); raise OSError("network blocked by cert-G harness")
        return super().connect(addr)
    def connect_ex(self, addr):
        if self.family in (socket.AF_INET, socket.AF_INET6) and not _is_loopback(addr):
            NET_ATTEMPTS.append(("connect_ex", repr(addr))); raise OSError("network blocked by cert-G harness")
        return super().connect_ex(addr)
    def sendto(self, data, *args):
        addr = args[-1]
        if self.family in (socket.AF_INET, socket.AF_INET6) and not _is_loopback(addr):
            NET_ATTEMPTS.append(("sendto", repr(addr))); raise OSError("network blocked by cert-G harness")
        return super().sendto(data, *args)
    def bind(self, addr):
        if self.family in (socket.AF_INET, socket.AF_INET6) and not _is_loopback(addr):
            NET_ATTEMPTS.append(("bind", repr(addr))); raise OSError("non-loopback bind blocked by cert-G harness")
        return super().bind(addr)
socket.socket = _GuardedSocket
_orig_gai = socket.getaddrinfo
def _gai(host, *a, **k):
    if host not in _LOOPBACK:
        NET_ATTEMPTS.append(("getaddrinfo", repr(host))); raise OSError("DNS blocked by cert-G harness")
    return _orig_gai(host, *a, **k)
socket.getaddrinfo = _gai
def _deny(name):
    def f(*a, **k):
        NET_ATTEMPTS.append((name, repr(a[:1])))
        raise OSError("network blocked by cert-G harness")
    return f
socket.gethostbyname = _deny("gethostbyname")
_orig_cc = socket.create_connection
def _cc(address, *a, **k):
    if not _is_loopback(address):
        NET_ATTEMPTS.append(("create_connection", repr(address))); raise OSError("network blocked by cert-G harness")
    return _orig_cc(address, *a, **k)
socket.create_connection = _cc

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
SIGNER = (Ed25519PrivateKey.from_private_bytes(bytes.fromhex(os.environ["CERT_G_SIGNER_SEED"]))
          if os.environ.get("CERT_G_SIGNER_SEED") else Ed25519PrivateKey.generate())
import license_manager as LM
LM.ODIN_PUBLIC_KEY = SIGNER.public_key().public_bytes(
    serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()

def sign_license(payload, signer=None):
    raw = json.dumps(payload).encode()
    sig = (signer or SIGNER).sign(raw)
    return base64.urlsafe_b64encode(raw).decode().rstrip("=") + "." + base64.urlsafe_b64encode(sig).decode().rstrip("=")

def license_payload(tier, **over):
    p = {"tier": tier, "licensee": "Cert G " + tier, "email": "cert-g@example.test",
         "key": "ODIN-CERT-GGGG-" + tier[:4].upper(), "issued_at": "2026-01-01",
         "expires_at": "2099-01-01"}
    p.update(over)
    return {k: v for k, v in p.items() if v is not None or k in over}

def install_license(tier, **over):
    """Install a signed license file as an operator would (file in LICENSE_DIR)."""
    if tier is None:
        path = Path(LM.LICENSE_DIR) / LM.LICENSE_FILENAME
        if path.exists(): path.unlink()
    else:
        LM.save_license_file(sign_license(license_payload(tier, **over)))
    LM._cached_license = None; LM._cached_mtime = 0

from sqlalchemy import text
from core.db import engine
from core.schema import bootstrap_database
bootstrap_database(engine, Path("backend").resolve())
from core.auth import hash_password
PASSWORD = "CertG-Password-" + secrets.token_hex(4) + "!"
def seed_user(username, role, group_id=None):
    with engine.begin() as c:
        c.execute(text("INSERT INTO users(username,email,password_hash,role,is_active,group_id) VALUES(:u,:e,:p,:r,1,:g)"),
                  {"u": username, "e": username + "@example.test", "p": hash_password(PASSWORD), "r": role, "g": group_id})
        return c.execute(text("SELECT id FROM users WHERE username=:u"), {"u": username}).scalar()
def sql(stmt, **params):
    with engine.begin() as c:
        r = c.execute(text(stmt), params)
        try: return [list(x) for x in r.fetchall()]
        except Exception: return None

from core.app import create_app
from fastapi.testclient import TestClient
from core.rate_limit import limiter
app = create_app()
def client():
    return TestClient(app, raise_server_exceptions=False)
def login(username, c=None):
    c = c or client()
    try: limiter.reset()
    except Exception: pass
    r = c.post("/api/auth/login", data={"username": username, "password": PASSWORD})
    assert r.status_code == 200, (r.status_code, r.text)
    tok = r.json()["access_token"]
    c.headers["Authorization"] = "Bearer " + tok
    return c
def body(r):
    try: return r.json()
    except Exception: return r.text[:2000]
OUT = {}
'''

EPILOGUE = r'''
OUT["_net_attempts"] = NET_ATTEMPTS
print("CERTG_JSON=" + json.dumps(OUT, default=str), flush=True)
sys.stderr.flush()
os._exit(0)  # daemon threads (e.g. an open SSE stream) must not block exit
'''


def run_probe(name: str, code: str, tmp_root: Path, extra_env: dict | None = None, timeout: int = 600) -> dict:
    work = Path(tmp_root) / name
    work.mkdir(parents=True, exist_ok=True)
    (work / "license").mkdir(exist_ok=True)
    from cryptography.fernet import Fernet
    env = dict(os.environ)
    for k in ("API_KEY", "LICENSE_SERVER_URL", "ODIN_LICENSE_FILE", "ODIN_LICENSE_READ_ONLY",
              "ODIN_REQUIRE_LICENSE_BINDING"):
        env.pop(k, None)
    env.update({
        "CERT_G_WORK": str(work),
        "DATABASE_URL": "sqlite:///" + str(work / "odin.db"),
        "LICENSE_DIR": str(work / "license"),
        "JWT_SECRET_KEY": secrets.token_urlsafe(48),
        "ENCRYPTION_KEY": Fernet.generate_key().decode(),
        "COOKIE_SECURE": "false",
        "ODIN_ITAR_MODE": "0",
        "BASE_URL": "http://127.0.0.1:9",
        "ODIN_CONTAINER_EXEC": "/usr/bin/false",
        "PYTHONPATH": str(ROOT / "backend") + os.pathsep + str(ROOT),
    })
    env.update(extra_env or {})
    script = PRELUDE + "\n" + textwrap.dedent(code) + "\n" + EPILOGUE
    (work / "probe.py").write_text(script)
    result = subprocess.run([sys.executable, str(work / "probe.py")], cwd=ROOT, env=env,
                            capture_output=True, text=True, timeout=timeout)
    (work / "stdout.txt").write_text(result.stdout)
    (work / "stderr.txt").write_text(result.stderr)
    lines = [l for l in result.stdout.splitlines() if l.startswith("CERTG_JSON=")]
    assert result.returncode == 0 and lines, result.stderr[-4000:]
    return json.loads(lines[-1][len("CERTG_JSON="):])
