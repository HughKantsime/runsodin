"""TEST ONLY helper: exempt exactly 127.0.0.1 and ::1 from the device destination policy.

Production blocks loopback device destinations. Tests whose device double is
a real server on 127.0.0.1 use allow_loopback_devices(monkeypatch); it patches
this process and, through a test-only sitecustomize on PYTHONPATH, any worker
subprocess the test starts.
"""
import os
from pathlib import Path

SITE_DIR = Path(__file__).resolve().parent / "loopback_device_policy"


def allow_loopback_devices(monkeypatch):
    import core.webhook_utils as webhook_utils
    real_blocked = webhook_utils.is_blocked_device_address
    monkeypatch.setattr(webhook_utils, "is_blocked_device_address",
                        lambda ip: False if str(ip) in ("127.0.0.1", "::1") else real_blocked(ip))
    # sitecustomize runs before multiprocessing restores the parent sys.path.
    # Make core importable at interpreter startup so the exemption is installed.
    existing = os.environ.get("PYTHONPATH", "")
    backend = Path(__file__).resolve().parents[2] / "backend"
    paths = [str(SITE_DIR), str(backend)]
    if existing:
        paths.append(existing)
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(paths))
