# Re-export stub — canonical location: modules/printers/monitors/mqtt_monitor.py
# This file is kept for backward compatibility. The daemon is now run as:
#   python -m modules.printers.monitors.mqtt_monitor
import runpy
import os
import sys

# Re-export public symbols so tests and other importers can access them directly.
# Uses a conditional import to avoid errors when run in __main__ mode before
# the full environment (DB_PATH, crypto, etc.) is available.
try:
    from modules.printers.monitors.mqtt_monitor import PrinterMonitor, DB_PATH  # noqa: F401
except Exception:
    pass

if __name__ == "__main__":
    sys.path.insert(0, os.environ.get('BACKEND_PATH', '/app/backend'))
    runpy.run_module("modules.printers.monitors.mqtt_monitor", run_name="__main__", alter_sys=True)
