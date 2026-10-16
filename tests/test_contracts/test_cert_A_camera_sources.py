"""Group A: go2rtc camera sources are validated IP literals, RTSP only."""
import socket

import pytest
from sqlalchemy import text

from tests.test_contracts.test_cert_A_harness import (  # noqa: F401
    cert_app, cert_client, fernet_env, review_db,
)


@pytest.fixture
def dns(monkeypatch):
    import core.webhook_utils as webhook_utils
    real = webhook_utils._real_getaddrinfo
    names = {}

    def fake(host, port, *args, **kwargs):
        key = webhook_utils.normalize_hostname(host)
        if key in names:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (names[key], port or 0))]
        return real(host, port, *args, **kwargs)
    monkeypatch.setattr(webhook_utils, "_real_getaddrinfo", fake)
    return names


@pytest.fixture
def written(monkeypatch):
    import modules.printers.route_utils as route_utils
    configs = []
    monkeypatch.setattr(route_utils, "_go2rtc_restart_with_lock", lambda config, **kw: configs.append(config))
    return configs


def test_source_uses_validated_ip_literal_and_keeps_credentials_and_path(dns):
    from modules.printers.route_utils import go2rtc_stream_source
    dns["camera-room.test"] = "192.168.60.7"
    assert go2rtc_stream_source("rtsp://viewer:p%40ss@Camera-Room.TEST:8554/live/main?ch=1") == \
        "rtsp://viewer:p%40ss@192.168.60.7:8554/live/main?ch=1"
    assert go2rtc_stream_source("rtsps://u:p@camera-room.test:7441/x?enableSrtp") == "rtspx://u:p@192.168.60.7:7441/x"


@pytest.mark.parametrize("url", ["http://192.168.60.7/stream", "https://192.168.60.7/x", "ffmpeg:http://192.168.60.7/x",
                                 "rtsp://127.0.0.1/x", "rtsp://169.254.169.254/x", "rtsp://loopback-camera.test/x"])
def test_http_and_blocked_sources_are_never_streamed(dns, url):
    from modules.printers.route_utils import go2rtc_stream_source
    dns["loopback-camera.test"] = "127.0.0.1"
    assert go2rtc_stream_source(url) is None


def test_go2rtc_config_follows_dns_changes_on_resync(cert_app, review_db, dns, written, monkeypatch):
    import core.crypto as crypto
    from modules.printers.route_utils import sync_go2rtc_config_raw
    from tests.test_contracts.test_cert_A_harness import bind_engine
    bind_engine(monkeypatch, review_db.get_bind())
    dns["shop-camera.test"] = "192.168.60.7"
    review_db.execute(text("UPDATE printers SET camera_url=:u,camera_enabled=1,is_active=1 WHERE id=301"),
                      {"u": crypto.encrypt("rtsp://cam:pw@shop-camera.test:554/live")})
    review_db.commit()
    sync_go2rtc_config_raw()
    assert written[-1]["streams"]["printer_301"] == "rtsp://cam:pw@192.168.60.7:554/live"
    dns["shop-camera.test"] = "192.168.60.9"  # DHCP/DNS moved the camera
    sync_go2rtc_config_raw()
    assert written[-1]["streams"]["printer_301"] == "rtsp://cam:pw@192.168.60.9:554/live"
    dns["shop-camera.test"] = "127.0.0.1"  # rebinding to loopback drops the stream
    sync_go2rtc_config_raw()
    assert "printer_301" not in written[-1]["streams"]


def test_monitor_daemon_resyncs_camera_sources_periodically():
    from modules.printers.monitors import mqtt_monitor
    import inspect
    assert mqtt_monitor.CAMERA_RESOLUTION_INTERVAL <= 300
    assert "sync_go2rtc_config_raw" in inspect.getsource(mqtt_monitor.MQTTMonitorDaemon)
