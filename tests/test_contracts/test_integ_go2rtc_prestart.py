"""HIGH 1 follow-up: per-boot go2rtc RTSP credentials without restarting go2rtc at start.

The container entrypoint rotates the relay secret and writes the go2rtc config (marked
applied) before supervisord starts go2rtc; the API then does not rotate again, so its
start-up sync finds the same configuration and restarts nothing."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_prestart_write_marks_config_applied_so_startup_sync_does_not_restart(tmp_path, monkeypatch):
    import subprocess
    from types import SimpleNamespace
    from modules.printers import camera_relay, route_utils
    monkeypatch.setenv("ODIN_CAMERA_RELAY_SECRET_FILE", str(tmp_path / "relay-secret"))
    path, lock = str(tmp_path / "go2rtc.yaml"), str(tmp_path / "go2rtc.lock")
    camera_relay.rotate_relay_secret()
    printers = [SimpleNamespace(id=4, api_type="generic", api_host=None, api_key=None,
                                camera_url="rtsp://192.168.1.40:554/s", model=None)]
    config = route_utils.go2rtc_config_for(printers, "192.168.1.10")
    route_utils.write_go2rtc_config_before_start(config, _path=path)
    restarts = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: restarts.append(a) or SimpleNamespace(returncode=0))
    # The API's start-up sync with the same secret (ODIN_CAMERA_RELAY_SECRET_ROTATED=1).
    route_utils._go2rtc_restart_with_lock(route_utils.go2rtc_config_for(printers, "192.168.1.10"), _path=path, _lock_path=lock)
    assert restarts == []
    # A rotation after go2rtc started (no pre-start) does change the config and restarts it.
    camera_relay.rotate_relay_secret()
    route_utils._go2rtc_restart_with_lock(route_utils.go2rtc_config_for(printers, "192.168.1.10"), _path=path, _lock_path=lock)
    assert len(restarts) == 1


def test_entrypoint_prepares_go2rtc_before_supervisord_and_api_skips_rotation():
    entrypoint = (ROOT / "docker" / "entrypoint.sh").read_text()
    # The image always passes CMD (supervisord with odin.conf): the step keys on that config.
    block = entrypoint[entrypoint.index('case "$*" in'):]
    assert "*/etc/supervisor/conf.d/odin.conf*)" in block
    assert block.index("scripts.prepare_go2rtc_config") < block.index('exec "$@"')
    assert "export ODIN_CAMERA_RELAY_SECRET_ROTATED=1" in block
    app = (ROOT / "backend" / "core" / "app.py").read_text()
    assert 'if os.environ.get("ODIN_CAMERA_RELAY_SECRET_ROTATED") != "1":\n                rotate_relay_secret()' in app
