"""Enterprise compose: go2rtc is its own service.

The API, monitors and vision reach go2rtc at ODIN_GO2RTC_API_URL (http://go2rtc:1984) on
the compose network instead of 127.0.0.1:1984. The config is shared through the
go2rtc-config volume, and go2rtc is reloaded through its own authenticated
POST /api/restart (there is no supervisord go2rtc program in the API container). go2rtc's
API requires per-boot credentials off loopback, RTSP stays on go2rtc's loopback with its
per-boot credentials, and neither is published. The live run is
test_integ_enterprise_go2rtc_live.py.
"""
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
PRINTERS = [SimpleNamespace(id=4, api_type="generic", api_host=None, api_key=None,
                            camera_url="rtsp://192.168.1.40:554/s", model=None)]


@pytest.fixture
def secret(tmp_path, monkeypatch):
    from modules.printers import camera_relay
    monkeypatch.setenv("ODIN_CAMERA_RELAY_SECRET_FILE", str(tmp_path / "relay-secret"))
    camera_relay.rotate_relay_secret()
    return camera_relay


@pytest.fixture
def remote(monkeypatch, secret):
    monkeypatch.setenv("ODIN_GO2RTC_API_URL", "http://go2rtc:1984")
    return secret


def test_single_container_config_keeps_go2rtc_api_on_loopback(secret, monkeypatch):
    from modules.printers import go2rtc_access, route_utils
    monkeypatch.delenv("ODIN_GO2RTC_API_URL", raising=False)
    config = route_utils.go2rtc_config_for(PRINTERS, "192.168.1.10")
    assert config["api"]["listen"] == "127.0.0.1:1984"
    assert go2rtc_access.go2rtc_is_remote() is False and go2rtc_access.go2rtc_client_kwargs() == {}


def test_enterprise_config_listens_on_the_service_network_with_per_boot_credentials(remote):
    from modules.printers import go2rtc_access, route_utils
    config = route_utils.go2rtc_config_for(PRINTERS, "192.168.1.10")
    user, password = go2rtc_access.go2rtc_api_credentials()
    assert config["api"] == {"listen": ":1984", "username": user, "password": password}
    assert re.fullmatch(r"[0-9a-f]{64}", password)
    # RTSP is consumed only by go2rtc's own ffmpeg, inside the go2rtc container.
    assert config["rtsp"]["listen"] == "127.0.0.1:8554" and config["rtsp"]["password"]
    assert go2rtc_access.go2rtc_client_kwargs() == {"auth": (user, password)}
    remote.rotate_relay_secret()
    assert go2rtc_access.go2rtc_api_credentials()[1] != password  # per boot


def _fake_go2rtc(monkeypatch, accepted):
    import httpx
    calls = []

    def post(url, auth=None, timeout=None):
        calls.append((url, auth))
        return SimpleNamespace(status_code=200 if auth in accepted else 401)
    monkeypatch.setattr(httpx, "post", post)
    return calls


def test_enterprise_config_advertises_only_the_configured_host_ip(remote, monkeypatch):
    """Every writer (api, monitors) must produce the same config: a writer's own container
    address is never go2rtc's WebRTC address."""
    from modules.printers import route_utils
    monkeypatch.setattr(route_utils, "_get_lan_ip", lambda: "172.19.0.5")
    assert "candidates" not in route_utils.go2rtc_config_for(PRINTERS, None)["webrtc"]
    assert route_utils.go2rtc_config_for(PRINTERS, "192.168.1.10")["webrtc"]["candidates"] == ["192.168.1.10:8555"]
    monkeypatch.delenv("ODIN_GO2RTC_API_URL")
    assert route_utils.go2rtc_config_for(PRINTERS, None)["webrtc"]["candidates"] == ["172.19.0.5:8555"]


def test_enterprise_reload_uses_go2rtc_api_with_the_credentials_it_runs_with(remote, tmp_path, monkeypatch):
    """After an API restart go2rtc still runs the previous boot's config: the reload
    authenticates with the previous credentials and never calls supervisorctl."""
    import subprocess
    from modules.printers import go2rtc_access, route_utils
    path, lock = str(tmp_path / "go2rtc.yaml"), str(tmp_path / "go2rtc.lock")
    running = go2rtc_access.go2rtc_api_credentials()
    remote.rotate_relay_secret()  # API container restarted
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("supervisorctl has no go2rtc program here"))
    calls = _fake_go2rtc(monkeypatch, [running])
    route_utils._go2rtc_restart_with_lock(route_utils.go2rtc_config_for(PRINTERS, "192.168.1.10"),
                                          _path=path, _lock_path=lock)
    assert [url for url, _ in calls] == ["http://go2rtc:1984/api/restart"] * 2
    assert calls[-1][1] == running
    state = route_utils._go2rtc_read_state(path)
    assert state.get("applied"), state


def test_enterprise_reload_failure_stays_pending(remote, tmp_path, monkeypatch):
    from modules.printers import route_utils
    path, lock = str(tmp_path / "go2rtc.yaml"), str(tmp_path / "go2rtc.lock")
    scheduled = []
    monkeypatch.setattr(route_utils, "_go2rtc_schedule_retry", lambda *a: scheduled.append(a))
    _fake_go2rtc(monkeypatch, [])
    route_utils._go2rtc_restart_with_lock(route_utils.go2rtc_config_for(PRINTERS, "192.168.1.10"),
                                          _path=path, _lock_path=lock)
    assert "applied" not in route_utils._go2rtc_read_state(path) and scheduled


