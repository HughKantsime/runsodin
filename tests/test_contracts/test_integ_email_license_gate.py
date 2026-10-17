"""Review fix O6: report and quiet-hours digest emails need email_notifications (Pro).

After a downgrade (or on Community) scheduled reports and digest emails were still
mailed; alert email was already gated. Creating a scheduled report (an email) is
Pro-gated like alert email."""
from sqlalchemy import text
from sqlalchemy.orm import Session

from tests.test_contracts.test_education_review_workflow import review_db  # noqa: F401
from tests.test_contracts.test_quiet_hours_delivery_behavior import digest_setup  # noqa: F401
from tests.test_contracts.test_report_daemon_behavior import seed


def _report_calls(review_db, monkeypatch):
    from modules.reporting import report_runner as runner
    seed(review_db)
    monkeypatch.setattr(runner, "get_smtp_config", lambda session: {"synthetic": True})
    monkeypatch.setitem(runner.GENERATORS, "job_summary", lambda session, filters: "<p>report</p>")
    calls = []
    monkeypatch.setattr(runner, "send_report_email", lambda c, r, s, h: calls.append(r) or True)
    runner.process_due_reports(review_db)
    return calls


def test_due_report_is_not_emailed_without_the_email_entitlement(review_db, monkeypatch, tier_license):  # noqa: F811
    tier_license(None)  # Community: no license file
    assert _report_calls(review_db, monkeypatch) == []


def test_due_report_is_emailed_on_pro(review_db, monkeypatch, tier_license):  # noqa: F811
    tier_license("pro")
    assert _report_calls(review_db, monkeypatch) == ["synthetic@example.test"]


def _digest(digest_setup, review_db, monkeypatch):  # noqa: F811
    runner = digest_setup
    review_db.execute(text("INSERT INTO alert_preferences(user_id,alert_type,email,browser_push) VALUES(3,'print_complete',1,0)"))
    review_db.commit()
    calls = []
    monkeypatch.setattr(runner, "_deliver_digest_email", lambda *a, **k: calls.append("email") or True)
    runner.process_quiet_hours_digest(review_db)
    with Session(review_db.get_bind()) as fresh:
        status = fresh.execute(text("SELECT delivery_status FROM quiet_hours_digest_sends WHERE user_id=3")).scalar_one()
    return calls, status


def test_digest_email_is_not_sent_without_the_email_entitlement(digest_setup, review_db, monkeypatch, tier_license):  # noqa: F811
    tier_license(None)
    calls, status = _digest(digest_setup, review_db, monkeypatch)
    assert calls == [] and "email:unlicensed" in status, status


def test_digest_email_is_sent_on_pro(digest_setup, review_db, monkeypatch, tier_license):  # noqa: F811
    tier_license("pro")
    calls, status = _digest(digest_setup, review_db, monkeypatch)
    assert calls == ["email"] and status == "sent", status


def test_creating_and_running_scheduled_reports_is_pro_gated():
    from fastapi.routing import APIRoute
    from modules.reporting.routes.reports import router
    gated = {}
    for route in router.routes:
        if isinstance(route, APIRoute) and route.path in ("/report-schedules", "/report-schedules/{schedule_id}/run"):
            for method in route.methods:
                gated[(method, route.path)] = any(getattr(d.call, "__name__", "") == "require_feature_email_notifications"
                                                  for d in route.dependant.dependencies)
    assert gated[("POST", "/report-schedules")] and gated[("POST", "/report-schedules/{schedule_id}/run")], gated
