"""O.D.I.N. — Scheduler blackout hours (saved in Settings > General).

Stored in system_config so a save takes effect on the next scheduler run.
The BLACKOUT_START / BLACKOUT_END environment settings are the defaults when
nothing has been saved.

The saved value is {"start", "end", "timezone"}: the clock times are in that IANA
time zone. A value saved before the time zone existed, and the environment
defaults, have no time zone and mean UTC, as they always did.
"""

from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from core.config import settings
from core.models import SystemConfig

BLACKOUT_KEY = "scheduler_blackout"
# A window whose start equals its end blocks nothing: that is how "off" is stored.
BLACKOUT_OFF = ("00:00", "00:00")


def blackout_is_off(start: str, end: str) -> bool:
    return start == end


def disable_blackout(db: Session) -> None:
    """Save "no quiet hours"; the caller commits. New installs start this way (setup wizard);
    an install that never saved a window keeps the BLACKOUT_START / BLACKOUT_END defaults."""
    set_blackout(db, *BLACKOUT_OFF)


def check_timezone(name: str) -> str:
    """Return ``name`` when it is an IANA time zone this server knows; ValueError otherwise."""
    try:
        ZoneInfo(name)
    except Exception:  # unknown key, empty, a directory name, a path outside the zone database
        raise ValueError("Unknown time zone. Use an IANA name such as America/New_York.") from None
    return name


def get_blackout_timezone(db: Session) -> str | None:
    """IANA zone the saved window's clock times are in; None means UTC (never saved with a zone)."""
    row = db.query(SystemConfig).filter(SystemConfig.key == BLACKOUT_KEY).first()
    value = row.value if row and isinstance(row.value, dict) else {}
    return value.get("timezone") or None


def get_blackout(db: Session) -> tuple[str, str]:
    """Effective (start, end) blackout window as HH:MM strings, in get_blackout_timezone (UTC when None)."""
    row = db.query(SystemConfig).filter(SystemConfig.key == BLACKOUT_KEY).first()
    value = row.value if row and isinstance(row.value, dict) else {}
    return (value.get("start") or settings.blackout_start, value.get("end") or settings.blackout_end)


def set_blackout(db: Session, start: str | None, end: str | None, timezone: str | None = None) -> None:
    """Save either end of the window and, when given, its time zone; the caller commits.
    Without a time zone the saved one is kept: a window with none stays UTC."""
    current_start, current_end = get_blackout(db)
    value = {"start": start or current_start, "end": end or current_end}
    zone = check_timezone(timezone) if timezone else get_blackout_timezone(db)
    if zone:
        value["timezone"] = zone
    row = db.query(SystemConfig).filter(SystemConfig.key == BLACKOUT_KEY).first()
    if row:
        row.value = value
    else:
        db.add(SystemConfig(key=BLACKOUT_KEY, value=value))
