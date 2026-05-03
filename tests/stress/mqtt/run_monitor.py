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

# Latent ODIN bug: telemetry/* files import as `backend.modules.*` while
# monitor entry imports as `modules.*`. With both paths on sys.path Python
# loads the SAME source twice as two distinct modules → two BambuReportEvent
# classes → V2's `isinstance(item, BambuReportEvent)` shim returns False →
# emit silently drops everything, _on_status never fires, no telemetry.
# Prod doesn't hit this because V2 is flag-gated off. Force-canonicalize.
import sys as _sys
import importlib as _importlib
for _suffix in (
    "modules.printers.telemetry.events",
    "modules.printers.telemetry.bambu.adapter",
    "modules.printers.telemetry.bambu.raw",
    "modules.printers.telemetry.state",
    "modules.printers.telemetry.transition",
    "modules.printers.telemetry.observability",
):
    _mod = _importlib.import_module(_suffix)
    _sys.modules[f"backend.{_suffix}"] = _mod

# Mirror the backend's instrumentation hook so monitor-process callsites
# (the actual hot path) get measured too.
_stress_dir = os.environ.get("ODIN_STRESS_INSTRUMENTATION")
if _stress_dir:
    from tests.stress.mqtt.instrument import install as _install_stress
    _install_stress(_stress_dir)

from modules.printers.monitors.mqtt_monitor import main  # noqa: E402

if __name__ == "__main__":
    main()
