"""Damage and backup alerts reach the phone past Education's boundary, which ordinary alerts don't.

These go to platform admins, name no printer, job or spool, and carry fixed text, so they hold no
student data. A failed read of the setting used to count as Education mode on any install, so damage
in the page holding the setting hid the alert reporting that damage, every day until it was repaired.
"""
import sqlite3
from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DatabaseError

from core.base import AlertSeverity, AlertType
from modules.notifications import alert_dispatcher
from modules.push import fanout
from tests.test_contracts.test_db_alerts_quiet_hours import KINDS, NOON, Clock, _rows, _simulate, phones  # noqa: F401


def _education_on(phones):
    with phones.engine.begin() as connection:
        # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- constant fixture SQL
        connection.execute(text("INSERT INTO system_config(key,value) VALUES('education_mode','\"true\"')"))


def _ordinary(phones, title="Ordinary", **resource):
    with phones.factory() as db:
        plan = alert_dispatcher.stage_alert(db, AlertType.MAINTENANCE_OVERDUE, AlertSeverity.CRITICAL, title, "Maintenance is due",
                                            target_user_ids=[1], **resource)
        db.commit()
        alert_dispatcher.deliver_staged_alert(db, plan)
    return plan


@pytest.mark.parametrize("kind", KINDS)
def test_with_education_on_these_reach_the_phone_every_day(phones, kind):
    _education_on(phones)
    title, _ = _simulate(phones, kind, NOON, days=7)
    assert len(phones.sent) == 7
    assert sorted(_rows(phones.engine, title)) == sorted([1, 2] * 7)


def test_with_education_on_an_ordinary_alert_still_stops_at_the_boundary(phones):
    _education_on(phones)
    Clock.current = NOON
    plan = _ordinary(phones)
    assert plan.suppress_external is False, "it's the boundary that holds it, not quiet hours"
    assert phones.sent == [] and _rows(phones.engine, "Ordinary") == [1]


def test_with_education_off_an_ordinary_alert_is_sent(phones):
    Clock.current = NOON
    _ordinary(phones)
    assert [title for _, title in phones.sent] == ["Ordinary"]


def test_a_failed_read_of_the_setting_stops_ordinary_alerts_and_not_these(phones, monkeypatch):
    def unreadable(*arguments, **keywords):
        raise sqlite3.DatabaseError("database disk image is malformed")
    monkeypatch.setattr(fanout, "education_scope", unreadable)
    Clock.current = NOON
    _ordinary(phones)
    assert phones.sent == [], "fails closed, as it did"
    KINDS["integrity"][1](phones)(NOON)
    assert [title for _, title in phones.sent] == ["Database integrity check failed"]


def _zero_pages_holding(path, needle):
    with open(path, "r+b") as database:
        content = database.read()
        size = int.from_bytes(content[16:18], "big")
        pages = {index // size for index in _find_all(content, needle)}
        assert pages and 0 not in pages
        for page in pages:
            database.seek(page * size)
            database.write(bytes(size))
    return pages


def _find_all(content, needle):
    start = content.find(needle)
    while start >= 0:
        yield start
        start = content.find(needle, start + 1)


@pytest.mark.parametrize("kind", ["integrity", "refused", "missed"])
def test_real_damage_in_the_page_holding_the_setting_doesnt_hide_the_alert(phones, kind):
    """The shape the reviewer found: every other page of system_config is fine, and the phone was the only miss."""
    with phones.engine.begin() as connection:
        for index in range(4000):
            if index == 2000:
                # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- constant fixture SQL
                connection.execute(text("INSERT INTO system_config(key,value) VALUES('education_mode','\"false\"')"))
            # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- constant fixture SQL
            connection.execute(text("INSERT INTO system_config(key,value) VALUES(:key,'\"x\"')"), {"key": f"education_filler_{index:05d}"})
    phones.engine.dispose()
    _zero_pages_holding(phones.engine.url.database, b"education_mode")
    with phones.engine.connect() as connection:
        with pytest.raises(DatabaseError):
            # nosemgrep: python.sqlalchemy.security.audit.avoid-sqlalchemy-text.avoid-sqlalchemy-text -- constant fixture SQL
            connection.execute(text("SELECT value FROM system_config WHERE key='education_mode'")).first()
    title, attempts = KINDS[kind][0], KINDS[kind][1](phones)
    Clock.current = NOON
    attempts(NOON)
    assert [sent_title for _, sent_title in phones.sent] == [title]
    assert sorted(_rows(phones.engine, title)) == [1, 2]
    # And it keeps coming until someone repairs it.
    tomorrow = NOON + timedelta(days=1, minutes=1)
    Clock.current = tomorrow
    attempts(tomorrow)
    assert len(phones.sent) == 2
    # An ordinary alert meets the same unreadable setting and stays held.
    _ordinary(phones)
    assert len(phones.sent) == 2


@pytest.mark.parametrize("resource", [{"printer_id": 1}, {"job_id": 1}, {"spool_id": 1}])
def test_one_that_names_a_resource_is_refused(phones, resource):
    """The exemption rests on naming nothing, so nothing may pass it while naming something."""
    with phones.factory() as db:
        with pytest.raises(ValueError, match="names no printer"):
            alert_dispatcher.stage_alert(db, AlertType.MAINTENANCE_OVERDUE, AlertSeverity.CRITICAL, "Named", "Text",
                                         target_user_ids=[1], admin_database_alert=True, **resource)
    assert _rows(phones.engine, "Named") == []
