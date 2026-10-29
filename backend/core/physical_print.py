"""Conservative identity checks for an unreserved physical print."""
from datetime import datetime, timezone
import math
from core.db_compat import as_utc_datetime

CLOSED_UNKNOWN = 'closed: superseded by a new physical start; end time unknown'


def same_physical_print(row, filename, elapsed, now=None):
    try:
        elapsed = float(elapsed)
        age = ((now or datetime.now(timezone.utc)) - as_utc_datetime(row['started_at'])).total_seconds()
        return (bool(filename) and filename in (row['filename'], row['job_name'])
                and math.isfinite(elapsed) and elapsed >= 0 and -120 <= age <= elapsed + 120)
    except (TypeError, ValueError, OverflowError):
        return False
