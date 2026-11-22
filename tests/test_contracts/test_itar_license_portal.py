"""The stock license address is never contacted by an ITAR installation."""
import asyncio
import logging
import socket
import time
from unittest.mock import Mock
from urllib.parse import urlsplit

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from core import config, db, itar
from core.app import create_app
from core.db import get_db
from core.dependencies import get_current_user
from modules.system import routes_health
from tests.test_contracts.test_education_review_workflow import review_db

WARNING = ("ITAR mode: the license portal is off (no license server address is set). "
           "Licences are activated from a file.")
DETAIL = "Online licence activation is off while ITAR mode is on. Upload a licence file instead."


@pytest.fixture
def configure(monkeypatch):
    monkeypatch.setenv("ODIN_ITAR_MODE", "1")
    monkeypatch.delenv("LICENSE_SERVER_URL", raising=False)

    def load(address=None, env_file=None):
        if address is not None:
            monkeypatch.setenv("LICENSE_SERVER_URL", address)
        loaded = config.Settings(_env_file=env_file)
        # Keep the singleton that the route modules imported, with fresh provenance.
        monkeypatch.setattr(config.settings, "__pydantic_fields_set__", loaded.model_fields_set.copy())
        monkeypatch.setitem(config.settings.__dict__, "license_server_url", loaded.license_server_url)
        return loaded

    return load


@pytest.fixture
def network(monkeypatch):
    dns = Mock(return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))])
    http = Mock(return_value=httpx.Response(200, content=b"ok"))
    connect = Mock(side_effect=AssertionError("unexpected socket connection"))
    monkeypatch.setattr(socket, "getaddrinfo", dns)
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", http)
    return dns, http, connect


@pytest.fixture
def admin_client(review_db, monkeypatch):
    monkeypatch.setattr(db, "SessionLocal", sessionmaker(bind=review_db.get_bind()))

    def client(app):
        app.dependency_overrides[get_current_user] = lambda: {"id": 1, "role": "admin", "group_id": None}
        app.dependency_overrides[get_db] = lambda: review_db
        return TestClient(app)  # No lifespan workers or hardware.

    return client


@pytest.mark.parametrize("address", [None, "", " \t "])
def test_stock_boot_and_online_routes_stay_offline(configure, network, admin_client, address, caplog, monkeypatch):
    configure(address)
    dns, http, connect = network
    with caplog.at_level(logging.WARNING):
        client = admin_client(create_app())
    assert [(r.levelno, r.message) for r in caplog.records if r.message == WARNING] == [(logging.WARNING, WARNING)]

    monkeypatch.setattr(routes_health, "_license_probe_cache",
                        {"checked_at": time.time(), "reachable": True, "detail": "previous probe"})
    status = client.get("/api/system/license-server-status")
    assert status.status_code == 200
    assert status.json()["reachable"] is False
    assert status.json()["detail"] == "off in ITAR mode"
    for action, body in [("activate", {"key": "test-key"}), ("trial-request", {"email": "test@example.test"}),
                         ("unactivate", {}), ("reactivate", {})]:
        response = client.post("/api/license/" + action, json=body)
        assert (response.status_code, response.json()["detail"]) == (409, DETAIL), action
    with pytest.raises(HTTPException) as exc:
        asyncio.run(routes_health._fetch_license_challenge(config.settings.license_server_url, "test-id"))
    assert (exc.value.status_code, exc.value.detail) == (409, DETAIL)
    dns.assert_not_called()
    http.assert_not_called()
    connect.assert_not_called()


@pytest.mark.parametrize("source", ["environment", "file"])
def test_explicit_public_default_refuses_boot(configure, network, source, tmp_path):
    default = config.Settings.model_fields["license_server_url"].default
    if source == "environment":
        configure(default)
    else:
        env_file = tmp_path / "settings.conf"
        env_file.write_text(f"license_server_url={default}\n")
        configure(None, env_file)
    with pytest.raises(RuntimeError, match="following outbound destinations resolve to public addresses") as exc:
        create_app()
    assert default in str(exc.value)
    dns, http, connect = network
    dns.assert_called_once_with(urlsplit(default).hostname, None)
    http.assert_not_called()
    connect.assert_not_called()


def test_disabling_portal_still_audits_other_destinations(configure, monkeypatch):
    configure()
    monkeypatch.setenv("ODIN_GO2RTC_API_URL", "https://8.8.8.8")
    with pytest.raises(RuntimeError, match=r"8\.8\.8\.8"):
        create_app()


def test_explicit_loopback_starts_and_reaches_portal(configure, network, admin_client, monkeypatch):
    address = "http://127.0.0.1:8090"
    configure(address)
    monkeypatch.setattr(routes_health, "_license_probe_cache", {"checked_at": 0, "reachable": None, "detail": ""})
    result = admin_client(create_app()).get("/api/system/license-server-status")
    assert result.status_code == 200 and result.json()["reachable"] is True
    _, http, _ = network
    http.assert_called_once()
    assert str(http.call_args.args[0].url) == address + "/api/v1/health"


def test_offline_trial_still_returns_a_request_code(configure, network, admin_client, tier_license):
    configure()
    tier_license(None)
    response = admin_client(create_app()).post(
        "/api/license/trial-request", json={"offline": True, "email": "test@example.test"},
    )
    assert response.status_code == 200
    assert response.json()["request_code"].startswith("ODINTRIAL1.")
    for recorder in network:
        recorder.assert_not_called()


def test_itar_off_preserves_blank_environment_override(configure, monkeypatch, tmp_path):
    monkeypatch.setenv("ODIN_ITAR_MODE", "0")
    env_file = tmp_path / "settings.conf"
    env_file.write_text("LICENSE_SERVER_URL=http://127.0.0.1\n")
    assert configure("", env_file).license_server_url == ""
    assert not itar.license_portal_off()
