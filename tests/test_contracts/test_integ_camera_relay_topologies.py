"""Camera-stack review LOW 4: the relay's peer check, in both deployment topologies.

Single container: go2rtc and the API share loopback. Enterprise compose: go2rtc is its
own container, so it reaches the relay at ODIN_CAMERA_RELAY_BASE_URL and connects from
the go2rtc service address, trusted through ODIN_CAMERA_RELAY_PEERS. The HMAC token is
required in both, and any proxy/CDN header refuses the request.
"""
from pathlib import Path

import pytest
import yaml

from tests.test_contracts.test_cert_A_camera_relay import _set_camera
from tests.test_contracts.test_cert_A_harness import (  # noqa: F401
    bind_engine, cert_app, cert_client, fernet_env, review_db,
)

ROOT = Path(__file__).resolve().parents[2]


def _call(review_db, monkeypatch, tmp_path, client, headers=None, token_ok=True):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from modules.printers import camera_relay
    bind_engine(monkeypatch, review_db.get_bind())
    monkeypatch.setenv("ODIN_CAMERA_RELAY_SECRET_FILE", str(tmp_path / "relay-secret"))
    camera_relay.rotate_relay_secret()
    _set_camera(review_db, "http://169.254.169.254/stream")  # authorised requests then stop at the device policy: 502
    app = FastAPI()
    app.include_router(camera_relay.router)
    token = camera_relay.relay_token(301 if token_ok else 302)
    return TestClient(app, client=client).get(f"/internal/camera-source/301/{token}", headers=headers or {},
                                               follow_redirects=False).status_code


@pytest.mark.parametrize("header", ["Via", "CF-Connecting-IP", "True-Client-IP", "X-Client-IP"])
def test_loopback_request_carrying_a_proxy_header_is_refused(cert_app, review_db, fernet_env, monkeypatch, tmp_path, header):
    assert _call(review_db, monkeypatch, tmp_path, ("127.0.0.1", 4000), {header: "203.0.113.9"}) == 404
    assert _call(review_db, monkeypatch, tmp_path, ("127.0.0.1", 4000)) == 502  # control


def test_single_container_topology_trusts_only_loopback(cert_app, review_db, fernet_env, monkeypatch, tmp_path):
    from modules.printers import camera_relay
    monkeypatch.delenv("ODIN_CAMERA_RELAY_BASE_URL", raising=False)
    monkeypatch.delenv("ODIN_CAMERA_RELAY_PEERS", raising=False)
    monkeypatch.setenv("PORT", "8000")
    assert camera_relay.relay_url(301).startswith("http://127.0.0.1:8000/internal/camera-source/301/")
    assert _call(review_db, monkeypatch, tmp_path, ("172.19.0.4", 4000)) == 404


def test_enterprise_topology_trusts_the_configured_go2rtc_peer_with_the_token(cert_app, review_db, fernet_env, monkeypatch,
                                                                            tmp_path):
    import socket
    from modules.printers import camera_relay
    monkeypatch.setenv("ODIN_CAMERA_RELAY_BASE_URL", "http://api:8000/")
    monkeypatch.setenv("ODIN_CAMERA_RELAY_PEERS", "go2rtc, 172.19.0.9")
    real = socket.getaddrinfo
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, *a, **k:
                        [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("172.19.0.4", 0))] if host == "go2rtc" else real(host, *a, **k))
    assert camera_relay.relay_url(301).startswith("http://api:8000/internal/camera-source/301/")
    assert _call(review_db, monkeypatch, tmp_path, ("172.19.0.4", 4000)) == 502   # go2rtc service: authorised
    assert _call(review_db, monkeypatch, tmp_path, ("172.19.0.9", 4000)) == 502   # literal peer
    assert _call(review_db, monkeypatch, tmp_path, ("172.19.0.4", 4000), token_ok=False) == 404  # token still required
    assert _call(review_db, monkeypatch, tmp_path, ("172.19.0.5", 4000)) == 404   # another container
    assert _call(review_db, monkeypatch, tmp_path, ("172.19.0.4", 4000), {"Via": "1.1 proxy"}) == 404


def test_enterprise_compose_wires_relay_base_url_peers_and_shared_secret():
    compose = yaml.safe_load((ROOT / "docker-compose.enterprise.yml").read_text())
    for service in ("api", "monitors"):
        env = compose["services"][service]["environment"]
        assert env["ODIN_CAMERA_RELAY_BASE_URL"] == "http://api:8000"
        assert env["ODIN_CAMERA_RELAY_PEERS"] == "go2rtc"
        assert env["ODIN_CAMERA_RELAY_SECRET_FILE"].startswith("/data/")
