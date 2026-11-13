"""Scheduler quiet hours are clock times in the farm's saved IANA time zone (daylight saving
followed); a window saved before the zone existed, and the environment defaults, stay UTC."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from core.models import SystemConfig
from core.scheduler_settings import BLACKOUT_KEY, BLACKOUT_OFF, disable_blackout, get_blackout, get_blackout_timezone, set_blackout
from modules.jobs import scheduler as scheduler_module
from modules.jobs.scheduler import Scheduler, SchedulerConfig
from modules.system.schemas import ConfigUpdate
from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401

NY = ZoneInfo("America/New_York")


def _scheduler(start="22:30", end="05:30", zone=None):
    scheduler = Scheduler.__new__(Scheduler)
    scheduler.config = SchedulerConfig.from_time_strings(start, end, blackout_timezone=zone)
    return scheduler


def _blocked(scheduler, local: datetime) -> bool:
    """The scheduler is asked in UTC, as it plans; ``local`` is an aware wall-clock time."""
    return scheduler._is_blackout_time(local.astimezone(timezone.utc))


def _quarter_hours(first_utc: datetime, hours: int):
    return [first_utc + timedelta(minutes=15 * n) for n in range(hours * 4)]


@pytest.mark.parametrize("month,utc_offset", [(1, 5), (7, 4)])
def test_new_york_window_blocks_2230_to_0530_local_in_january_and_july(month, utc_offset):
    scheduler = _scheduler(zone="America/New_York")
    for instant in _quarter_hours(datetime(2026, month, 14, 0, 0, tzinfo=timezone.utc), 48):
        local = instant.astimezone(NY)
        minutes = local.hour * 60 + local.minute
        assert scheduler._is_blackout_time(instant) == (minutes >= 22 * 60 + 30 or minutes < 5 * 60 + 30), local
    # the same wall clock is a different UTC instant in winter and summer
    assert _blocked(scheduler, datetime(2026, month, 15, 22, 30, tzinfo=NY))
    assert not _blocked(scheduler, datetime(2026, month, 15, 22, 15, tzinfo=NY))
    assert _blocked(scheduler, datetime(2026, month, 15, 5, 15, tzinfo=NY))
    assert not _blocked(scheduler, datetime(2026, month, 15, 5, 30, tzinfo=NY))
    assert scheduler._is_blackout_time(datetime(2026, month, 16, 22 + utc_offset - 24, 30, tzinfo=timezone.utc))
    assert not scheduler._is_blackout_time(datetime(2026, month, 15, 5 + utc_offset, 30, tzinfo=timezone.utc))
    # a time with no zone is UTC, as everywhere else in the scheduler
    assert scheduler._is_blackout_time(datetime(2026, month, 15, 5 + utc_offset, 15))


def test_spring_forward_night_ends_at_0530_daylight_time():
    # 2026-03-08: 02:00 EST becomes 03:00 EDT, so the quiet night is one hour shorter.
    scheduler = _scheduler(zone="America/New_York")
    blocked = [t for t in _quarter_hours(datetime(2026, 3, 8, 0, 0, tzinfo=timezone.utc), 24) if scheduler._is_blackout_time(t)]
    night = [t for t in blocked if t < datetime(2026, 3, 8, 12, 0, tzinfo=timezone.utc)]
    assert night[0] == datetime(2026, 3, 8, 3, 30, tzinfo=timezone.utc)  # 22:30 EST, March 7
    assert night[-1] == datetime(2026, 3, 8, 9, 15, tzinfo=timezone.utc)  # 05:15 EDT
    assert not scheduler._is_blackout_time(datetime(2026, 3, 8, 9, 30, tzinfo=timezone.utc))  # 05:30 EDT
    assert len(night) == 24  # six hours, not seven, and no gap inside
    # this evening starts on daylight time
    assert not scheduler._is_blackout_time(datetime(2026, 3, 9, 2, 15, tzinfo=timezone.utc))  # 22:15 EDT
    assert scheduler._is_blackout_time(datetime(2026, 3, 9, 2, 30, tzinfo=timezone.utc))  # 22:30 EDT


def test_fall_back_night_ends_at_0530_standard_time():
    # 2026-11-01: 02:00 EDT becomes 01:00 EST, so the quiet night is one hour longer.
    scheduler = _scheduler(zone="America/New_York")
    assert not scheduler._is_blackout_time(datetime(2026, 11, 1, 2, 15, tzinfo=timezone.utc))  # 22:15 EDT, Oct 31
    night = _quarter_hours(datetime(2026, 11, 1, 2, 30, tzinfo=timezone.utc), 8)  # 22:30 EDT to 05:15 EST
    assert len(night) == 32 and all(scheduler._is_blackout_time(t) for t in night)
    assert night[-1].astimezone(NY).strftime("%H:%M %Z") == "05:15 EST"
    assert not scheduler._is_blackout_time(datetime(2026, 11, 1, 10, 30, tzinfo=timezone.utc))  # 05:30 EST
    # both passes through 01:30 are quiet
    assert scheduler._is_blackout_time(datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc))
    assert scheduler._is_blackout_time(datetime(2026, 11, 1, 6, 30, tzinfo=timezone.utc))


def test_a_window_with_no_zone_is_utc_exactly_as_before():
    legacy, explicit_utc = _scheduler(), _scheduler(zone="UTC")
    for instant in _quarter_hours(datetime(2026, 7, 14, 0, 0, tzinfo=timezone.utc), 48):
        minutes = instant.hour * 60 + instant.minute
        expected = minutes >= 22 * 60 + 30 or minutes < 5 * 60 + 30
        assert legacy._is_blackout_time(instant) == expected
        assert legacy._is_blackout_time(instant.replace(tzinfo=None)) == expected
        assert explicit_utc._is_blackout_time(instant) == expected
    assert SchedulerConfig().blackout_timezone is None


def test_off_stays_off_in_any_zone():
    for zone in (None, "America/New_York", "Pacific/Auckland"):
        scheduler = _scheduler(*BLACKOUT_OFF, zone=zone)
        assert not any(scheduler._is_blackout_time(t) for t in _quarter_hours(datetime(2026, 3, 7, 0, 0, tzinfo=timezone.utc), 72))


def test_saved_legacy_value_and_env_defaults_have_no_zone(review_db):  # noqa: F811
    db = review_db
    db.query(SystemConfig).filter(SystemConfig.key == BLACKOUT_KEY).delete(); db.commit()
    assert get_blackout(db) == ("22:30", "05:30") and get_blackout_timezone(db) is None  # environment defaults
    db.add(SystemConfig(key=BLACKOUT_KEY, value={"start": "21:00", "end": "06:00"})); db.commit()  # saved by an older version
    assert get_blackout(db) == ("21:00", "06:00") and get_blackout_timezone(db) is None
    # changing the times without naming a zone never adds one
    set_blackout(db, "20:00", None); db.commit()
    assert get_blackout(db) == ("20:00", "06:00") and get_blackout_timezone(db) is None
    assert db.query(SystemConfig).filter(SystemConfig.key == BLACKOUT_KEY).one().value == {"start": "20:00", "end": "06:00"}


def test_zone_is_saved_with_the_window_kept_by_later_saves_and_used_by_the_scheduler(review_db, monkeypatch):  # noqa: F811
    db = review_db
    set_blackout(db, "22:30", "05:30", "America/New_York"); db.commit()
    assert db.query(SystemConfig).filter(SystemConfig.key == BLACKOUT_KEY).one().value == {"start": "22:30", "end": "05:30", "timezone": "America/New_York"}
    set_blackout(db, "23:00", None); db.commit()  # a client that only sends times
    assert get_blackout(db) == ("23:00", "05:30") and get_blackout_timezone(db) == "America/New_York"
    set_blackout(db, None, None, "Europe/Berlin"); db.commit()  # "use my time zone": same clock hours
    assert get_blackout(db) == ("23:00", "05:30") and get_blackout_timezone(db) == "Europe/Berlin"

    seen = {}
    monkeypatch.setattr(scheduler_module.Scheduler, "run", lambda self, db: seen.setdefault("config", self.config))
    scheduler_module.run_scheduler(db)
    assert (seen["config"].blackout_start_hour, seen["config"].blackout_end_minute, seen["config"].blackout_timezone) == (23, 30, "Europe/Berlin")

    disable_blackout(db); db.commit()  # off keeps the zone and blocks nothing
    assert get_blackout(db) == BLACKOUT_OFF and get_blackout_timezone(db) == "Europe/Berlin"


def test_config_api_reports_and_saves_the_zone(review_db, monkeypatch):  # noqa: F811
    from modules.system import routes_config
    db = review_db
    monkeypatch.setattr(routes_config, "log_audit", lambda *a, **k: None)
    db.query(SystemConfig).filter(SystemConfig.key == BLACKOUT_KEY).delete(); db.commit()
    assert routes_config.get_config(current_user={}, db=db)["blackout_timezone"] is None
    routes_config.update_config(ConfigUpdate(blackout_timezone="America/New_York"), current_user={}, db=db)
    shown = routes_config.get_config(current_user={}, db=db)
    assert (shown["blackout_start"], shown["blackout_end"], shown["blackout_timezone"]) == ("22:30", "05:30", "America/New_York")


@pytest.mark.parametrize("zone", ["Mars/Olympus_Mons", "", "EST5", "America", "../../etc/passwd", "America/New_York "])
def test_unknown_zone_is_refused(zone, review_db):  # noqa: F811
    with pytest.raises(ValidationError):  # FastAPI answers a body that fails validation with 422
        ConfigUpdate(blackout_timezone=zone)
    with pytest.raises(ValueError):
        set_blackout(review_db, "22:30", "05:30", zone or "Nowhere/None")
    assert ConfigUpdate(blackout_timezone="America/New_York").blackout_timezone == "America/New_York"
