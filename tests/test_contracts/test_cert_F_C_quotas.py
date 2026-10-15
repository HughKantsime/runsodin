"""Cert F / cluster C: Print Quotas (F11-11.8-01..04 backend side) on a real ODIN server.

Real admin quota API, real job creation/completion routes, real quota_usage schema. Period rollover is
simulated only by re-keying the user's real usage row to the previous window's key (the server reads the
wall clock); window keys themselves are checked on the production ``_get_period_key``.
"""
from __future__ import annotations

import itertools
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.quota import _get_period_key
from tests.test_contracts.test_cert_F_00_live import LiveOdin, strong_password

CAPTURE = Path(__file__).resolve().parents[2] / "artifacts/application-certification/cert-F/C-quota-wire.json"
_ips = (f"198.20.{a}.{b}" for a, b in itertools.product(range(1, 200), range(1, 250)))


def execute(o, statement: str, **params) -> None:
    from sqlalchemy import text
    with o.engine.begin() as c:
        c.execute(text(statement), params)


@pytest.fixture(scope="module")
def odin(tmp_path_factory):
    o = LiveOdin(tmp_path_factory.mktemp("odin_quota"), env={"ODIN_CERT_PROXY_HEADERS": "1"}).start()
    o.setup_admin()
    o.ah = o_login(o, *o.admin)
    o.capture = {}
    yield o
    o.stop()
    CAPTURE.write_text(json.dumps(o.capture, indent=1, sort_keys=True, default=str))


def o_login(o, user, pw):
    r = o.login(user, pw, **{"X-Forwarded-For": next(_ips)})
    assert r.status_code == 200, r.text
    return {"Authorization": "Bearer " + r.json()["access_token"]}


def make_user(o, name, role="operator"):
    pw = strong_password()
    r = o.post("/api/users", json={"username": name, "email": f"{name}@example.test", "password": pw, "role": role}, headers=o.ah)
    assert r.status_code == 200, r.text
    uid = o.sql("SELECT id FROM users WHERE username=:u", u=name)[0][0]
    return uid, o_login(o, name, pw)


def set_quota(o, uid, **body):
    r = o.put(f"/api/admin/quotas/{uid}", json=body, headers=o.ah)
    assert r.status_code == 200, r.text
    return r


def new_job(o, h, **extra):
    return o.post("/api/jobs", json={"item_name": "Quota synthetic", **extra}, headers=h)


# ----------------------------------------------------------------- 11.8-01 / 04 configuration API

def test_admin_sets_jobs_grams_hours_limits_and_list_reflects_them(odin):
    uid, _ = make_user(odin, "q_cfg")
    set_quota(odin, uid, quota_jobs=7, quota_grams=250.5, quota_hours=12.25, quota_period="weekly")
    rows = odin.get("/api/admin/quotas", headers=odin.ah).json()
    odin.capture["admin_quotas_list"] = rows
    row = next(r for r in rows if r["user_id"] == uid)
    assert row == {"user_id": uid, "username": "q_cfg", "quota_jobs": 7, "quota_grams": 250.5, "quota_hours": 12.25,
                   "quota_period": "weekly", "usage": {"grams_used": 0, "hours_used": 0, "jobs_used": 0}}
    # partial update + null clears a single limit
    set_quota(odin, uid, quota_grams=None)
    row = next(r for r in odin.get("/api/admin/quotas", headers=odin.ah).json() if r["user_id"] == uid)
    assert row["quota_grams"] is None and row["quota_jobs"] == 7 and row["quota_hours"] == 12.25
    a = odin.get("/api/audit-logs", params={"action": "quota_updated"}, headers=odin.ah).json()["logs"]
    assert any(e["entity_id"] == uid for e in a)


def test_quota_admin_api_refuses_non_admin(odin):
    uid, h = make_user(odin, "q_nonadmin")
    assert odin.get("/api/admin/quotas", headers=h).status_code == 403
    assert odin.put(f"/api/admin/quotas/{uid}", json={"quota_jobs": 1000}, headers=h).status_code == 403
    assert odin.get("/api/quotas", headers=h).status_code == 200


