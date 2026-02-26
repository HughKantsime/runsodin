"""print_file_meta.py — Standalone print file metadata extraction.

Parses uploaded print files to extract bed dimensions and compatible printer
API types. No DB imports — pure file parsing only.

Public API:
    extract_print_file_meta(file_path, extension) -> dict
        Returns {bed_x_mm, bed_y_mm, compatible_api_types}
        All values are nullable — extraction failures return None, not exceptions.
"""

import zipfile
import logging

log = logging.getLogger("odin.print_file_meta")

# Known printer bed sizes keyed on lowercase model name fragments.
# Order matters — more specific keys should come before shorter ones.
KNOWN_PRINTER_BEDS = {
    "x1 carbon": (256, 256),
    "x1c": (256, 256),
    "x1e": (256, 256),
    "x1": (256, 256),
    "p1s": (256, 256),
    "p1p": (256, 256),
    "a1 mini": (180, 180),
    "a1": (256, 256),
    "h2d": (320, 320),
    "mk4": (250, 210),
    "mk3": (250, 210),
    "mini": (180, 180),
    "ender 3": (220, 220),
    "ender-3": (220, 220),
    "voron": (300, 300),
}


def _extract_gcode_meta(file_path: str):
    """Scan first 100 lines of a gcode file for slicer bed size comments.

    Recognises:
    - PrusaSlicer:  ; bed_size_x = 256.00  / ; bed_size_y = 256.00
    - Cura:         ; machine_width = 220   / ; machine_depth = 220
    - Bambu gcode:  ; plate_x = ...         / ; print_size_x = ...

    Returns (x, y) tuple of floats or (None, None).
    """
    x = None
    y = None
    try:
        with open(file_path, "r", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i >= 100:
                    break
                stripped = line.strip()
                if not stripped.startswith(";"):
                    continue
                # Remove leading semicolon and whitespace
                content = stripped[1:].strip()

                # PrusaSlicer bed_size_x / bed_size_y
                if content.startswith("bed_size_x"):
                    try:
                        x = float(content.split("=", 1)[1].strip())
                    except (ValueError, IndexError):
                        pass
                elif content.startswith("bed_size_y"):
                    try:
                        y = float(content.split("=", 1)[1].strip())
                    except (ValueError, IndexError):
                        pass

                # Cura machine_width / machine_depth
                elif content.startswith("machine_width"):
                    try:
                        x = float(content.split("=", 1)[1].strip())
                    except (ValueError, IndexError):
                        pass
                elif content.startswith("machine_depth"):
                    try:
                        y = float(content.split("=", 1)[1].strip())
                    except (ValueError, IndexError):
                        pass

                # Bambu gcode — plate_x or print_size_x
                elif content.startswith("plate_x"):
                    try:
                        x = float(content.split("=", 1)[1].strip())
                    except (ValueError, IndexError):
                        pass
                elif content.startswith("print_size_x"):
                    try:
                        x = float(content.split("=", 1)[1].strip())
                    except (ValueError, IndexError):
                        pass

                if x is not None and y is not None:
                    break
    except Exception as e:
        log.debug(f"[print_file_meta] gcode parse error for {file_path}: {e}")

    return (x, y)


def _extract_3mf_meta(file_path: str):
    """Extract bed dimensions from a .3mf (zip) file.

    Tries:
    1. Metadata/slice_info.config — Bambu-specific: has machine_model string.
       Resolved via KNOWN_PRINTER_BEDS lookup.
    2. Metadata/model_settings.config — PrusaSlicer: has bed_shape entries.

    Returns (x, y) tuple of floats or (None, None).
    """
    x = None
    y = None
    try:
        with zipfile.ZipFile(file_path, "r") as zf:
            names_lower = {n.lower(): n for n in zf.namelist()}

            # --- Attempt 1: Bambu slice_info.config ---
            slice_key = names_lower.get("metadata/slice_info.config")
            if slice_key:
                try:
                    content = zf.read(slice_key).decode("utf-8", errors="replace")
                    # Look for machine_model or printer_model attribute
                    import re
                    match = re.search(
                        r'(?:machine_model|printer_model)\s*=\s*"?([^"\n]+)"?',
                        content,
                        re.IGNORECASE,
                    )
                    if match:
                        model_str = match.group(1).strip().lower()
                        result = _lookup_known_bed(model_str)
                        if result != (None, None):
                            return result
                except Exception as e:
                    log.debug(f"[print_file_meta] slice_info parse error: {e}")

            # --- Attempt 2: PrusaSlicer model_settings.config bed_shape ---
            settings_key = names_lower.get("metadata/model_settings.config")
            if settings_key:
                try:
                    content = zf.read(settings_key).decode("utf-8", errors="replace")
                    import re
                    # bed_shape = 0x0,220x0,220x220,0x220
                    match = re.search(r"bed_shape\s*=\s*([^\n]+)", content)
                    if match:
                        coords_str = match.group(1).strip()
                        coords = []
                        for pair in coords_str.split(","):
                            pair = pair.strip()
                            if "x" in pair:
                                parts = pair.split("x")
                                try:
                                    coords.append((float(parts[0]), float(parts[1])))
                                except ValueError:
                                    pass
                        if coords:
                            max_x = max(c[0] for c in coords)
                            max_y = max(c[1] for c in coords)
                            if max_x > 0 and max_y > 0:
                                x, y = max_x, max_y
                except Exception as e:
                    log.debug(f"[print_file_meta] model_settings parse error: {e}")

    except Exception as e:
        log.debug(f"[print_file_meta] 3mf open error for {file_path}: {e}")

    return (x, y)


def _lookup_known_bed(model_str: str):
    """Resolve a printer model string to (x, y) using KNOWN_PRINTER_BEDS.

    Iterates keys in definition order — more specific keys first.
    Returns (None, None) if no match found.
    """
    model_lower = model_str.lower()
    for key, dims in KNOWN_PRINTER_BEDS.items():
        if key in model_lower:
            return dims
    return (None, None)


def _resolve_api_types(extension: str) -> str:
    """Map file extension to compatible printer API types.

    .3mf  → "bambu"
    .gcode / .bgcode → "moonraker,prusalink,elegoo"
    """
    ext = extension.lower().lstrip(".")
    if ext == "3mf":
        return "bambu"
    if ext in ("gcode", "bgcode"):
        return "moonraker,prusalink,elegoo"
    return ""


def extract_print_file_meta(file_path: str, extension: str) -> dict:
    """Extract bed dimensions and compatible API types from a print file.

    Args:
        file_path: Absolute path to the file on disk.
        extension: File extension including leading dot (e.g. ".3mf", ".gcode").

    Returns:
        dict with keys:
            bed_x_mm (float | None): X bed dimension in mm.
            bed_y_mm (float | None): Y bed dimension in mm.
            compatible_api_types (str): Comma-separated printer API types, or "".

    Never raises — extraction failures return None values.
    """
    ext = extension.lower().lstrip(".")
    bed_x = None
    bed_y = None

    try:
        if ext == "3mf":
            bed_x, bed_y = _extract_3mf_meta(file_path)
        elif ext == "gcode":
            bed_x, bed_y = _extract_gcode_meta(file_path)
        elif ext == "bgcode":
            # bgcode is binary — skip metadata extraction
            pass
    except Exception as e:
        log.warning(f"[print_file_meta] Unexpected error extracting meta from {file_path}: {e}")

    compatible_api_types = _resolve_api_types(extension)

    return {
        "bed_x_mm": bed_x,
        "bed_y_mm": bed_y,
        "compatible_api_types": compatible_api_types,
    }
