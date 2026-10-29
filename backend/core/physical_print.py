"""Conservative identity checks for an unreserved physical print."""
from datetime import datetime, timezone
import math
from core.db_compat import as_utc_datetime

CLOSED_UNKNOWN = 'closed: superseded by a new physical start; end time unknown'


def same_physical_print(row, filename, elapsed, now=None):
    """Same file, and the row is no older than the printer's wall-clock time on this print.

    ``elapsed`` is Moonraker's print_stats.total_duration (pauses and heating included); when the
    printer does not report it, the filename alone decides.
    """
    if not filename or filename not in (row['filename'], row['job_name']):
        return False
    if elapsed is None:
        return True
    try:
        elapsed = float(elapsed)
        age = ((now or datetime.now(timezone.utc)) - as_utc_datetime(row['started_at'])).total_seconds()
        return math.isfinite(elapsed) and elapsed >= 0 and -120 <= age <= elapsed + 120
    except (TypeError, ValueError, OverflowError):
        return False