# ----------------------------------------------------------------- 11.8-03 job-count enforcement

def test_job_count_limit_enforced_at_creation(odin):
    uid, h = make_user(odin, "q_jobs")
    set_quota(odin, uid, quota_jobs=2, quota_period="monthly")
    assert [new_job(odin, h).status_code for _ in range(2)] == [201, 201]
    before = odin.sql("SELECT COUNT(*) FROM jobs")[0][0]
    r = new_job(odin, h)
    odin.capture["quota_exceeded_response"] = {"status": r.status_code, "body": r.json()}
    assert r.status_code == 429 and "quota exceeded" in r.text.lower()
    assert odin.sql("SELECT COUNT(*) FROM jobs")[0][0] == before
    mine = odin.get("/api/quotas", headers=h).json()
    odin.capture["my_quota"] = mine
    assert mine["usage"]["jobs_used"] == 2 and mine["period_key"] == _get_period_key("monthly")
    # bulk creation cannot overshoot either
    set_quota(odin, uid, quota_jobs=3)
    r = odin.post("/api/jobs/bulk", json={"jobs": [{"item_name": "b1"}, {"item_name": "b2"}]}, headers=h)
    assert r.status_code in (422, 429), r.text
    assert odin.sql("SELECT COUNT(*) FROM jobs")[0][0] == before


# ----------------------------------------------------------------- 11.8-02 periods: real windows + reset

PREV = {"daily": timedelta(days=1), "weekly": timedelta(days=7), "monthly": timedelta(days=32), "semester": timedelta(days=190)}


@pytest.mark.parametrize("period", ["daily", "weekly", "monthly", "semester"])
def test_period_window_accounting_and_reset(odin, period):
    uid, h = make_user(odin, f"q_{period}")
    set_quota(odin, uid, quota_jobs=1, quota_period=period)
    assert new_job(odin, h).status_code == 201
    assert new_job(odin, h).status_code == 429
    key = _get_period_key(period)
    assert odin.get("/api/quotas", headers=h).json()["period_key"] == key
    assert odin.sql("SELECT period_key, jobs_used FROM quota_usage WHERE user_id=:u", u=uid) == [(key, 1)]
    # Roll the clock: the consumed row now belongs to the previous window.
    prev_key = _get_period_key(period, now=datetime.now(timezone.utc) - PREV[period])
    assert prev_key != key
    execute(odin, "UPDATE quota_usage SET period_key=:p WHERE user_id=:u", p=prev_key, u=uid)
    assert new_job(odin, h).status_code == 201          # new window -> fresh allowance
    assert new_job(odin, h).status_code == 429
    assert sorted(odin.sql("SELECT period_key, jobs_used FROM quota_usage WHERE user_id=:u", u=uid)) == sorted([(prev_key, 1), (key, 1)])


def test_period_keys_follow_calendar_windows():
    utc = timezone.utc
    k = lambda p, *a: _get_period_key(p, now=datetime(*a, tzinfo=utc))
    assert k("daily", 2026, 3, 9, 23, 59) != k("daily", 2026, 3, 10, 0, 0)
    assert k("daily", 2026, 3, 10, 0, 0) == k("daily", 2026, 3, 10, 23, 59)
    assert k("monthly", 2026, 3, 1) == k("monthly", 2026, 3, 31) != k("monthly", 2026, 4, 1)
    assert k("semester", 2026, 1, 1) == k("semester", 2026, 6, 30) == "2026-S1"
    assert k("semester", 2026, 7, 1) == k("semester", 2026, 12, 31) == "2026-S2"
    assert k("semester", 2027, 1, 1) == "2027-S1"
    # mid-year Monday..Sunday share one weekly key; the next Monday starts a new one
    week = [k("weekly", 2026, 3, d) for d in range(9, 16)]
    assert len(set(week)) == 1 and k("weekly", 2026, 3, 16) != week[0]


