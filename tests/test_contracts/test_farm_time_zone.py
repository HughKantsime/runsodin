"""One farm time zone (spec: .claude/specs/farm-time-zone.md): where "today" starts.

Resolution order (core.farm_time.farm_zone): the saved farm_timezone; else the zone saved with
the quiet hours; else the server's TZ; else UTC. Stored timestamps stay naive UTC.
Unset pins, per site: dashboard and timeline = UTC midnight; Home Assistant day totals and
capacity "recorded on" = the server's TZ, else UTC.
"""
import json
import logging
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError
from sqlalchemy import text

from core import farm_time
from core.farm_time import (FARM_TIMEZONE_KEY, farm_day_start, farm_today, farm_zone, farm_zone_source,
                            get_farm_timezone, set_farm_timezone)
from core.models import SystemConfig
from core.scheduler_settings import BLACKOUT_KEY, get_blackout, get_blackout_timezone, set_blackout
from modules.system.schemas import ConfigUpdate
from tests.test_contracts.test_api_token_delegation_behavior import interactive_headers, token_client  # noqa: F401
from tests.test_contracts.test_batch_acceptance import batch  # noqa: F401
from tests.test_contracts.test_cert_E_ana_backend import FIXED_NOW, ana  # noqa: F401
from tests.test_contracts.test_cert_G_harness import run_probe
from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401
from tests.test_contracts.test_home_assistant_status import URL, ha_client  # noqa: F401
from tests.test_contracts.test_operations_preparation import BASE as OPS, ops  # noqa: F401
from tests.test_contracts.test_product_bom_integrity import bom_client  # noqa: F401
from tests.test_contracts.test_square_readonly_workflow import shop  # noqa: F401

UTC = timezone.utc


@pytest.fixture(autouse=True)
def no_server_tz(monkeypatch):
    monkeypatch.delenv("TZ", raising=False)


def _utc(*parts):
    return datetime(*parts, tzinfo=UTC)


# ---------------- resolution order ----------------

def test_resolution_order_setting_then_quiet_hours_then_server_tz_then_utc(review_db, monkeypatch):  # noqa: F811
    db = review_db
    assert get_farm_timezone(db) is None
    assert farm_zone_source(db) == ("UTC", "utc") and farm_zone(db) == ZoneInfo("UTC")  # 4: nothing anywhere
    monkeypatch.setenv("TZ", "America/Chicago")
    assert farm_zone_source(db) == ("America/Chicago", "server")  # 3: the server's TZ
    set_blackout(db, "22:30", "05:30", "Europe/Berlin"); db.commit()
    assert farm_zone_source(db) == ("Europe/Berlin", "quiet_hours")  # 2: the zone saved with the quiet hours
    set_farm_timezone(db, "America/New_York"); db.commit()
    assert farm_zone_source(db) == ("America/New_York", "setting")  # 1: the saved setting
    assert farm_zone(db) == ZoneInfo("America/New_York") and get_farm_timezone(db) == "America/New_York"
    # clearing it goes back down the order; the quiet-hours window was never touched
    set_farm_timezone(db, ""); db.commit()
    assert get_farm_timezone(db) is None and farm_zone_source(db) == ("Europe/Berlin", "quiet_hours")
    assert get_blackout(db) == ("22:30", "05:30") and get_blackout_timezone(db) == "Europe/Berlin"


def test_a_quiet_hours_window_saved_without_a_zone_gives_no_farm_zone_and_is_left_alone(review_db):  # noqa: F811
    db = review_db
    db.query(SystemConfig).filter(SystemConfig.key == BLACKOUT_KEY).delete()
    db.add(SystemConfig(key=BLACKOUT_KEY, value={"start": "21:00", "end": "06:00"})); db.commit()
    assert farm_zone_source(db) == ("UTC", "utc")
    set_farm_timezone(db, "America/New_York"); db.commit()
    # setting the farm zone does not give the legacy window a zone: the scheduler still reads it as UTC
    assert get_blackout_timezone(db) is None
    assert db.query(SystemConfig).filter(SystemConfig.key == BLACKOUT_KEY).one().value == {"start": "21:00", "end": "06:00"}


def test_unknown_zone_is_refused_on_save(review_db):  # noqa: F811
    for bad in ("Mars/Olympus", "EST5", "../etc/passwd"):
        with pytest.raises(ValueError, match="Unknown time zone"):
            set_farm_timezone(review_db, bad)
        with pytest.raises(ValidationError):
            ConfigUpdate(farm_timezone=bad)
    assert get_farm_timezone(review_db) is None
    assert ConfigUpdate(farm_timezone="America/New_York").farm_timezone == "America/New_York"
    assert ConfigUpdate(farm_timezone="").farm_timezone == ""  # "" clears the setting
    with pytest.raises(ValidationError):
        ConfigUpdate(blackout_timezone="")  # the quiet-hours zone keeps its own rule


