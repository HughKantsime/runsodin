"""Base Pydantic model for telemetry ingestion.

Every vendor-specific raw model inherits from `_StrictBase`. The config
is deliberate:

- `extra="allow"` — do not reject payloads with new fields. Firmware updates
  add fields; a production pipeline cannot crash on a new key. Unknown
  fields are reported to `observability.UnmappedFieldObserver` exactly once
  per (vendor, field_path) pair so we see them without spamming logs.

- `extra="forbid"` is used only on **message envelopes** (the capture format
  `ts`/`iso`/`printer_id`/`direction`/`protocol`/...) — those are our
  format, we control them, unexpected keys in envelopes mean a bug in
  capture/replay.

- `strict=False` with explicit `BeforeValidator`-based coercion — Bambu
  firmware is polymorphic on several fields (e.g. `stg_cur` as int or
  str-of-int). We accept the observed forms, normalize deterministically,
  and raise on unparseable input. Strict mode would reject valid data;
  permissive mode without explicit coercion would silently accept garbage.

- `validate_assignment=True` — if code somewhere mutates a status after
  construction, the assignment is re-validated.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class _StrictBase(BaseModel):
    """Base for all telemetry raw models. See module docstring for rationale."""

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
        strict=False,
        validate_assignment=True,
    )


class _EnvelopeBase(BaseModel):
    """Base for message envelopes we control (capture JSONL lines, replay frames)."""

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        strict=True,
        validate_assignment=True,
    )