@pytest.mark.xfail(strict=True, reason=(
    "DEFECT: backend/core/quota.py:20 weekly key uses strftime('%Y-W%W'), which restarts at week 00 on 1 January. "
    "Scenario: the Monday 2025-12-29 .. Sunday 2026-01-04 week is split into 2025-W52 and 2026-W00, so a student "
    "with a 5-jobs/week quota can submit 5 jobs on 31 Dec and 5 more on 1 Jan in the same calendar week."))
def test_weekly_window_is_one_calendar_week_across_new_year():
    utc = timezone.utc
    keys = {_get_period_key("weekly", now=datetime(2025, 12, 29, tzinfo=utc) + timedelta(days=d)) for d in range(7)}
    assert len(keys) == 1, keys


def test_unlisted_period_value_is_silently_treated_as_monthly(odin):
    uid, h = make_user(odin, "q_badperiod")
    r = odin.put(f"/api/admin/quotas/{uid}", json={"quota_jobs": 1, "quota_period": "fortnightly"}, headers=odin.ah)
    # Observation only (accepted, stored verbatim, accounted monthly); not one of the advertised periods.
    assert r.status_code == 200
    assert odin.get("/api/quotas", headers=h).json()["period_key"] == _get_period_key("monthly")


# ----------------------------------------------------------------- grams / hours limits

GRAMS_HOURS_DEFECT = (
    "DEFECT: no production code ever writes quota_usage.grams_used or hours_used (only charge_job_quota at "
    "backend/core/quota.py:47-84 increments jobs_used; grams/hours are compared against recorded usage that stays 0; "
    "job completion at backend/modules/jobs/routes/jobs_lifecycle.py:85 does not charge them). Scenario: an admin "
    "gives a student a 50 g / 1 h quota; the student prints a 100 g, 5 h model, completes it, and keeps creating "
    "jobs indefinitely: the grams and hours limits never block anything.")


def _model(o, h, grams, hours):
    r = o.post("/api/models", json={"name": "Quota model %s" % grams, "build_time_hours": hours,
                                    "color_requirements": {"color1": {"color": "black", "grams": grams}}}, headers=h)
    assert r.status_code == 201, r.text
    return r.json()["id"]


@pytest.mark.parametrize("field,limit", [("quota_grams", 50), ("quota_hours", 1)])
@pytest.mark.xfail(strict=True, reason=GRAMS_HOURS_DEFECT)
def test_completed_print_consumes_grams_or_hours_and_blocks_next_job(odin, field, limit):
    uid, h = make_user(odin, f"q_{field}")
    set_quota(odin, uid, **{field: limit, "quota_period": "monthly"})
    mid = _model(odin, h, 100, 5)
    r = new_job(odin, h, model_id=mid, duration_hours=5)
    assert r.status_code == 201, r.text
    jid = r.json()["id"]
    done = odin.post(f"/api/jobs/{jid}/complete", headers=h)
    assert done.status_code == 200, done.text
    usage = odin.get("/api/quotas", headers=h).json()["usage"]
    assert usage["grams_used" if field == "quota_grams" else "hours_used"] > 0, usage
    assert new_job(odin, h).status_code == 429


@pytest.mark.parametrize("field,col,used", [("quota_grams", "grams_used", 60), ("quota_hours", "hours_used", 2)])
def test_recorded_grams_or_hours_exhaustion_blocks_creation(odin, field, col, used):
    """Only reachable when usage is recorded by something other than ODIN (see GRAMS_HOURS_DEFECT)."""
    uid, h = make_user(odin, f"q_rec_{col}")
    set_quota(odin, uid, **{field: 50 if col == "grams_used" else 1, "quota_period": "monthly"})
    execute(odin, f"INSERT INTO quota_usage (user_id, period_key, {col}) VALUES (:u, :p, :v)", u=uid,
            p=_get_period_key("monthly"), v=used)
    assert new_job(odin, h).status_code == 429
