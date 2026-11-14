"""O.D.I.N. — Scheduler blackout hours (saved in Settings > General).

Stored in system_config so a save takes effect on the next scheduler run.
The BLACKOUT_START / BLACKOUT_END environment settings are the defaults when
nothing has been saved.
"""

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


def get_blackout(db: Session) -> tuple[str, str]:
    """Effective (start, end) blackout window as HH:MM strings (UTC)."""
    row = db.query(SystemConfig).filter(SystemConfig.key == BLACKOUT_KEY).first()
    value = row.value if row and isinstance(row.value, dict) else {}
    return (value.get("start") or settings.blackout_start, value.get("end") or settings.blackout_end)


def set_blackout(db: Session, start: str | None, end: str | None) -> None:
    """Save either end of the window; the caller commits."""
    current_start, current_end = get_blackout(db)
    value = {"start": start or current_start, "end": end or current_end}
    row = db.query(SystemConfig).filter(SystemConfig.key == BLACKOUT_KEY).first()
    if row:
        row.value = value
    else:
        db.add(SystemConfig(key=BLACKOUT_KEY, value=value))
