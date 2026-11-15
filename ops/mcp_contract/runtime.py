"""Process-local isolation for the disposable MCP harness; never imported by ODIN."""

import base64
import json
import os
from pathlib import Path
import secrets
import sys
from datetime import datetime, timedelta, timezone

ROOT = Path(__file__).resolve().parents[2]


def database_url(out):
    return f"sqlite:///{out / 'odin.db'}"


def refuse_database_environment(out):
    # Check before clearing the environment or importing anything that opens a DB.
    if any(value and value != database_url(out) for key, value in os.environ.items()
           if key.upper().endswith("DATABASE_URL")):
        raise ValueError("Refusing a database URL outside this run's temporary database.")


def loopback_only(event, args):
    """Deny DNS and socket traffic outside literal IPv4 loopback, before I/O."""
    if event == "socket.getaddrinfo":
        host = args[0]
    elif event in {"socket.connect", "socket.bind", "socket.sendto"}:
        address = args[-1]
        host = address[0] if isinstance(address, tuple) else None
    else:
        return
    if host != "127.0.0.1":
        raise PermissionError("MCP contract harness permits only 127.0.0.1 connections.")


def configure(out):
    """Call once, in a fresh process, before any backend imports."""
    refuse_database_environment(out)
    if "core.db" in sys.modules:
        raise RuntimeError("Start the fixture in a fresh Python process.")
    # Do not inherit credentials, proxy settings, integrations, Python hooks, or
    # deployment paths. Working in the private directory also prevents .env reads.
    path = os.environ.get("PATH", os.defpath)
    os.environ.clear()
    os.environ.update({
        "PATH": path, "HOME": str(out), "TMPDIR": str(out),
        "DATABASE_URL": database_url(out), "JWT_SECRET_KEY": secrets.token_urlsafe(48),
        "API_KEY": secrets.token_urlsafe(32), "COOKIE_SECURE": "false",
        "LICENSE_DIR": str(out), "ODIN_LICENSE_FILE": str(out / "odin.license"),
        "LICENSE_SERVER_URL": "http://127.0.0.1", "ODIN_ITAR_MODE": "1",
        "ODIN_BRANDING_DIRECTORY": str(out / "branding"),
        "NO_PROXY": "127.0.0.1", "PYTHONDONTWRITEBYTECODE": "1",
    })
    os.chdir(out)
    sys.path[:0] = [str(ROOT), str(ROOT / "backend")]
    sys.dont_write_bytecode = True
    sys.addaudithook(loopback_only)


def enable_test_license(out):
    """Exercise real license verification using an ephemeral test trust root."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    import license_manager

    key = Ed25519PrivateKey.generate()
    license_manager.ODIN_PUBLIC_KEY = key.public_key().public_bytes(
        Encoding.PEM, PublicFormat.SubjectPublicKeyInfo).decode()
    now = datetime.now(timezone.utc)
    payload = json.dumps({
        "tier": "pro", "licensee": "Disposable contract test",
        "issued_at": now.isoformat(), "expires_at": (now + timedelta(days=1)).isoformat(),
        "features": ["job_approval", "maintenance"],
    }).encode()
    envelope = b".".join(base64.urlsafe_b64encode(part) for part in (payload, key.sign(payload)))
    (out / "odin.license").write_bytes(envelope)
    info = license_manager.get_license()
    if not info.valid or not all(info.has_feature(f) for f in ("job_approval", "maintenance")):
        raise RuntimeError("Could not enable the disposable test license.")


def allow_simulator(printer_port):
    """Use ODIN's existing test-only loopback seam, limited to our owned peer."""
    import runpy
    from core import webhook_utils

    runpy.run_path(str(ROOT / "tests/support/loopback_device_policy/sitecustomize.py"))
    resolve = webhook_utils.device_destination_ips

    def simulator_only(host, port=None):
        hostname, embedded_port = webhook_utils.split_device_host(host)
        if hostname != "127.0.0.1" or (port or embedded_port) != printer_port:
            raise webhook_utils.DeviceDestinationBlocked("Only this run's simulator is allowed.")
        return resolve(host, port)

    webhook_utils.device_destination_ips = simulator_only


def create_test_app():
    from core.app import create_app
    from contextlib import asynccontextmanager

    app = create_app()

    @asynccontextmanager
    async def quiet_lifespan(app):
        # Registration, routes, middleware, auth, and writes remain real. Normal
        # lifespan wires notification subscribers and starts background writers.
        try:
            yield
        finally:
            app.state.branding_assets.close()

    app.router.lifespan_context = quiet_lifespan
    return app
