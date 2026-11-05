"""Damage and backup alerts reach the phone whatever the quiet hours are, for as long as the problem lasts.

The daily limit counts the inbox row, so an alert suppressed once is due again at the same hour the
next day, and is suppressed again. These run the real functions over many days; only the clock and
the outermost sender are stubbed.
"""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from core.base import AlertSeverity, AlertType
from core.db_integrity import DAMAGED, IntegrityReport
from modules.notifications import alert_dispatcher, quiet_hours
from modules.push import fanout
from modules.system import backup_failure_alerts
from modules.system.backup_service import BackupMissedError, BackupValidationError
from modules.system.db_guardian import record_integrity
from modules.system.db_guardian_status import publish_integrity_alert

# The guardian's real pace: 17,280 attempts in the 60 days.
STEP = timedelta(minutes=5)
DAYS = 60
NIGHT = datetime(2026, 11, 2, 3, 0, tzinfo=timezone.utc)
NOON = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
REPORT = IntegrityReport(DAMAGED, 0.1, 1, 0, ("Tree 3 page 3 cell 166: 2nd reference to page 155",))


class Clock(datetime):
    current = NIGHT

    @classmethod
    def now(cls, tz=None):
        return cls.current.astimezone(tz) if tz else cls.current.replace(tzinfo=None)


@pytest.fixture
def phones(tmp_path, monkeypatch):
    """Two admins, system quiet hours 22:00 to 07:00, and an admin's phone with the same hours of its own."""
    from core.schema import bootstrap_database
    engine = create_engine(f"sqlite:///{tmp_path / 'alerts.db'}")
    bootstrap_database(engine)
    device_prefs = json.dumps({"quiet_hours_enabled": True, "quiet_hours_start": "22:00", "quiet_hours_end": "07:00"})
    with engine.begin() as connection:
        # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- constant fixture SQL
        connection.execute(text("INSERT INTO users(id,username,password_hash,role,is_active,group_id) VALUES"
                                "(1,'admin','x','admin',1,NULL),(2,'second','x','admin',1,NULL),(3,'viewer','x','viewer',1,NULL)"))
        for key, value in (("quiet_hours_enabled", "true"), ("quiet_hours_start", "22:00"), ("quiet_hours_end", "07:00")):
            # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- constant fixture SQL
            connection.execute(text("INSERT INTO system_config(key,value) VALUES(:key,:value)"), {"key": key, "value": json.dumps(value)})
    with sessionmaker(bind=engine)() as db:
        db.add(fanout.PushDevice(user_id=1, device_id="phone", token="a" * 64, platform="apns", preferences_json=device_prefs))
        db.commit()
    sent = []
    provider = SimpleNamespace(send_push_result=lambda **sending: sent.append((Clock.current, sending["title"])) or SimpleNamespace(status="sent"))
    monkeypatch.setattr(fanout, "get_provider", lambda sandbox=False: provider)
    monkeypatch.setattr(fanout, "datetime", Clock)
    monkeypatch.setattr(quiet_hours, "datetime", Clock)
    monkeypatch.setattr(quiet_hours, "engine", engine)
    quiet_hours.invalidate_cache()
    yield SimpleNamespace(engine=engine, factory=sessionmaker(bind=engine), sent=sent)
    quiet_hours.invalidate_cache()
    engine.dispose()


def _rows(engine, title):
    with engine.connect() as connection:
        # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- constant fixture SQL
        return connection.execute(text("SELECT user_id FROM alerts WHERE title=:title"), {"title": title}).scalars().all()


def _backup_alert(error, phones):
    return lambda now: backup_failure_alerts.publish_backup_failure(error, session_factory=phones.factory, now=now)


def _integrity_alert(phones):
    status = {}
    return lambda now: record_integrity(status, REPORT, now, publish=lambda state: publish_integrity_alert(state, session_factory=phones.factory))


