"""Claim F20-20.3-10: Default LICENSE_SERVER_URL=https://runsodin.com (configurable).

Executes the real Settings class and the real activate_license route handler
with the outbound HTTP client stubbed (no network, no portal POST).
"""
import asyncio
import contextlib

import pytest
from fastapi import HTTPException


def test_default_license_server_url_is_runsodin(monkeypatch):
    from core.config import Settings

    monkeypatch.delenv("LICENSE_SERVER_URL", raising=False)
    s = Settings(_env_file=None)
    assert s.license_server_url == "https://runsodin.com"


def test_license_server_url_env_override(monkeypatch):
    from core.config import Settings

    monkeypatch.setenv("LICENSE_SERVER_URL", "http://localhost:18999")
    s = Settings(_env_file=None)
    assert s.license_server_url == "http://localhost:18999"


class _Resp:
    status_code = 403
    headers = {"content-type": "application/json"}

    def json(self):
        return {"error": "stub"}


def _run_activate(monkeypatch, url):
    import core.itar as itar
    from modules.system import routes_health as rh

    captured = {}

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, u, json=None):
            captured["url"] = u
            captured["keys"] = sorted(json.keys())
            return _Resp()

    monkeypatch.setattr(rh.settings, "license_server_url", url)
    monkeypatch.setattr(rh.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(itar, "pin_for_request", lambda u: contextlib.nullcontext())
    monkeypatch.setattr(rh, "get_installation_id", lambda: "inst-claims-test")
    monkeypatch.setattr(rh, "get_device_keypair", lambda: (None, "PUBKEY"))

    req = rh.LicenseActivateRequest(key="ODIN-TEST-AAAA-BBBB")
    with pytest.raises(HTTPException) as ei:
        asyncio.run(rh.activate_license(req, current_user={}, license_mutation_allowed=None))
    assert ei.value.status_code == 403
    return captured


def test_activate_route_uses_default_url(monkeypatch):
    from core.config import Settings

    monkeypatch.delenv("LICENSE_SERVER_URL", raising=False)
    default = Settings(_env_file=None).license_server_url
    cap = _run_activate(monkeypatch, default)
    assert cap["url"] == "https://runsodin.com/api/v1/activate"
    assert cap["keys"] == ["device_pubkey", "installation_id", "key"]


def test_activate_route_uses_configured_url(monkeypatch):
    cap = _run_activate(monkeypatch, "https://licenses.example.internal")
    assert cap["url"] == "https://licenses.example.internal/api/v1/activate"
