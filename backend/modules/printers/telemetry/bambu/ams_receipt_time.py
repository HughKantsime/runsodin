"""Conservative database-clock receipt estimates, never admission authority.

Capture database_now with PostgreSQL clock_timestamp() (not transaction time)
or the SQLite database clock. Sample monotonic immediately before and after
that query. Freeze the returned estimate before a persistence retry.
"""
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from fractions import Fraction


@dataclass(frozen=True)
class ReceiptEstimate:
    received_at: datetime
    clock_domain: str
    provenance_version: str = 'db-clock-estimate-v1'


def estimate_receipt_time(*, received_monotonic, query_started_monotonic,
                          query_finished_monotonic, database_now,
                          receipt_clock_domain, current_clock_domain):
    """Subtract age at query return, conservatively including query latency.

    Domains must come from trusted runtime ownership; this helper cannot
    establish that authority from caller-supplied labels. Age at exactly the
    120-second boundary refuses. No local wall-clock fallback is permitted.
    """
    samples = (received_monotonic, query_started_monotonic,
               query_finished_monotonic)
    try:
        valid_samples = all(type(value) in (int, float) and math.isfinite(value)
                            and value >= 0 for value in samples)
    except OverflowError:
        valid_samples = False
    if not valid_samples:
        raise ValueError('AMS receipt clock unavailable')
    if (type(receipt_clock_domain) is not str or not receipt_clock_domain
            or len(receipt_clock_domain) > 128
            or receipt_clock_domain != current_clock_domain
            or type(current_clock_domain) is not str):
        raise ValueError('AMS receipt clock domain unavailable')
    # Use the exact representations of supplied floats for arithmetic, so
    # subtraction and conversion to microseconds cannot round age downward.
    received, started, finished = map(Fraction, samples)
    elapsed = finished - started
    age = finished - received
    if (elapsed < 0 or elapsed > 1 or age < 0 or age >= 120
            or received > started):
        raise ValueError('AMS receipt clock anchor unavailable')
    if (type(database_now) is not datetime or database_now.tzinfo is None
            or database_now.utcoffset() is None):
        raise ValueError('AMS database clock unavailable')
    try:
        # datetime rounds to microseconds. Round the subtraction upward to
        # avoid making the estimate younger through precision conversion.
        microseconds = math.ceil(age * 1_000_000)
        timestamp = database_now.astimezone(timezone.utc) - timedelta(
            microseconds=microseconds)
    except (OverflowError, ValueError):
        raise ValueError('AMS database clock unavailable') from None
    return ReceiptEstimate(timestamp, receipt_clock_domain)
