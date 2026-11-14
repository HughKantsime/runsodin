"""Under ITAR mode go2rtc never starts with a configuration written before this boot.

Review 2026-11-13 finding 5: scripts/prepare_go2rtc_config.py caught every failure and the
entrypoint carried on, so supervisord started go2rtc with the file already on the volume.
An installation that ran with ITAR mode off can hold a public RTSP source there; with ITAR
mode turned on and the rewrite failing (a full volume), vision or a viewer asking the local
go2rtc for that stream made go2rtc contact the public camera.

Now, under ITAR mode, a failed preparation replaces the file with one that has no camera
sources (exit 10, go2rtc may start), and if even that fails the entrypoint refuses to start
the container (exit 11, or any other status). With ITAR mode off nothing changes (exit 1,
file untouched, container starts).

The entrypoint tests run the real shell block from docker/entrypoint.sh with the Python
command replaced by a fixed exit status. The image itself is not built or started here.
"""
import os
import subprocess
from pathlib import Path

import pytest
import yaml

from scripts import prepare_go2rtc_config as prepare

ROOT = Path(__file__).resolve().parents[2]
STALE = "streams:\n  printer_4: rtsp://8.8.8.8:554/live\napi:\n  listen: 127.0.0.1:1984\n"


@pytest.fixture
def stale(tmp_path, monkeypatch):
    """A go2rtc configuration from before ITAR mode, and a preparation that fails."""
    path = tmp_path / "go2rtc.yaml"
    path.write_text(STALE)
    (tmp_path / "go2rtc.yaml.sync-state.json").write_text('{"applied": "abc"}')
    monkeypatch.setenv("GO2RTC_CONFIG", str(path))

    def fail():
        raise OSError(28, "No space left on device")
    from modules.printers import camera_relay
    monkeypatch.setattr(camera_relay, "rotate_relay_secret", fail)
    return path


def test_a_failed_preparation_under_itar_leaves_go2rtc_no_camera_source(stale, monkeypatch):
    monkeypatch.setenv("ODIN_ITAR_MODE", "1")
    assert prepare.main() == prepare.SAFE_TO_START_WITHOUT_CAMERAS == 10
    assert "8.8.8.8" not in stale.read_text()
    config = yaml.safe_load(stale.read_text())
    assert config["streams"] == {} and config["webrtc"]["ice_servers"] == []
    assert config["api"]["listen"] == "127.0.0.1:1984" and config["rtsp"]["listen"] == "127.0.0.1:8554"
    assert not Path(str(stale) + ".sync-state.json").exists(), "the API's first sync must reload go2rtc"
    assert oct(stale.stat().st_mode & 0o777) == "0o600"


def test_a_volume_with_no_room_for_a_second_file_is_still_cleared_in_place(stale, monkeypatch):
    monkeypatch.setenv("ODIN_ITAR_MODE", "1")
    real_open = os.open

    def no_new_files(path, flags, mode=0o777, **kwargs):
        if ".itar-empty." in str(path):
            raise OSError(28, "No space left on device")
        return real_open(path, flags, mode, **kwargs)
    monkeypatch.setattr(os, "open", no_new_files)
    assert prepare.main() == 10
    assert stale.read_text() == prepare.empty_config()


def test_when_the_old_file_cannot_be_replaced_the_status_says_do_not_start(stale, monkeypatch):
    monkeypatch.setenv("ODIN_ITAR_MODE", "1")

    def read_only(*args, **kwargs):
        raise OSError(30, "Read-only file system")
    monkeypatch.setattr(os, "open", read_only)
    assert prepare.main() == prepare.STALE_CONFIGURATION_LEFT == 11
    assert stale.read_text() == STALE


@pytest.mark.parametrize("value", ["", "0", "off"])
def test_with_itar_off_a_failed_preparation_changes_nothing(stale, monkeypatch, value):
    monkeypatch.setenv("ODIN_ITAR_MODE", value)
    assert prepare.main() == 1
    assert stale.read_text() == STALE
    assert Path(str(stale) + ".sync-state.json").exists()


def test_the_empty_configuration_is_the_image_default():
    shipped = yaml.safe_load((ROOT / "docker/go2rtc.yaml").read_text())
    assert yaml.safe_load(prepare.empty_config()) == shipped
    assert shipped["streams"] == {}


# ---- the entrypoint ----

def _entrypoint(itar_mode, status):
    """Run the ITAR block and the go2rtc block of docker/entrypoint.sh, with the preparation
    replaced by `exit status`. Returns (exit code, output)."""
    text = (ROOT / "docker/entrypoint.sh").read_text(encoding="utf-8")
    itar_block = text[text.index("# ITAR-PROXIES-BEGIN"):text.index("# ITAR-PROXIES-END")]
    start = text.rindex('case "$*" in')
    go2rtc_block = text[start:text.index('exec "$@"', start)]
    command = "(cd /app/backend && /usr/sbin/runuser -u odin -- python3 -m scripts.prepare_go2rtc_config)"
    assert go2rtc_block.count(command) == 1
    script = ("set -Eeuo pipefail\n" + itar_block
              + "\nset -- /usr/bin/supervisord -n -c /etc/supervisor/conf.d/odin.conf\n"
              + go2rtc_block.replace(command, f"(exit {status})")
              + '\necho "STARTED rotated=${ODIN_CAMERA_RELAY_SECRET_ROTATED:-0}"\n')
    result = subprocess.run(["/bin/bash", "-c", script], capture_output=True, text=True,
                            env={"PATH": os.environ.get("PATH", ""), "ODIN_ITAR_MODE": itar_mode})
    return result.returncode, result.stdout + result.stderr


@pytest.mark.parametrize("status", [1, 2, 11, 137])
def test_under_itar_the_container_does_not_start_when_the_old_configuration_may_remain(status):
    code, output = _entrypoint("1", status)
    assert code == 1 and "STARTED" not in output
    assert "ODIN_ITAR_MODE=1" in output and "could not be prepared" in output


def test_under_itar_the_container_starts_when_the_configuration_was_emptied():
    code, output = _entrypoint("1", 10)
    assert code == 0 and "STARTED rotated=0" in output, "the API rotates the secret and syncs after it starts"


def test_under_itar_a_prepared_configuration_starts_as_before():
    assert _entrypoint("true", 0) == (0, "STARTED rotated=1\n")


@pytest.mark.parametrize("status", [0, 1, 10, 11, 137])
def test_with_itar_off_the_container_always_starts(status):
    code, output = _entrypoint("", status)
    assert code == 0 and output == f"STARTED rotated={1 if status == 0 else 0}\n"
