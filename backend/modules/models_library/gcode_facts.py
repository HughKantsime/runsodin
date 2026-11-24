"""Read slicer comments from the ends of a verified plain G-code snapshot."""
import math
import re
from pathlib import Path

from core.fact_text import fact_text
from .threemf_parser import MAX_GCODE_HEADER_BYTES, _FACT_COLOR, _SLICER_HEADER

_SUMMARY = re.compile(r"^;\s*(filament_type|filament_colou?r|filament used \[g\]|total filament weight \[g\])\s*[:=]\s*(.*)$", re.I)


def _grams(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and 0 <= number <= 1e7 else None


def read_facts(path):
    with Path(path).open("rb") as source:
        size = source.seek(0, 2)
        source.seek(0)
        head = source.read(MAX_GCODE_HEADER_BYTES)
        tail_start = max(len(head), size - MAX_GCODE_HEADER_BYTES)
        source.seek(tail_start)
        tail = source.read(MAX_GCODE_HEADER_BYTES)
    binary_header = head.startswith(b"GCDE") or b"\0" in head
    if binary_header:
        # Some exports prefix binary instructions with readable slicer comments.
        header = head.partition(b"\0")[0]
        if not header.lstrip().startswith(b";") or b"\n" not in header:
            return "binary_gcode", None
        sample = header.rpartition(b"\n")[0]
    elif tail_start == len(head):
        sample = head + tail
    else:
        sample = head.rpartition(b"\n")[0] + b"\n" + tail.partition(b"\n")[2]
    fields = {}
    slicer = None
    for line in sample.decode("utf-8", "replace").splitlines():
        line = line.strip()
        match = _SLICER_HEADER.match(line)
        if match and slicer is None:
            slicer = {"name": match[1], "version": match[2]}
        match = _SUMMARY.match(line)
        if match:
            fields[match[1].lower()] = [v.strip() for v in re.split("[;,]", match[2], maxsplit=64)][:64]
    materials = fields.get("filament_type", [])
    colors = fields.get("filament_colour", fields.get("filament_color", []))
    weights = [_grams(v) for v in fields.get("filament used [g]", [])]
    filaments = []
    for i in range(max(len(materials), len(colors), len(weights))):
        color = colors[i] if i < len(colors) else ""
        filaments.append({"slot": i + 1, "type": (fact_text(materials[i]) if i < len(materials) else None) or "Unknown",
                          "color": color if _FACT_COLOR.fullmatch(color) else None,
                          "grams": weights[i] if i < len(weights) else None})
    weight = _grams(fields.get("total filament weight [g]", [None])[0])
    if weight is None and weights and None not in weights:
        weight = _grams(sum(weights))
    if slicer is None and not filaments and weight is None:
        return "no_slicer_facts", None
    return "ok", {"slicer": slicer, "plates": [{"index": 1, "warnings": None, "thumbnail": None,
                  "layer_height": None, "print_time_seconds": None, "weight_grams": weight, "filaments": filaments}]}
