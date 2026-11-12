"""Constants for the ODIN integration."""
from datetime import timedelta

DOMAIN = "odin"

CONF_CAMERA_TOKEN = "camera_token"
CONF_SCAN_INTERVAL = "scan_interval"

DEFAULT_SCAN_INTERVAL = 20
MIN_SCAN_INTERVAL = 10
MAX_SCAN_INTERVAL = 120

STATUS_PATH = "/api/v1/integrations/home-assistant/status"
# Schema 2 adds the print-floor fields (next job, bench state, day totals, queue hours);
# schema 1 (older ODIN) lacks them and its entities report unknown.
SUPPORTED_SCHEMA_VERSIONS = (1, 2)

PRINTER_STATUSES = ["printing", "paused", "error", "offline", "idle", "unknown"]
# What the next job is waiting on; "nothing" stands for a null bench state.
BENCH_STATES = ["nothing", "paused", "out_of_service", "clear_plate", "blocked", "held"]

REQUEST_TIMEOUT = timedelta(seconds=10)
