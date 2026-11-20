"""Read-only outcome definitions shared by print and job reporting.

SQL fragments accept a source-owned ``prefix`` (for example ``pj.``).
A missing print end means unknown; a missing cancelled job end means not started.
"""

PRINT_FAILED_SQL = "({prefix}status = 'failed' AND {prefix}ended_at IS NOT NULL)"
PRINT_STOPPED_SQL = "({prefix}status = 'cancelled' AND {prefix}ended_at IS NOT NULL)"
PRINT_DID_NOT_COMPLETE_SQL = "({prefix}status IN ('failed', 'cancelled') AND {prefix}ended_at IS NOT NULL)"
JOB_FAILED_SQL = "{prefix}status = 'failed'"
JOB_STOPPED_SQL = "({prefix}status = 'cancelled' AND {prefix}actual_end IS NOT NULL)"
JOB_DID_NOT_COMPLETE_SQL = "(" + JOB_FAILED_SQL + " OR " + JOB_STOPPED_SQL + ")"


def print_outcome(status, ended_at):
    """Return the failed/stopped part, or None for completed/running/unknown."""
    if ended_at is not None:
        return {'failed': 'failed', 'cancelled': 'stopped'}.get(status)
    return None


def job_outcome(status, actual_end):
    """Return the failed/stopped part, excluding cancellation before a start."""
    if status == 'failed':
        return 'failed'
    if status == 'cancelled' and actual_end is not None:
        return 'stopped'
    return None
