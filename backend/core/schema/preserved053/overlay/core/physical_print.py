"""Conservative identity checks for an unreserved physical print."""
from datetime import datetime, timezone
import math
from core.db_compat import as_utc_datetime

CLOSED_UNKNOWN = 'closed: superseded by a new physical start; end time unknown'
# Some printers' timers stop during a pause while a row's age keeps growing. Only a first
# look can land on a long pause: a start the monitor watched happen is a new print.
PAUSE_ALLOWANCE_SECONDS = 24 * 3600


def continuing_print_limit(print_timer, *, first_look):
    """The age a running row may have and still be this print, given the printer's print timer."""
    timer = print_timer if type(print_timer) in (int, float) and math.isfinite(print_timer) and print_timer > 0 else 0
    return timer + PAUSE_ALLOWANCE_SECONDS if first_look else timer


def continues_print(row, filename, elapsed, job_id=None):
    """The printer's own job id decides when both sides have one; a name alone cannot be Unknown."""
    if job_id is not None:
        if row['job_id'] is not None:
            return row['job_id'] == job_id
        if filename == 'Unknown':
            return False
    return same_physical_print(row, filename, elapsed)


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