def test_enterprise_prestart_write_leaves_the_running_go2rtc_to_be_reloaded(remote, tmp_path):
    from modules.printers import route_utils
    path = str(tmp_path / "go2rtc.yaml")
    route_utils.write_go2rtc_config_before_start(route_utils.go2rtc_config_for(PRINTERS, None), _path=path)
    assert Path(path).exists() and "applied" not in route_utils._go2rtc_read_state(path)


def test_rotation_keeps_exactly_the_previous_secret(secret, tmp_path):
    first = secret.relay_secret()
    secret.rotate_relay_secret()
    assert secret.previous_relay_secret() == first and secret.relay_secret() != first
    assert oct(Path(str(tmp_path / "relay-secret.previous")).stat().st_mode)[-3:] == "600"


def test_frame_consumers_use_the_configured_go2rtc_with_credentials(remote, monkeypatch):
    import importlib
    import httpx
    from modules.printers import go2rtc_access
    from modules.vision import detection_thread
    importlib.reload(detection_thread)
    try:
        calls = []
        monkeypatch.setattr(httpx, "get", lambda url, **kw: calls.append((url, kw)) or SimpleNamespace(status_code=500))
        worker = detection_thread.PrinterVisionThread(9, "cam", None, {}, 2, 33, "RUNNING")
        worker._capture_frame()
        assert calls == [("http://go2rtc:1984/api/frame.jpeg?src=printer_9",
                          {"timeout": 5, "auth": go2rtc_access.go2rtc_api_credentials()})]
    finally:
        monkeypatch.delenv("ODIN_GO2RTC_API_URL")
        importlib.reload(detection_thread)
    from modules.archives import timelapse_capture
    source = Path(timelapse_capture.__file__).read_text()
    assert "auth=go2rtc_client_kwargs().get('auth')" in source and "GO2RTC_BASE = go2rtc_api_url()" in source


def test_no_backend_code_hardcodes_go2rtc_loopback_api():
    offenders = []
    for path in (ROOT / "backend").rglob("*.py"):
        rel = path.relative_to(ROOT).as_posix()
        if "/schema/preserved" in rel or rel.endswith("go2rtc_access.py"):
            continue
        for number, line in enumerate(path.read_text().splitlines(), 1):
            if re.search(r"(127\.0\.0\.1|localhost):1984", line) and not line.lstrip().startswith("#"):
                offenders.append(f"{rel}:{number}")
    assert offenders == []


def test_go2rtc_service_address_is_an_own_endpoint_for_the_device_policy(remote, monkeypatch):
    import socket
    from core import webhook_utils
    real = webhook_utils._real_getaddrinfo
    monkeypatch.setattr(webhook_utils, "_real_getaddrinfo", lambda host, *a, **k:
                        [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("172.19.0.7", 0))] if host == "go2rtc" else real(host, *a, **k))
    assert webhook_utils.is_own_service_endpoint("172.19.0.7", 1984)
    assert webhook_utils.is_own_service_endpoint("172.19.0.7", 8554)
    assert not webhook_utils.is_own_service_endpoint("172.19.0.8", 1984)


def _compose():
    return yaml.safe_load((ROOT / "docker-compose.enterprise.yml").read_text())["services"]


def test_enterprise_compose_shares_the_go2rtc_config_and_points_at_the_service():
    services = _compose()
    for name in ("api", "monitors"):
        assert "go2rtc-config:/app/go2rtc" in services[name]["volumes"], name
        env = services[name]["environment"]
        assert env["ODIN_GO2RTC_API_URL"] == "http://go2rtc:1984"
        assert env["GO2RTC_LOCKFILE"].startswith("/app/go2rtc/")
    assert services["monitors"]["environment"]["ODIN_HOST_IP"] == services["api"]["environment"]["ODIN_HOST_IP"]
    vision = services["vision"]["environment"]
    assert vision["ODIN_GO2RTC_API_URL"] == "http://go2rtc:1984"
    assert vision["ODIN_CAMERA_RELAY_SECRET_FILE"] == services["api"]["environment"]["ODIN_CAMERA_RELAY_SECRET_FILE"]
    go2rtc = services["go2rtc"]
    assert go2rtc["volumes"] == ["go2rtc-config:/config:ro"]
    assert go2rtc["depends_on"] == {"api": {"condition": "service_healthy"}}
    assert go2rtc["ports"] == ["8555:8555"] and go2rtc["expose"] == ["1984"]


def test_entrypoint_rotates_once_for_the_enterprise_api_too():
    entrypoint = (ROOT / "docker" / "entrypoint.sh").read_text()
    block = entrypoint[entrypoint.index('case "$*" in'):]
    assert "*/etc/supervisor/conf.d/api.conf*|*/etc/supervisor/conf.d/odin.conf*)" in block
    assert "monitors.conf" not in block.split(")")[0]
