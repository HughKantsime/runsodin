"""Window and valuation helpers shared by the analytics routes."""
from core.db_compat import as_utc_datetime
from modules.models_library.models import Model


def _ended_in_range(ended_at, range_start):
    from core.db_compat import as_utc_datetime
    try:
        ended = as_utc_datetime(ended_at)
    except (TypeError, ValueError, OverflowError):
        return False
    return ended is not None and ended.replace(tzinfo=None) >= range_start


def _span_hours(start, end):
    """Hours between two stored timestamps, or None when either is missing/unparseable/negative."""
    if start is None or end is None:
        return None
    try:
        span = (as_utc_datetime(end) - as_utc_datetime(start)).total_seconds() / 3600
    except (TypeError, ValueError, OverflowError):
        return None
    return span if span >= 0 else None


def _job_in_window(job, range_start):
    """A job belongs to the window when it ended inside it, or (no recorded end) was created in it."""
    stamp = job.actual_end if job.actual_end is not None else job.created_at
    if stamp is None:
        return False
    try:
        stamp = as_utc_datetime(stamp)
    except (TypeError, ValueError, OverflowError):
        return False
    return stamp is not None and stamp.replace(tzinfo=None) >= range_start


def _job_price(db, job):
    """Unit price of a job: stored price, else model catalog price, else None (unknown)."""
    if job.suggested_price is not None:
        return job.suggested_price
    if job.model_id:
        model = db.query(Model).filter(Model.id == job.model_id).first()
        if model and model.cost_per_item:
            return model.cost_per_item * (model.markup_percent if model.markup_percent is not None else 300) / 100
    return None


def _job_revenue(db, job):
    """Revenue of one job with a known positive quantity; None when the job has no known price."""
    price = _job_price(db, job)
    return None if price is None else price * job.quantity
