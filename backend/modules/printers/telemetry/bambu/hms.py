"""Bambu HMS (Health Monitoring System) events.

Bambu printers emit a `print.hms[]` array in every MQTT status report
containing `(attr, code)` tuples that represent active health events —
filament issues, chamber warnings, AMS desiccant state, dust filter
reminders, etc. Captures (`odin-e2e/captures/run-2026-04-16`) show
H2D and X1C emitting 2-4 active HMS codes continuously for the whole
4-hour session — signals users today never see because the legacy
adapter drops the `hms` field on the floor.

This module models HMS events, loads the catalog (bootstrapped from
observed codes in T0.2), and provides lookup with **fail-loud**
semantics: codes not in the catalog are returned as `None` so the
caller decides to log at WARN and surface as "UNKNOWN HMS" — never
silently ignored.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import computed_field

from backend.modules.printers.telemetry.base import _StrictBase

Severity = Literal["info", "warning", "error", "unknown"]


class BambuHMSEvent(_StrictBase):
    """One entry from a Bambu `print.hms[]` array.

    The `(attr, code)` pair is Bambu's primary key; looking the combined
    key up in the catalog yields a `HMSEntry` with human-readable severity
    and message.
    """

    attr: int
    code: int

    @computed_field
    @property
    def key(self) -> str:
        """Canonical catalog key: `"<attr_hex>_<code_hex>"` (8 hex chars each)."""
        return f"{self.attr:08X}_{self.code:08X}"

    @computed_field
    @property
    def attr_hex(self) -> str:
        return f"0x{self.attr:08X}"

    @computed_field
    @property
    def code_hex(self) -> str:
        return f"0x{self.code:08X}"


@dataclass(frozen=True)
class HMSEntry:
    """Catalog row for a single HMS code."""

    key: str
    attr: int
    code: int
    severity: Severity
    message: str


class BambuHMSCatalog:
    """In-memory catalog of known HMS codes.

    Catalog data is loaded once from `hms_codes.json` sitting next to
    this module. Unknown codes encountered at runtime return `None`
    from `lookup()`; the caller is responsible for fail-loud handling
    (log at WARN, surface as `ActiveError(source="hms", code=key, message="unknown HMS")`
    in the PrinterStatus, observer pingback).
    """

    def __init__(self, entries: dict[str, HMSEntry]):
        self._entries = entries

    @classmethod
    def load(cls, path: Path | None = None) -> "BambuHMSCatalog":
        """Load the catalog from JSON on disk. Fails loud on missing/malformed file."""
        catalog_path = path or Path(__file__).parent / "hms_codes.json"
        if not catalog_path.exists():
            raise FileNotFoundError(
                f"Bambu HMS catalog not found at {catalog_path}. "
                "The V2 telemetry pipeline requires this file; check the "
                "backend/modules/printers/telemetry/bambu/ directory."
            )
        raw = json.loads(catalog_path.read_text())
        codes = raw.get("codes")
        if not isinstance(codes, dict):
            raise ValueError(
                f"Malformed HMS catalog at {catalog_path}: "
                "'codes' key missing or not a dict."
            )
        entries: dict[str, HMSEntry] = {}
        for key, data in codes.items():
            entries[key] = HMSEntry(
                key=key,
                attr=int(data["attr"]),
                code=int(data["code"]),
                severity=_normalize_severity(data.get("severity", "unknown")),
                message=str(data.get("message", "UNKNOWN — needs catalog entry")),
            )
        return cls(entries)

    def lookup(self, event: BambuHMSEvent) -> HMSEntry | None:
        """Return the catalog row for an event, or `None` if not known.

        Never returns a synthesized placeholder. The caller decides how
        to render unknowns so the UNKNOWN case is always visible (not
        silently mapped to a generic "error" tile).
        """
        return self._entries.get(event.key)

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, key: str) -> bool:
        return key in self._entries

    def keys(self):
        return self._entries.keys()


def _normalize_severity(raw: str) -> Severity:
    """Coerce catalog `severity` field to the Severity literal type; fail loud on garbage."""
    normalized = str(raw).lower().strip()
    if normalized in ("info", "warning", "error", "unknown"):
        return normalized  # type: ignore[return-value]
    raise ValueError(
        f"invalid HMS severity {raw!r}; must be info|warning|error|unknown"
    )


@lru_cache(maxsize=1)
def get_catalog() -> BambuHMSCatalog:
    """Process-wide singleton for the HMS catalog. Lazily loaded."""
    return BambuHMSCatalog.load()
