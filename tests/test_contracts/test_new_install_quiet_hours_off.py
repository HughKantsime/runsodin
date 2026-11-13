"""A new install starts with scheduler quiet hours off; an existing install keeps what it had."""
from datetime import datetime, timezone

from core.models import SystemConfig
from core.scheduler_settings import BLACKOUT_KEY, BLACKOUT_OFF, blackout_is_off, disable_blackout, get_blackout, set_blackout
from modules.jobs.scheduler import Scheduler, SchedulerConfig
from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401


def test_off_is_a_window_that_blocks_no_time():
    scheduler = Scheduler.__new__(Scheduler)
    scheduler.config = SchedulerConfig.from_time_strings(*BLACKOUT_OFF)
    assert not any(scheduler._is_blackout_time(datetime(2026, 11, 13, hour, minute, tzinfo=timezone.utc))
                   for hour in range(24) for minute in (0, 15, 30, 45))
    default = Scheduler.__new__(Scheduler)
    default.config = SchedulerConfig()
    assert default._is_blackout_time(datetime(2026, 11, 13, 23, 0, tzinfo=timezone.utc))


def test_an_install_that_never_saved_keeps_the_default_window(review_db):  # noqa: F811
    db = review_db
    db.query(SystemConfig).filter(SystemConfig.key == BLACKOUT_KEY).delete(); db.commit()
    assert get_blackout(db) == ("22:30", "05:30")


def test_disable_saves_off_and_a_later_save_turns_it_back_on(review_db):  # noqa: F811
    db = review_db
    disable_blackout(db); db.commit()
    assert get_blackout(db) == BLACKOUT_OFF and blackout_is_off(*get_blackout(db))
    set_blackout(db, "21:00", "06:00"); db.commit()
    assert get_blackout(db) == ("21:00", "06:00") and not blackout_is_off(*get_blackout(db))


def test_the_setup_wizard_account_step_turns_quiet_hours_off():
    import inspect
    from modules.system import routes_setup
    source = inspect.getsource(routes_setup.setup_create_admin)
    assert source.index("create_user_record(") < source.index("disable_blackout(db)") < source.index("stage_session(")


def test_setup_refuses_an_admin_email_that_is_not_an_address():
    import pytest
    from pydantic import ValidationError
    from modules.system.routes_setup import SetupAdminRequest
    with pytest.raises(ValidationError):
        SetupAdminRequest(username="owner", email="not-an-email", password="Synthetic123")
    assert SetupAdminRequest(username="owner", email=" owner@example.test ", password="Synthetic123").email == "owner@example.test"
    assert SetupAdminRequest(username="owner", email="", password="Synthetic123").email == ""
