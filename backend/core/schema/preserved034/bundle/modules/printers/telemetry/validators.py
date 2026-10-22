"""Polymorphic coercion validators for telemetry fields.

Bambu firmware sends the same logical field with inconsistent types across
firmware versions and (sometimes) across messages within the same session.
Observed examples from `odin-e2e/captures/run-2026-04-16`:

- `print.stg_cur` — captured as both `int` and `str` of the same numeric
  value. Values seen: 1, 2, 3, 4, 13, 14, 29, 39, 255, -1 (and their
  string forms).
- `print.cooling_fan_speed` — always `str` in captures, but the field is
  numeric; legacy adapter silently coerced to int via `.get(k, default)`.

These validators make the coercion explicit and **fail loud** on
unparseable input, replacing the legacy silent-default behavior.
"""
from __future__ import annotations

from typing import Any


def coerce_int_or_raise(v: Any) -> int:
    """Accept `int` or `str`-of-int; raise `ValueError` on anything else.

    Never returns a default. The caller decides what to do with unparseable
    input — typically it propagates up as a Pydantic validation error, which
    the adapter catches and reports as a DEGRADED telemetry event (visible in
    the observability queue), not as a silent fallback.

    Empirical basis: `print.stg_cur` in bambu-p1s and bambu-h2d captures
    appears as both `2` and `"2"` within the same session.
    """
    if isinstance(v, bool):
        # bool is a subclass of int in Python; treat it as unparseable here
        # because no telemetry field in the captures is a real bool-as-int.
        raise ValueError(f"refusing to coerce bool to int: {v!r}")
    if isinstance(v, int):
        return v
    if isinstance(v, str):
        stripped = v.strip()
        if not stripped:
            raise ValueError("empty string cannot coerce to int")
        return int(stripped)  # raises ValueError on non-numeric
    raise ValueError(f"cannot coerce {type(v).__name__} to int: {v!r}")


def coerce_float_or_raise(v: Any) -> float:
    """Accept `int`, `float`, or `str`-of-number; raise on anything else.

    Empirical basis: Bambu's temperature fields (`bed_temper`, `nozzle_temper`)
    appear as both `float` and `int` in the same session depending on whether
    the firmware happens to send `35.0` or `35`.
    """
    if isinstance(v, bool):
        raise ValueError(f"refusing to coerce bool to float: {v!r}")
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        stripped = v.strip()
        if not stripped:
            raise ValueError("empty string cannot coerce to float")
        return float(stripped)
    raise ValueError(f"cannot coerce {type(v).__name__} to float: {v!r}")
