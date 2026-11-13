"""One format for job datetimes: what Moonraker stores and what the apps are sent.

Storage: naive UTC "YYYY-MM-DD HH:MM:SS[.ffffff]" (no zone), the form the ORM writes.
Wire:    naive UTC "YYYY-MM-DDTHH:MM:SS" (the iOS/iPad decoders accept only this).

Stored rows are never rewritten; to_wire() normalizes whatever a row holds at read time.
Standalone on purpose: no database or app imports, so writers and serializers can share it.
"""
import logging
from datetime import datetime, timezone

log = logging.getLogger("odin.datetime_wire")

# Unparseable values are logged once each, and never more than this many distinct ones.
_MAX_REPORTED = 256
_reported: set[str] = set()


def _parse(value) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        text = value.strip()
        if text[-1:] in ("Z", "z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text)
    raise TypeError(f"not a datetime or ISO string: {type(value).__name__}")


def _utc_naive(parsed: datetime) -> datetime:
    """Aware values convert to UTC; naive values are already UTC by convention."""
    if parsed.utcoffset() is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _report_once(value) -> None:
    key = repr(value)[:80]
    if key in _reported or len(_reported) >= _MAX_REPORTED:
        return
    _reported.add(key)
    log.warning("Unparseable stored datetime %s; sending null", key)


def to_wire(value):
    """Any stored job datetime -> "YYYY-MM-DDTHH:MM:SS" (UTC), or None.

    None and "" give None. A value that cannot be parsed also gives None and is logged
    once: any other string would fail the app's whole list decode, while null only
    blanks that one field.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        return _utc_naive(_parse(value)).replace(microsecond=0).isoformat(timespec="seconds")
    except (TypeError, ValueError, OverflowError):
        _report_once(value)
        return None


def to_storage_utc(dt):
    """A datetime (or ISO string) -> naive UTC "YYYY-MM-DD HH:MM:SS[.ffffff]", for writers.

    The zone goes (the apps' decoders reject it); sub-second precision stays, because
    Moonraker history matching compares exact start and end times. None gives None.
    Unlike to_wire(), garbage raises ValueError: a writer must not persist it.
    """
    if dt is None:
        return None
    try:
        return _utc_naive(_parse(dt)).isoformat(sep=" ")
    except (TypeError, OverflowError) as error:
        raise ValueError(f"cannot store {dt!r} as a UTC timestamp") from error