def test_unknown_zone_is_ignored_with_a_warning_at_run_time(review_db, monkeypatch, caplog):  # noqa: F811
    db = review_db
    db.add(SystemConfig(key=FARM_TIMEZONE_KEY, value="Mars/Olympus")); db.commit()  # e.g. dropped from the zone database
    monkeypatch.setenv("TZ", "Not/AZone")
    with caplog.at_level(logging.WARNING, logger="odin.api"):
        assert farm_zone_source(db) == ("UTC", "utc")
        assert farm_day_start(db, _utc(2026, 11, 13, 3, 0)) == _utc(2026, 11, 13)
    assert "Mars/Olympus" in caplog.text and "Not/AZone" in caplog.text
    monkeypatch.setenv("TZ", "America/Chicago")  # the next rule applies
    assert farm_zone_source(db) == ("America/Chicago", "server")


# ---------------- the day boundary ----------------

@pytest.mark.parametrize("now,start", [
    (_utc(2026, 1, 15, 17, 0), _utc(2026, 1, 15, 5, 0)),    # January noon EST
    (_utc(2026, 1, 15, 4, 59), _utc(2026, 1, 14, 5, 0)),    # 23:59 the evening before
    (_utc(2026, 1, 15, 5, 0), _utc(2026, 1, 15, 5, 0)),     # midnight itself
    (_utc(2026, 7, 15, 16, 0), _utc(2026, 7, 15, 4, 0)),    # July noon EDT
    (_utc(2026, 7, 15, 3, 59), _utc(2026, 7, 14, 4, 0)),
    (_utc(2026, 3, 8, 6, 30), _utc(2026, 3, 8, 5, 0)),      # spring forward, 01:30 EST
    (_utc(2026, 3, 8, 16, 0), _utc(2026, 3, 8, 5, 0)),      # spring forward, noon EDT: midnight was EST
    (_utc(2026, 3, 9, 3, 59), _utc(2026, 3, 8, 5, 0)),      # that day is 23 hours long
    (_utc(2026, 3, 9, 4, 0), _utc(2026, 3, 9, 4, 0)),
    (_utc(2026, 11, 1, 5, 30), _utc(2026, 11, 1, 4, 0)),    # fall back, first 01:30 (EDT)
    (_utc(2026, 11, 1, 6, 30), _utc(2026, 11, 1, 4, 0)),    # fall back, second 01:30 (EST)
    (_utc(2026, 11, 1, 17, 0), _utc(2026, 11, 1, 4, 0)),    # fall back, noon EST: midnight was EDT
    (_utc(2026, 11, 2, 4, 59), _utc(2026, 11, 1, 4, 0)),    # that day is 25 hours long
    (_utc(2026, 11, 2, 5, 0), _utc(2026, 11, 2, 5, 0)),
])
def test_farm_day_start_in_new_york(review_db, now, start):  # noqa: F811
    set_farm_timezone(review_db, "America/New_York"); review_db.commit()
    assert farm_day_start(review_db, now) == start
    assert farm_day_start(review_db, now).tzinfo is UTC
    assert farm_day_start(review_db, now.replace(tzinfo=None)) == start  # a time with no zone is UTC
    assert farm_today(review_db, now) == now.astimezone(ZoneInfo("America/New_York")).date()


def test_unset_day_start_is_utc_midnight(review_db):  # noqa: F811
    assert farm_day_start(review_db, _utc(2026, 11, 13, 3, 30)) == _utc(2026, 11, 13)
    assert farm_today(review_db, _utc(2026, 11, 13, 3, 30)) == date(2026, 11, 13)
    assert _utc(2026, 1, 1) <= farm_day_start(review_db) <= datetime.now(UTC)  # no "now" given: the clock


# ---------------- dashboard "Completed today" ----------------

def _finish(db, job_id, ended):
    """One completed ODIN job and one print started at the printer, both ending at ``ended`` (naive UTC)."""
    db.execute(text("INSERT INTO jobs(id,item_name,status,printer_id,quantity,priority,created_at,actual_end,charged_to_org_id,hold) "
                    "VALUES (:id,'Farm day','completed',9,1,3,'2026-10-12 08:00:00',:end,1,0)"), {"id": job_id, "end": ended})
    db.execute(text("INSERT INTO print_jobs(printer_id,job_name,status,started_at,ended_at) "
                    "VALUES (9,'By hand.3mf','completed','2026-10-12 08:05:00',:end)"), {"end": ended})
    db.commit()


def _completed_today(client):
    return client.get("/stats", params={"days": 7}).json()["jobs"]["completed_today"]


