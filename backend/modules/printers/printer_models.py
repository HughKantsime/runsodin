"""
printer_models.py — Centralized printer model code mappings.

Maps raw API/protocol codes to friendly display names for Bambu, PrusaLink,
and Elegoo protocols. Used by test-connection endpoints to auto-detect model.

Usage:
    from printer_models import normalize_model_name

    name = normalize_model_name("bambu", "BL-P001")  # -> "X1C"
    name = normalize_model_name("bambu", "Bambu Lab H2D")  # -> "H2D"
    name = normalize_model_name("bambu", "UNKNOWN")   # -> "UNKNOWN" (passthrough)
    name = normalize_model_name("bambu", "")          # -> None
"""

from typing import Optional

# ---------------------------------------------------------------------------
# Bambu Lab: the single source of truth for Bambu model identity.
#
# Keys are Bambu machine model ids: the `model_id` of Bambu Studio's machine
# profiles (resources/profiles/BBL/machine/*.json; every model_id there as of
# Bambu Studio 02.05.00.66), which is also what sliced
# 3MF files carry as `printer_model_id` and what MQTT `printer_type` reports
# when present. The captured A1 3MF fixture carries N2S. The captured A1/H2D
# MQTT sessions carry no `printer_type` at all; their identity is the ota
# module's `product_name` ("Bambu Lab A1", "Bambu Lab H2D"), handled by
# BAMBU_PRODUCT_NAMES below.
#
# Hardware evidence: N2S (A1) and the product names "Bambu Lab A1" and
# "Bambu Lab H2D" are captured from real printers. The remaining ids follow
# Bambu Studio's machine profiles and are not yet confirmed on hardware.
# Codes that previously appeared here without a source (BL-P003, BL-P00401,
# BL-P00501, BL-A001, BL-A002, BL-A003, BL-H001) were removed; an
# unrecognised code passes through unchanged.
# ---------------------------------------------------------------------------
BAMBU_MODEL_IDS = {
    "BL-P001": "X1C",
    "BL-P002": "X1",
    "C13": "X1E",
    "C11": "P1P",
    "C12": "P1S",
    "N1": "A1 Mini",
    "N2S": "A1",
    "N7": "P2S",
    "O1C2": "H2C",
    "O1D": "H2D",
    "O1E": "H2D Pro",
    "O1S": "H2S",
}

# Product names reported in info.module[name=ota].product_name, without the
# "Bambu Lab " prefix, upper-cased -> friendly name.
BAMBU_PRODUCT_NAMES = {
    "X1 CARBON": "X1C",
    "X1C": "X1C",
    "X1": "X1",
    "X1E": "X1E",
    "P1P": "P1P",
    "P1S": "P1S",
    "A1": "A1",
    "A1 MINI": "A1 Mini",
    "P2S": "P2S",
    "H2D": "H2D",
    "H2D PRO": "H2D Pro",
    "H2S": "H2S",
    "H2C": "H2C",
}

# Total XY build areas, Bambu specifications (sources in docs/dispatch-capability.md).
BAMBU_BEDS = {
    'X1C': (256, 256), 'X1': (256, 256), 'X1E': (256, 256),
    'P1P': (256, 256), 'P1S': (256, 256), 'P2S': (256, 256),
    'A1': (256, 256), 'A1 Mini': (180, 180), 'H2D': (350, 320),
    'H2D Pro': (350, 320), 'H2C': (330, 320), 'H2S': (340, 320),
}

_BAMBU_FRIENDLY_NAMES = {name.upper(): name for name in BAMBU_PRODUCT_NAMES.values()}

# Nozzle (extruder) count per model: the length of `nozzle_diameter` in the
# Bambu Studio 02.05.00.66 machine profiles. Every other listed model has one.
BAMBU_NOZZLE_COUNTS = {
    "H2D": 2,
    "H2D Pro": 2,
    "H2C": 2,
}


def bambu_nozzle_count(machine_type: Optional[str]) -> int:
    """Nozzle count for a stored machine_type / model code; 1 when unknown."""
    return BAMBU_NOZZLE_COUNTS.get(bambu_model_name(machine_type) or "", 1)


def bambu_model_name(raw_value: Optional[str]) -> Optional[str]:
    """Friendly Bambu model for a model id, product name or friendly name.

    Unrecognised values pass through stripped; empty values return None.
    """
    if not isinstance(raw_value, str) or not raw_value.strip():
        return None
    value = raw_value.strip()
    upper = value.upper()
    for code, name in BAMBU_MODEL_IDS.items():
        if code.upper() == upper:
            return name
    # Firmware spells the same product "X1 Carbon" or "X1-Carbon"; compare
    # with hyphens, underscores and repeated spaces folded to one space.
    folded = " ".join(upper.replace("-", " ").replace("_", " ").split())
    product = folded[len("BAMBU LAB "):].strip() if folded.startswith("BAMBU LAB ") else folded
    if product in BAMBU_PRODUCT_NAMES:
        return BAMBU_PRODUCT_NAMES[product]
    return _BAMBU_FRIENDLY_NAMES.get(upper, value)


# ---------------------------------------------------------------------------
# PrusaLink: known hardware model codes -> friendly names.
# Do not feed GET /api/version's `printer` field into this map: the official
# PrusaLink schema defines that field as a printer software-version string,
# not hardware identity. Callers must already have a documented model code.
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
            return bambu_model_name(raw_stripped)

        if api_type == "prusalink":
            return _PRUSALINK_MODELS.get(raw_stripped, raw_stripped)

        if api_type == "anycubic_lan":
            from modules.printers.parsing.anycubic_lan import MODELS
            return MODELS.get(raw_stripped, raw_stripped)

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
