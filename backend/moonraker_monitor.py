# Re-export stub — canonical location: modules/printers/monitors/moonraker_monitor.py
import runpy
import os
import sys

if __name__ == "__main__":
    sys.path.insert(0, os.environ.get('BACKEND_PATH', '/app/backend'))
    runpy.run_module("modules.printers.monitors.moonraker_monitor", run_name="__main__", alter_sys=True)
