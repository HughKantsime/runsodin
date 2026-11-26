"""The internal camera relay route and its allowlist exception."""
import re

RELAY_PATH = "/internal/camera-source/{printer_id}/{token}"
_RELAY_PATTERN = re.compile(RELAY_PATH.format(printer_id=r"[0-9]+", token=r"[^/]+"))


def is_relay_request(scope):
    # go2rtc sends a literal GET path. Encoded aliases do not need the exception.
    raw_path = scope.get("raw_path")
    return (scope["method"] == "GET" and _RELAY_PATTERN.fullmatch(scope["path"]) is not None
            and raw_path is not None and b"%" not in raw_path)
