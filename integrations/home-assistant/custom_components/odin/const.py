"""Constants for the ODIN integration."""
from datetime import timedelta

DOMAIN = "odin"

CONF_CAMERA_TOKEN = "camera_token"
CONF_SCAN_INTERVAL = "scan_interval"

DEFAULT_SCAN_INTERVAL = 20
MIN_SCAN_INTERVAL = 10
MAX_SCAN_INTERVAL = 120

STATUS_PATH = "/api/v1/integrations/home-assistant/status"
SUPPORTED_SCHEMA_VERSION = 1

PRINTER_STATUSES = ["printing", "paused", "error", "offline", "idle", "unknown"]

REQUEST_TIMEOUT = timedelta(seconds=10)
