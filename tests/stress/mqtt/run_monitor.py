"""Wrapper around `modules.printers.monitors.mqtt_monitor` that resolves
the synthetic-printer broker hostname `mosquitto` to 127.0.0.1 without
needing sudo / /etc/hosts.

Stress-test only. Production monitor process is `python -m
modules.printers.monitors.mqtt_monitor` directly — no monkey-patching.
"""

from __future__ import annotations

import os
import socket

_REMAP = {
    "mosquitto": os.environ.get("ODIN_STRESS_BROKER_IP", "127.0.0.1"),
}

_orig_getaddrinfo = socket.getaddrinfo


def _patched_getaddrinfo(host, *args, **kwargs):
    if isinstance(host, str) and host in _REMAP:
        host = _REMAP[host]
    return _orig_getaddrinfo(host, *args, **kwargs)


socket.getaddrinfo = _patched_getaddrinfo

# Mirror the backend's instrumentation hook so monitor-process callsites
# (the actual hot path) get measured too.
_stress_dir = os.environ.get("ODIN_STRESS_INSTRUMENTATION")
if _stress_dir:
    from tests.stress.mqtt.instrument import install as _install_stress
    _install_stress(_stress_dir)

from modules.printers.monitors.mqtt_monitor import main  # noqa: E402

if __name__ == "__main__":
    main()
