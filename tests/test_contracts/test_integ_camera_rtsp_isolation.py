"""Camera-stack review HIGH 1: go2rtc's RTSP server must not re-serve streams to other orgs.

The exploit: an operator in org A sets camera_url=rtsp://<ODIN's own address>:8554/printer_<id
of org B>, so ODIN's go2rtc pulls org B's stream from its own RTSP server and serves it as
org A's camera. Fixed twice over: go2rtc's RTSP server listens on loopback only and requires
per-boot credentials, and the device policy refuses ODIN's own addresses on ODIN's own
service ports.
"""
from pathlib import Path

import pytest
import yaml
from sqlalchemy import text

from tests.test_contracts.test_cert_A_harness import (  # noqa: F401
    cert_app, cert_client, fernet_env, review_db,
)

ROOT = Path(__file__).resolve().parents[2]
OWN_IP = "192.0.2.77"  # stands in for the container/host address (ODIN_HOST_IP)


@pytest.fixture
def own_address(monkeypatch):
    monkeypatch.setenv("ODIN_HOST_IP", OWN_IP)
    return OWN_IP


def test_go2rtc_rtsp_server_is_loopback_only_and_authenticated():
    from types import SimpleNamespace
    from modules.printers.route_utils import go2rtc_config_for, go2rtc_rtsp_credentials
    printer = SimpleNamespace(id=7, api_type="generic", api_host=None, api_key=None,
                              camera_url="rtsp://192.168.1.40:554/stream", model=None)
    config = go2rtc_config_for([printer], "192.168.1.10")
    user, password = go2rtc_rtsp_credentials()
    assert config["rtsp"] == {"listen": "127.0.0.1:8554", "username": user, "password": password}
    assert user and len(password) >= 32
    # ODIN's own MJPEG variant reads go2rtc's RTSP server with those credentials.
    assert config["streams"]["printer_7_mjpeg"] == f"ffmpeg:rtsp://{user}:{password}@127.0.0.1:8554/printer_7#video=mjpeg"


def test_shipped_default_go2rtc_config_binds_rtsp_to_loopback():
    config = yaml.safe_load((ROOT / "docker" / "go2rtc.yaml").read_text())
    assert config["rtsp"]["listen"] == "127.0.0.1:8554"
    assert config["api"]["listen"] == "127.0.0.1:1984"


@pytest.mark.parametrize("port", [8554, 8555, 1984, 8000])
def test_reviewer_exploit_own_address_on_odin_port_is_refused(cert_app, review_db, own_address, monkeypatch, port):
    import modules.printers.route_utils as route_utils
    monkeypatch.setattr(route_utils, "_go2rtc_restart_with_lock", lambda config, **kw: None)
    operator = cert_app.roles["operator"]
    response = cert_app.patch("/printers/302", headers=operator,
                              json={"camera_url": f"rtsp://{own_address}:{port}/printer_301"})
    assert response.status_code == 400, response.text
    stored = review_db.execute(text("SELECT camera_url FROM printers WHERE id=302")).scalar_one()
    assert stored is None
    # Even a value that bypassed the route (e.g. written before this fix) never reaches go2rtc.
    assert route_utils.go2rtc_stream_source(f"rtsp://{own_address}:{port}/printer_301", 302) is None


def test_own_address_on_other_ports_and_other_hosts_stay_allowed(cert_app, own_address, monkeypatch):
    import modules.printers.route_utils as route_utils
    monkeypatch.setattr(route_utils, "_go2rtc_restart_with_lock", lambda config, **kw: None)
    operator = cert_app.roles["operator"]
    # A camera served by the ODIN host itself on its own port (e.g. a USB webcam streamer).
    assert cert_app.patch("/printers/302", headers=operator,
                          json={"camera_url": f"rtsp://{own_address}:554/cam"}).status_code == 200
    assert cert_app.patch("/printers/302", headers=operator,
                          json={"camera_url": "rtsp://192.168.1.41:8554/cam"}).status_code == 200


def test_container_interfaces_and_gateway_are_enumerated_at_validation_time(monkeypatch):
    from core import webhook_utils
    monkeypatch.delenv("ODIN_HOST_IP", raising=False)
    monkeypatch.setattr(webhook_utils, "_proc_ipv4_local", lambda: {"172.18.0.5"})
    monkeypatch.setattr(webhook_utils, "_proc_ipv6_local", lambda: {"fd00:18::5"})
    monkeypatch.setattr(webhook_utils, "_proc_default_gateways", lambda: {"172.18.0.1"})
    for ip in ("172.18.0.5", "fd00:18::5", "172.18.0.1", "::ffff:172.18.0.5"):
        assert webhook_utils.is_own_service_endpoint(ip, 8554), ip
    assert not webhook_utils.is_own_service_endpoint("172.18.0.9", 8554)
    assert not webhook_utils.is_own_service_endpoint("172.18.0.5", 554)
    with pytest.raises(webhook_utils.DeviceDestinationBlocked):
        webhook_utils.device_destination_ips("172.18.0.5", 8554)
    monkeypatch.setattr(webhook_utils, "_proc_ipv4_local", lambda: set())
    monkeypatch.setattr(webhook_utils, "_proc_default_gateways", lambda: set())
    assert not webhook_utils.is_own_service_endpoint("172.18.0.5", 8554)  # not cached
