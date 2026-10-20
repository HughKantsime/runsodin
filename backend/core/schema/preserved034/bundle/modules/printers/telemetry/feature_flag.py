"""Feature flag for the V2 telemetry pipeline.

`ODIN_TELEMETRY_V2` env var controls which adapter path is live for
Bambu telemetry:

- `0` (default) — legacy `adapters/bambu.py` + `monitors/mqtt_printer.py`
  handle ingestion.
- `1` — the V2 pipeline (this package) handles Bambu ingestion.
  Moonraker/Prusa/Elegoo stay on legacy regardless.
- `shadow` — both pipelines run side by side, writing to different
  status fields. Diff metric exposed for comparison. Shadow-mode is
  wired in Phase 7 of the track, not here.

Call `is_v2_enabled()` at printer-adapter construction time; wire the
new adapter only if it returns True. Keep the gate this simple — no
per-printer override, no runtime flip. A restart flips the mode.
"""
from __future__ import annotations

import os
from typing import Literal

Mode = Literal["legacy", "v2", "shadow"]

_ENV_VAR = "ODIN_TELEMETRY_V2"
_VALID_VALUES = {"0", "1", "shadow"}


def mode() -> Mode:
    """Read the env var and return the normalized mode.

    Fail-loud on an invalid value: the operator typed something
    unexpected, don't silently default.
    """
    raw = os.environ.get(_ENV_VAR, "0").strip().lower()
    if raw not in _VALID_VALUES:
        raise ValueError(
            f"{_ENV_VAR}={raw!r} is not a valid value; "
            f"expected one of {sorted(_VALID_VALUES)}"
        )
    if raw == "1":
        return "v2"
    if raw == "shadow":
        return "shadow"
    return "legacy"


def is_v2_enabled() -> bool:
    """Convenience: True if the V2 adapter should be the primary ingestion path."""
    return mode() == "v2"


def is_shadow_enabled() -> bool:
    """Convenience: True if BOTH adapters should run side by side (Phase 7)."""
    return mode() == "shadow"