KINDS = {
    "refused": ("Database backup failed", lambda phones: _backup_alert(BackupValidationError("Insufficient free disk space for a verified backup"), phones)),
    "missed": ("Database backup failed", lambda phones: _backup_alert(BackupMissedError("missed"), phones)),
    "offsite_missed": ("Database backup failed", lambda phones: _backup_alert(BackupMissedError("offsite_missed"), phones)),
    "integrity": ("Database integrity check failed", _integrity_alert),
}


def _simulate(phones, kind, start, days=DAYS):
    title, build = KINDS[kind]
    attempt = build(phones)
    attempts = 0
    while (moment := start + attempts * STEP) < start + timedelta(days=days):
        Clock.current = moment
        attempt(moment)
        attempts += 1
    return title, attempts


@pytest.mark.parametrize("kind", KINDS)
def test_a_problem_that_starts_at_night_reaches_the_phone_every_day(phones, kind):
    title, attempts = _simulate(phones, kind, NIGHT)
    assert attempts >= DAYS
    assert len(phones.sent) == DAYS, "one to the phone a day, for as long as it lasts"
    assert all(when.hour == 3 for when, _ in phones.sent), "the limit keeps it at the same hour, inside the quiet window"
    assert {sent_title for _, sent_title in phones.sent} == {title}
    assert sorted(_rows(phones.engine, title)) == sorted([1, 2] * DAYS)


@pytest.mark.parametrize("kind", KINDS)
def test_a_problem_that_starts_by_day_is_the_same(phones, kind):
    _simulate(phones, kind, NOON, days=7)
    assert len(phones.sent) == 7


def test_an_ordinary_alert_still_waits_at_night_and_a_critical_one_is_not_special_by_severity(phones):
    Clock.current = NIGHT
    with phones.factory() as db:
        plan = alert_dispatcher.stage_alert(db, AlertType.MAINTENANCE_OVERDUE, AlertSeverity.CRITICAL, "Ordinary", "Maintenance is due",
                                            target_user_ids=[1])
        db.commit()
        alert_dispatcher.deliver_staged_alert(db, plan)
    assert plan.suppress_external is True and plan.admin_database_alert is False
    assert phones.sent == [] and _rows(phones.engine, "Ordinary") == [1]


def test_an_ordinary_alert_by_day_is_sent_and_the_phones_own_quiet_hours_still_count(phones):
    Clock.current = NOON
    with phones.factory() as db:
        plan = alert_dispatcher.stage_alert(db, AlertType.MAINTENANCE_OVERDUE, AlertSeverity.CRITICAL, "By day", "Maintenance is due",
                                            target_user_ids=[1])
        db.commit()
        alert_dispatcher.deliver_staged_alert(db, plan)
    assert [title for _, title in phones.sent] == ["By day"]
    # The system's window is open, the phone's isn't: for an ordinary alert the phone's own setting wins.
    phones.sent.clear()
    with phones.engine.begin() as connection:
        # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- constant fixture SQL
        connection.execute(text("UPDATE system_config SET value='\"false\"' WHERE key='quiet_hours_enabled'"))
    quiet_hours.invalidate_cache()
    Clock.current = NIGHT
    with phones.factory() as db:
        plan = alert_dispatcher.stage_alert(db, AlertType.MAINTENANCE_OVERDUE, AlertSeverity.CRITICAL, "On a quiet phone", "Maintenance is due",
                                            target_user_ids=[1])
        db.commit()
        alert_dispatcher.deliver_staged_alert(db, plan)
    assert plan.suppress_external is False and phones.sent == []
    # And the same phone, in the same minute, still gets this one.
    KINDS["integrity"][1](phones)(NIGHT)
    assert [title for _, title in phones.sent] == ["Database integrity check failed"]


def test_nothing_is_left_for_the_digest_to_send_again(phones):
    _simulate(phones, "refused", NIGHT, days=3)
    with phones.engine.connect() as connection:
        # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- constant fixture SQL
        assert connection.execute(text("SELECT count(*) FROM quiet_hours_deferred_deliveries")).scalar_one() == 0
