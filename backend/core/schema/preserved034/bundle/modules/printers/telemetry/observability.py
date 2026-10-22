"""Telemetry observability — track fields we receive but do not model.

The V2 telemetry pipeline uses `extra="allow"` on every raw model so
future firmware can add fields without crashing the pipeline. But
allowing them silently would mean drift we never notice. This module
is the record of "what we're not modeling yet" — a structured,
bounded, rate-limited log that lets us see new fields without spamming
logs or leaking memory.

Contract:
- `UnmappedFieldObserver.observe(vendor, extras)` walks the leaf paths
  of an `extras` dict (what Pydantic gives us via `model_extra`) and
  records each `(vendor, field_path)` pair.
- Each pair is logged at WARN *exactly once* per process lifetime.
  Subsequent sightings increment a counter silently.
- `snapshot()` returns the current state as a list of reports suitable
  for serialization to `GET /api/telemetry/unmapped-fields`.
- `MAX_UNIQUE_FIELDS` (default 1000) caps memory. If exceeded, further
  observations are dropped with a single ERROR log — this is a
  breakage signal ("a rogue adapter is reporting garbage").

This module is self-contained. Prometheus export + API route live in
follow-up tasks (T4.x / route module), so this file has zero
dependencies on FastAPI or prometheus_client.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class UnmappedFieldReport:
    """One row from the observer's snapshot."""

    vendor: str
    field_path: str
    sample_type: str
    sample_value: str          # truncated to 200 chars
    first_seen_iso: str
    last_seen_iso: str
    count: int


@dataclass
class _Entry:
    """Mutable internal row. Kept separate from the frozen public report."""

    vendor: str
    field_path: str
    sample_type: str
    sample_value: str
    first_seen_iso: str
    last_seen_iso: str
    count: int = 0


def _walk_leaves(obj: Any, prefix: str = "") -> Iterator[tuple[str, Any]]:
    """Yield (dotted_path, leaf_value) for every scalar leaf in a nested dict/list."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            path = f"{prefix}.{k}" if prefix else k
            if isinstance(v, (dict, list)):
                yield from _walk_leaves(v, path)
            else:
                yield path, v
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, (dict, list)):
                yield from _walk_leaves(item, prefix + "[]")


class UnmappedFieldObserver:
    """Bounded, rate-limited tracker of telemetry fields we don't model.

    Thread-safe (the adapter may call observe from a background task).
    """

    MAX_UNIQUE_FIELDS = 1000
    VALUE_TRUNCATE = 200

    def __init__(self):
        self._entries: dict[tuple[str, str], _Entry] = {}
        self._overflow_logged = False
        self._lock = threading.Lock()

    def observe(self, vendor: str, extras: dict[str, Any] | None) -> None:
        """Record every leaf field in `extras` against `vendor`.

        `extras` is whatever Pydantic's `model_extra` returns — may be
        None if no unknown fields were present (the common case once
        the catalog stabilizes).
        """
        if not extras:
            return

        now = _now_iso()
        with self._lock:
            for path, value in _walk_leaves(extras):
                key = (vendor, path)
                existing = self._entries.get(key)
                if existing is not None:
                    existing.count += 1
                    existing.last_seen_iso = now
                    continue

                # new field
                if len(self._entries) >= self.MAX_UNIQUE_FIELDS:
                    if not self._overflow_logged:
                        logger.error(
                            "UnmappedFieldObserver capacity exceeded (%d fields); "
                            "further unmapped fields will be dropped until restart",
                            self.MAX_UNIQUE_FIELDS,
                        )
                        self._overflow_logged = True
                    continue

                sample_type = type(value).__name__
                sample_value = str(value)[: self.VALUE_TRUNCATE]
                entry = _Entry(
                    vendor=vendor,
                    field_path=path,
                    sample_type=sample_type,
                    sample_value=sample_value,
                    first_seen_iso=now,
                    last_seen_iso=now,
                    count=1,
                )
                self._entries[key] = entry
                # Log ONCE per (vendor, field_path). Subsequent occurrences
                # are silent — only the count grows.
                logger.warning(
                    "unmapped telemetry field: vendor=%s path=%s type=%s sample=%r",
                    vendor, path, sample_type, sample_value,
                )

    def snapshot(self) -> list[UnmappedFieldReport]:
        """Return an immutable, sorted snapshot of the observer state."""
        with self._lock:
            reports = [
                UnmappedFieldReport(
                    vendor=e.vendor,
                    field_path=e.field_path,
                    sample_type=e.sample_type,
                    sample_value=e.sample_value,
                    first_seen_iso=e.first_seen_iso,
                    last_seen_iso=e.last_seen_iso,
                    count=e.count,
                )
                for e in self._entries.values()
            ]
        reports.sort(key=lambda r: (r.vendor, r.field_path))
        return reports

    def reset(self) -> None:
        """Clear all state. Test-only."""
        with self._lock:
            self._entries.clear()
            self._overflow_logged = False

    def __len__(self) -> int:
        return len(self._entries)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# Process-wide singleton. Import and use `observer` directly.
observer = UnmappedFieldObserver()
