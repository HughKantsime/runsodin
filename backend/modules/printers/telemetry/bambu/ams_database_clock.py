"""Database-clock receipt anchoring; no evidence persistence or admission."""
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from sqlalchemy import text
from core.runtime_identity import revalidate_runtime_identity
from modules.printers.telemetry.bambu.ams_receipt_time import estimate_receipt_time


@dataclass(frozen=True)
class DatabaseClockAnchor:
    database_now: datetime
    query_started_monotonic: float
    query_finished_monotonic: float


def sample_database_clock(db):
    """Caller owns transaction. Never commit, retry or use local wall time.

    SQLite's millisecond formatting can round upward. Subtract a full
    millisecond so its coarse representation cannot refresh evidence.
    """
    try:
        dialect = db.get_bind().dialect.name
        started = time.monotonic()
        if dialect == 'postgresql':
            now = db.execute(text('SELECT clock_timestamp()')).scalar_one()
            if type(now) is not datetime or now.tzinfo is None or now.utcoffset() is None:
                raise ValueError()
            now = now.astimezone(timezone.utc)
        elif dialect == 'sqlite':
            value = db.execute(text("SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')")).scalar_one()
            if type(value) is not str or not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z', value):
                raise ValueError()
            now = datetime.strptime(value, '%Y-%m-%dT%H:%M:%S.%fZ').replace(tzinfo=timezone.utc) - timedelta(milliseconds=1)
        else:
            raise ValueError()
        finished = time.monotonic()
        return DatabaseClockAnchor(now, started, finished)
    except Exception:
        raise ValueError('AMS database clock unavailable') from None


def anchor_original_receipt(db, *, received_monotonic, receipt_clock_domain,
                            runtime, generation_started_at):
    """Freeze once before write retries; never clamp a receipt to generation start."""
    try:
        revalidate_runtime_identity(runtime)
        anchor = sample_database_clock(db)
        revalidate_runtime_identity(runtime)
        estimate = estimate_receipt_time(received_monotonic=received_monotonic,
            query_started_monotonic=anchor.query_started_monotonic,
            query_finished_monotonic=anchor.query_finished_monotonic,
            database_now=anchor.database_now, receipt_clock_domain=receipt_clock_domain,
            current_clock_domain=runtime.clock_domain)
        if (type(generation_started_at) is not datetime or generation_started_at.tzinfo is None
                or generation_started_at.utcoffset() is None
                or estimate.received_at < generation_started_at.astimezone(timezone.utc)):
            raise ValueError()
        return estimate
    except Exception:
        raise ValueError('AMS receipt chronology unavailable') from None