def test_dashboard_counts_the_new_york_day(ana, monkeypatch):  # noqa: F811
    client, _identity, db = ana
    assert FIXED_NOW == _utc(2026, 10, 13, 12)  # 08:00 in New York
    before = _completed_today(client)
    _finish(db, 7201, datetime(2026, 10, 13, 1, 0))   # 21:00 New York time yesterday
    _finish(db, 7202, datetime(2026, 10, 13, 4, 30))  # 00:30 New York time today
    # unset: today's behaviour, the UTC day; both are after UTC midnight (a job and a print each)
    assert _completed_today(client) == before + 4
    set_farm_timezone(db, "America/New_York"); db.commit()
    assert _completed_today(client) == before + 2  # only the 00:30 pair
    set_farm_timezone(db, "Pacific/Auckland"); db.commit()  # 01:00 on the 14th there: nothing yet today
    assert _completed_today(client) == before
    # no setting: the zone saved with the quiet hours, then the server's TZ (these rules used to disagree)
    set_farm_timezone(db, None); set_blackout(db, "22:30", "05:30", "America/New_York"); db.commit()
    assert _completed_today(client) == before + 2
    db.query(SystemConfig).filter(SystemConfig.key == BLACKOUT_KEY).delete(); db.commit()
    assert _completed_today(client) == before + 4
    monkeypatch.setenv("TZ", "America/New_York")
    assert _completed_today(client) == before + 2


def test_dashboard_unset_boundary_is_utc_midnight(ana):  # noqa: F811
    client, _identity, db = ana
    before = _completed_today(client)
    _finish(db, 7203, datetime(2026, 10, 12, 23, 59, 59))
    assert _completed_today(client) == before
    _finish(db, 7204, datetime(2026, 10, 13, 0, 0, 1))  # (SQLite compares the stored text, so not 00:00:00 itself)
    assert _completed_today(client) == before + 2


# ---------------- Home Assistant ----------------

def _ha(client, issue, monkeypatch, now):
    from modules.printers import routes_integrations

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz)

    monkeypatch.setattr(routes_integrations, "datetime", FixedDateTime)
    response = client.get(URL, headers=issue(["read"]))
    assert response.status_code == 200
    return response.json()


def test_home_assistant_day_totals_and_timezone_follow_the_setting(ha_client, review_db, monkeypatch):  # noqa: F811
    client, issue = ha_client
    db, now = review_db, _utc(2026, 11, 12, 12)
    for ended in ("2026-11-12 00:30:00", "2026-11-12 04:30:00", "2026-11-12 05:30:00"):
        db.execute(text("INSERT INTO print_jobs(printer_id,job_name,status,started_at,ended_at) "
                        "VALUES(9,'part.gcode','completed','2026-11-11 00:00:00',:e)"), {"e": ended})
    db.commit()
    # unset and no TZ: UTC
    body = _ha(client, issue, monkeypatch, now)
    assert body["timezone"] == "UTC" and body["schema_version"] == 2
    assert body["fleet"]["today"] == {"plates_done": 3, "plates_failed": 0, "since": "2026-11-12T00:00:00Z"}
    # unset with the server's TZ: unchanged from before the setting existed
    monkeypatch.setenv("TZ", "America/New_York")
    body = _ha(client, issue, monkeypatch, now)
    assert body["timezone"] == "America/New_York"
    assert body["fleet"]["today"] == {"plates_done": 1, "plates_failed": 0, "since": "2026-11-12T05:00:00Z"}
    # the setting wins over TZ
    set_farm_timezone(db, "America/Halifax"); db.commit()
    body = _ha(client, issue, monkeypatch, now)
    assert body["timezone"] == "America/Halifax"
    assert body["fleet"]["today"] == {"plates_done": 2, "plates_failed": 0, "since": "2026-11-12T04:00:00Z"}


# ---------------- timeline window start and capacity "recorded on" ----------------

def _timeline_start(db):
    from modules.jobs import scheduler_routes
    admin = {"id": 3, "role": "admin", "group_id": None, "is_active": True}
    return scheduler_routes.get_timeline(start_date=None, days=1, current_user=admin, db=db).start_date


def test_timeline_default_start_is_the_farm_midnight_and_utc_when_unset(review_db, monkeypatch):  # noqa: F811
    from modules.jobs import scheduler_routes

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return _utc(2026, 11, 13, 3, 30).astimezone(tz)

    monkeypatch.setattr(scheduler_routes, "datetime", FixedDateTime)
    assert _timeline_start(review_db) == _utc(2026, 11, 13)  # unset: UTC midnight, as before
    set_farm_timezone(review_db, "America/New_York"); review_db.commit()
    assert _timeline_start(review_db) == _utc(2026, 11, 12, 5)  # 22:30 on the 12th in New York
    # an explicit start is used as given
    given = datetime(2026, 10, 12)
    admin = {"id": 3, "role": "admin", "group_id": None, "is_active": True}
    assert scheduler_routes.get_timeline(start_date=given, days=1, current_user=admin, db=review_db).start_date == given


