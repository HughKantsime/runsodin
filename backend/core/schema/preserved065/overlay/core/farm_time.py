"""O.D.I.N. — The farm's time zone: where "today" starts.

One admin setting (Settings > General), ``farm_timezone`` in system_config, an IANA
name. Stored timestamps stay naive UTC; this module only says where the day
boundary is drawn. Resolution order, in ``farm_zone``:

1. the saved ``farm_timezone``;
2. else the zone saved with the scheduler's quiet hours, if one was saved;
3. else the server's ``TZ`` environment variable;
4. else UTC.

A name this server's zone database does not know is skipped with a warning and
the next rule applies, so a bad value never breaks a page.
"""

import logging
import os
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from core.models import SystemConfig
from core.scheduler_settings import check_timezone, get_blackout_timezone

log = logging.getLogger("odin.api")

FARM_TIMEZONE_KEY = "farm_timezone"


def get_farm_timezone(db: Session) -> str | None:
    """The saved setting as stored; None when no admin has set it."""
    row = db.query(SystemConfig).filter(SystemConfig.key == FARM_TIMEZONE_KEY).first()
    value = row.value if row else None
    return value if isinstance(value, str) and value else None


def set_farm_timezone(db: Session, name: str | None) -> str | None:
    """Save the farm time zone, or clear it with None / ""; the caller commits.
    ValueError for a name this server does not know."""
    row = db.query(SystemConfig).filter(SystemConfig.key == FARM_TIMEZONE_KEY).first()
    if not name:
        if row:
            db.delete(row)
        return None
    check_timezone(name)
    if row:
        row.value = name
    else:
        db.add(SystemConfig(key=FARM_TIMEZONE_KEY, value=name))
    return name


def farm_zone_source(db: Session) -> tuple[str, str]:
    """(IANA name, rule that gave it): "setting", "quiet_hours", "server" or "utc"."""
    for source, name in (("setting", get_farm_timezone(db)), ("quiet_hours", get_blackout_timezone(db)),
                         ("server", os.environ.get("TZ"))):
        if not name:
            continue
        try:
            return check_timezone(name), source
        except ValueError:
            log.warning("Farm time zone: unknown time zone %r (%s); ignoring it", name, source)
    return "UTC", "utc"


def alert_local_timezone(db, org_settings=None) -> str:
    """Use the organization's saved zone when present, otherwise the farm's effective zone."""
    name = (org_settings or {}).get("timezone")
    if name and (name != "UTC" or org_settings.get("quiet_hours_timezone_confirmed") is True):
        try:
            return check_timezone(name)
        except ValueError:
            pass
    if isinstance(db, Session):
        return farm_zone_source(db)[0]
    # Notification daemons already hold a Connection. Reuse it for the ORM reads.
    with Session(bind=db) as session:
        return farm_zone_source(session)[0]


def farm_zone(db: Session) -> ZoneInfo:
    """The zone the farm's day is counted in (see the module docstring for the order)."""
    return ZoneInfo(farm_zone_source(db)[0])


def _aware(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    return now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now


def farm_day_start(db: Session, now: datetime | None = None) -> datetime:
    """The farm's local midnight at or before ``now``, as an aware UTC datetime.
    A ``now`` with no zone is UTC, as stored timestamps are."""
    zone = farm_zone(db)
    local = _aware(now).astimezone(zone)
    # Built from the date, not by zeroing the clock: on a daylight-saving change day midnight
    # has a different UTC offset from ``now`` and zoneinfo picks the right one for it.
    return datetime(local.year, local.month, local.day, tzinfo=zone).astimezone(timezone.utc)


def farm_today(db: Session, now: datetime | None = None) -> date:
    """The farm's calendar date."""
    return _aware(now).astimezone(farm_zone(db)).date()
