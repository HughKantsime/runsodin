# Re-export stub — canonical location: modules/archives/timelapse_capture.py
# This file is kept for backward compatibility. The daemon is now run as:
#   python -m modules.archives.timelapse_capture
import runpy
import os
import sys

if __name__ == "__main__":
    sys.path.insert(0, os.environ.get('BACKEND_PATH', '/app/backend'))
    runpy.run_module("modules.archives.timelapse_capture", run_name="__main__", alter_sys=True)