def test_capacity_recorded_on_is_the_farm_date(ops, monkeypatch):  # noqa: F811
    client, db = ops[0], ops[3]

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return _utc(2026, 11, 13, 3, 30).astimezone(tz)

    monkeypatch.setattr(farm_time, "datetime", FixedDateTime)

    def record():
        state = client.get(OPS + "/conflicts").json()
        payload = {"revision": state["revision"], "starts_on": "2026-11-20", "ends_on": "2026-11-20", "available_minutes": 30, "estimates": []}
        response = client.put(OPS + "/capacity", json=payload)
        assert response.status_code == 200, response.text
        return response.json()["capacity"]["recorded_on"]

    assert record() == "2026-11-13"  # unset, no TZ: the UTC date
    monkeypatch.setenv("TZ", "America/New_York")
    assert record() == "2026-11-12"  # unset: the server's TZ, as before
    set_farm_timezone(db, "Asia/Tokyo"); db.commit()
    assert record() == "2026-11-13"  # the setting wins


# ---------------- the setting through the real app: RBAC, validation, audit ----------------

FARM_ZONE_PROBE = r'''
install_license("pro")
seed_user("tz-admin", "admin")
seed_user("tz-operator", "operator")
seed_user("tz-viewer", "viewer")
admin, operator, viewer = login("tz-admin"), login("tz-operator"), login("tz-viewer")
def seen():
    data = body(viewer.get("/api/config"))
    return [data.get("farm_timezone"), data.get("farm_timezone_effective")]
OUT["default"] = seen()
OUT["operator_put"] = operator.put("/api/config", json={"farm_timezone": "America/New_York"}).status_code
OUT["viewer_put"] = viewer.put("/api/config", json={"farm_timezone": "America/New_York"}).status_code
OUT["after_refused"] = seen()
OUT["invalid_put"] = admin.put("/api/config", json={"farm_timezone": "Mars/Olympus"}).status_code
OUT["after_invalid"] = seen()
OUT["put"] = admin.put("/api/config", json={"farm_timezone": "America/New_York"}).status_code
OUT["after_put"] = seen()
OUT["quiet_zone"] = body(viewer.get("/api/config")).get("blackout_timezone")
OUT["change"] = admin.put("/api/config", json={"farm_timezone": "Europe/Berlin"}).status_code
OUT["clear"] = admin.put("/api/config", json={"farm_timezone": ""}).status_code
OUT["after_clear"] = seen()
OUT["audit"] = [r[0] for r in sql("SELECT details FROM audit_logs WHERE action='config.updated' ORDER BY id")]
'''


@pytest.fixture(scope="module")
def zone_probe(tmp_path_factory):
    import os
    root = tmp_path_factory.mktemp("farm-zone")
    saved = os.environ.pop("TZ", None)
    try:
        return run_probe("farm-zone", FARM_ZONE_PROBE, root, {"ENV_FILE_PATH": str(root / "data.env")})
    finally:
        if saved is not None:
            os.environ["TZ"] = saved


def test_config_reports_the_setting_and_the_effective_zone_to_a_viewer(zone_probe):
    assert zone_probe["default"] == [None, {"zone": "UTC", "source": "utc"}]
    assert zone_probe["after_put"] == ["America/New_York", {"zone": "America/New_York", "source": "setting"}]
    assert zone_probe["after_clear"] == [None, {"zone": "UTC", "source": "utc"}]


def test_setting_write_is_admin_only_and_validated(zone_probe):
    assert zone_probe["operator_put"] == 403 and zone_probe["viewer_put"] == 403
    assert zone_probe["after_refused"] == zone_probe["default"]
    assert zone_probe["invalid_put"] == 422 and zone_probe["after_invalid"] == zone_probe["default"]
    assert zone_probe["put"] == 200 and zone_probe["change"] == 200 and zone_probe["clear"] == 200
    assert zone_probe["quiet_zone"] is None  # saving the farm zone never writes the quiet-hours zone


def test_setting_change_is_audited_by_name_with_old_and_new_value(zone_probe):
    details = [json.loads(d) if isinstance(d, str) else d for d in zone_probe["audit"]]
    changes = [d["farm_timezone"] for d in details if "farm_timezone" in d]
    assert changes == [{"from": None, "to": "America/New_York"}, {"from": "America/New_York", "to": "Europe/Berlin"},
                       {"from": "Europe/Berlin", "to": None}]
    assert all("farm_timezone" in d["keys"] for d in details if "farm_timezone" in d)
