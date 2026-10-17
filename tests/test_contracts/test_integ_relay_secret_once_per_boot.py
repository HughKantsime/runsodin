"""Review fix O8: the camera relay secret rotates at most once per boot and never from a
non-API process.

If the entrypoint's pre-start step failed, every uvicorn worker rotated the secret at
start-up (each invalidating the others' relay tokens), and relay_secret() rotated from any
process that found no secret file, monitors included. Rotation now holds a file lock and a
boot marker (ODIN_BOOT_ID, exported by the entrypoint): a second rotation in the same boot
returns the current secret. relay_secret() in a non-API process (ODIN_DB_ROLE other than
api) fails closed instead of rotating."""
import multiprocessing
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def secret_file(tmp_path, monkeypatch):
    monkeypatch.setenv("ODIN_CAMERA_RELAY_SECRET_FILE", str(tmp_path / "relay-secret"))
    monkeypatch.delenv("ODIN_DB_ROLE", raising=False)
    from modules.printers import camera_relay
    monkeypatch.setattr(camera_relay, "_process_secret", None)
    return tmp_path / "relay-secret"


def test_second_rotation_in_the_same_boot_keeps_the_secret(secret_file, monkeypatch):
    from modules.printers.camera_relay import relay_secret, rotate_relay_secret
    monkeypatch.setenv("ODIN_BOOT_ID", "boot-a")
    first = rotate_relay_secret()
    assert rotate_relay_secret() == first and relay_secret() == first  # e.g. a second uvicorn worker
    monkeypatch.setenv("ODIN_BOOT_ID", "boot-b")
    assert rotate_relay_secret() != first  # next boot rotates


def _rotate(path, boot, queue):
    import os
    os.environ["ODIN_CAMERA_RELAY_SECRET_FILE"] = path
    os.environ["ODIN_BOOT_ID"] = boot
    os.environ.pop("ODIN_DB_ROLE", None)
    from modules.printers.camera_relay import rotate_relay_secret
    queue.put(rotate_relay_secret())


def test_concurrent_workers_in_one_boot_rotate_once(secret_file):
    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    workers = [ctx.Process(target=_rotate, args=(str(secret_file), "boot-c", queue)) for _ in range(4)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(60)
    values = {queue.get(timeout=5) for _ in workers}
    assert len(values) == 1 and secret_file.read_text().strip() in values


def test_non_api_process_never_rotates_and_fails_closed(secret_file, monkeypatch):
    from modules.printers import camera_relay
    monkeypatch.setenv("ODIN_DB_ROLE", "monitor-bambu")
    with pytest.raises(camera_relay.RelaySecretUnavailable):
        camera_relay.relay_secret()
    assert not secret_file.exists()
    monkeypatch.setenv("ODIN_DB_ROLE", "api")
    value = camera_relay.relay_secret()  # the API may create it
    monkeypatch.setenv("ODIN_DB_ROLE", "monitor-bambu")
    assert camera_relay.relay_secret() == value  # monitors read the shared file


def test_monitor_go2rtc_sync_without_a_secret_writes_nothing(secret_file, monkeypatch, tmp_path):
    from modules.printers import route_utils
    monkeypatch.setenv("ODIN_DB_ROLE", "monitor-bambu")
    writes = []
    monkeypatch.setattr(route_utils, "_go2rtc_restart_with_lock", lambda config, **k: writes.append(config))

    class Cursor:
        def execute(self, *a):
            return self

        def fetchall(self):
            return [(4, "generic", None, None, "rtsp://192.168.1.40:554/s", None)]

        def fetchone(self):
            return None

    class Conn:
        def cursor(self):
            return Cursor()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False
    import core.db_utils
    monkeypatch.setattr(core.db_utils, "get_db", lambda: Conn())
    route_utils.sync_go2rtc_config_raw()
    assert writes == [] and not secret_file.exists()


def test_entrypoint_exports_a_boot_id_before_the_prestart_step():
    entrypoint = (ROOT / "docker" / "entrypoint.sh").read_text()
    assert 'ODIN_BOOT_ID="$(cat /proc/sys/kernel/random/uuid)"' in entrypoint and "export ODIN_BOOT_ID\n" in entrypoint
    assert entrypoint.index("export ODIN_BOOT_ID\n") < entrypoint.index("scripts.prepare_go2rtc_config")
