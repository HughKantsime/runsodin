"""
printer_models.py — Centralized printer model code mappings.

Maps raw API/protocol codes to friendly display names for Bambu, PrusaLink,
and Elegoo protocols. Used by test-connection endpoints to auto-detect model.

Usage:
    from printer_models import normalize_model_name

    name = normalize_model_name("bambu", "BL-P001")  # -> "X1C"
    name = normalize_model_name("bambu", "UNKNOWN")   # -> "UNKNOWN" (passthrough)
    name = normalize_model_name("bambu", "")          # -> None
"""

from typing import Optional

# ---------------------------------------------------------------------------
# Bambu Lab: raw MQTT `printer_type` codes -> friendly names
# Source: community research, Bambu developer firmware strings
# ---------------------------------------------------------------------------
_BAMBU_MODELS = {
    "BL-P001": "X1C",
    "BL-P002": "X1E",
    "BL-P003": "X1",
    "C11": "P1S",
    "BL-P00501": "P1S",
    "C12": "P1P",
    "BL-P00401": "P1P",
    "N2S": "A1",
    "BL-A001": "A1",
    "N1": "A1 Mini",
    "BL-A002": "A1 Mini",
    "BL-H001": "H2D",
}

# ---------------------------------------------------------------------------
# PrusaLink: `printer` field from GET /api/version -> friendly names
# Source: PrusaLink OpenAPI spec (github.com/prusa3d/Prusa-Link-Web)
# The /api/version response includes a "printer" object with a "type" field,
# or sometimes a top-level "printer" string key depending on firmware version.
# ---------------------------------------------------------------------------
_PRUSALINK_MODELS = {
    "MK4S": "MK4S",
    "MK4": "MK4",
    "MK39": "MK3.9",
    "MK3.9": "MK3.9",
    "MK35": "MK3.5",
    "MK3.5": "MK3.5",
    "MINI": "MINI+",
    "MINI+": "MINI+",
    "XL": "XL",
    "CORE_ONE": "CORE One",
    "COREONE": "CORE One",
}

# ---------------------------------------------------------------------------
# Elegoo: `MachineName` from SDCP UDP discovery response -> friendly names
# These are generally already human-readable; the mapping normalizes casing
# and handles any variant spellings seen in firmware.
# ---------------------------------------------------------------------------
_ELEGOO_MODELS = {
    "Centauri Carbon": "Centauri Carbon",
    "centauri carbon": "Centauri Carbon",
    "Neptune 4 Pro": "Neptune 4 Pro",
    "Neptune 4 Plus": "Neptune 4 Plus",
    "Neptune 4 Max": "Neptune 4 Max",
    "Neptune 4": "Neptune 4",
    "Saturn 4 Ultra": "Saturn 4 Ultra",
    "Saturn 4": "Saturn 4",
}


def normalize_model_name(api_type: str, raw_value: Optional[str]) -> Optional[str]:
    """
    Map a raw protocol model code to a friendly display name.

    Args:
        api_type:  One of "bambu", "prusalink", "elegoo", "moonraker".
        raw_value: The raw model code/name from the printer's API.

    Returns:
        Friendly name string, or the raw_value as-is if unrecognized,
        or None if raw_value is empty/None.
        For "moonraker": always returns None (detection handled inline).
        Never raises an exception.
    """
    if not raw_value or not raw_value.strip():
        return None

    raw_stripped = raw_value.strip()
    api_type = (api_type or "").lower()

    try:
        if api_type == "bambu":
            return _BAMBU_MODELS.get(raw_stripped, raw_stripped)

        if api_type == "prusalink":
            return _PRUSALINK_MODELS.get(raw_stripped, raw_stripped)

        if api_type == "elegoo":
            # Try exact match first, then case-insensitive
            if raw_stripped in _ELEGOO_MODELS:
                return _ELEGOO_MODELS[raw_stripped]
            lower = raw_stripped.lower()
            for key, val in _ELEGOO_MODELS.items():
                if key.lower() == lower:
                    return val
            # Unknown Elegoo model — pass through as-is (already human-readable)
            return raw_stripped

        if api_type == "moonraker":
            # Moonraker detection is handled inline in the endpoint;
            # this function does not map Moonraker model codes.
            return None

    except Exception:
        # Safety net — detection must never raise
        pass

    # Unknown api_type — passthrough
    return raw_stripped
